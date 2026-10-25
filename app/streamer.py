"""Telegram -> HTTP streaming for the Mini App player.

Shape borrowed from the file-to-link projects (biisal, tgfilestream),
rewritten for this codebase:

- A stream URL is /api/stream/<token>. The token is HMAC-signed
  server-side and carries the catalog file id + an expiry, so links
  can't be forged or enumerated. The Mini App gets a token from
  POST /api/stream_token after proving its Telegram user id via
  initData — no login, no START needed, since nothing is delivered
  to the user's DM.
- Bytes come from Telegram's DCs in 1MB chunks via raw upload.GetFile
  on a cached per-DC media session. Nothing is written to disk, and
  nothing goes through the 20MB Bot API download limit.
- HTTP Range is honored, so the browser starts instantly and can seek.
  Chunk math: the requested range is aligned down to a 1MB boundary;
  the first and last chunks are sliced to the exact range. (The
  reference implementations get the part count wrong when the range
  end lands exactly on a chunk boundary; part_count here is
  end//chunk - offset//chunk + 1, which is correct in all cases.)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from hashlib import sha256

from pyrogram import raw, utils
from pyrogram.crypto import aes
from pyrogram.errors import (AuthBytesInvalid, CDNFileHashMismatch,
                             FileReferenceExpired, VolumeLocNotFound)
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

log = logging.getLogger("mediabot.streamer")

CHUNK_SIZE = 1024 * 1024
PROPS_TTL = 30 * 60
TOKEN_TTL = 60 * 60
MAX_STREAMS = 25

_MEDIA_ATTRS = ("document", "video", "audio", "animation", "voice",
                "video_note")


class StreamFileNotFound(Exception):
    pass


# -- signed tokens ---------------------------------------------------------------
def stream_secret(bot_token: str, configured: str = "") -> str:
    """Signing secret: explicit STREAM_SECRET if set, else a stable value
    derived from the bot token (server-side only, never leaves the box)."""
    if configured:
        return configured
    return hmac.new(b"core-tg-stream-v1", bot_token.encode(),
                    hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(secret: str, file_id: int, ttl: int = TOKEN_TTL) -> str:
    payload = _b64(json.dumps({"fid": int(file_id),
                               "exp": int(time.time()) + ttl},
                              separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(),
                        hashlib.sha256).digest())
    return payload + "." + sig


def verify_token(secret: str, token: str) -> int | None:
    """Returns the catalog file id, or None for a bad/expired token."""
    try:
        payload, sig = token.split(".", 1)
        expect = _b64(hmac.new(secret.encode(), payload.encode(),
                               hashlib.sha256).digest())
        if not hmac.compare_digest(expect, sig):
            return None
        data = json.loads(_unb64(payload))
        if int(data["exp"]) < time.time():
            return None
        return int(data["fid"])
    except Exception:
        return None


# -- HTTP Range ------------------------------------------------------------------
def parse_range(header: str, size: int) -> tuple[int, int]:
    """Parse a Range header into an inclusive (start, end). Raises
    ValueError for anything unsatisfiable (caller answers 416)."""
    if not header.startswith("bytes="):
        raise ValueError("not bytes")
    spec = header[len("bytes="):].strip()
    if "," in spec:
        raise ValueError("multi-range unsupported")
    start_s, _, end_s = spec.partition("-")
    if start_s == "":
        # suffix: last N bytes
        n = int(end_s)
        if n <= 0:
            raise ValueError("bad suffix")
        start, end = max(size - n, 0), size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError("out of range")
    return start, min(end, size - 1)


def chunk_plan(start: int, end: int, chunk: int = CHUNK_SIZE):
    """(offset, first_cut, last_cut, part_count) for a byte range."""
    offset = start - (start % chunk)
    return (offset, start - offset, end % chunk + 1,
            end // chunk - offset // chunk + 1)


# -- Telegram chunk fetcher ------------------------------------------------------
class ByteStreamer:
    """Resolves catalog files to Telegram media and yields their bytes
    for one HTTP range. One instance per bot client; media sessions are
    cached per DC, file properties per (chat_id, msg_id) for 30 min."""

    def __init__(self, client):
        self.client = client
        self._props: dict[tuple[int, int], tuple[float, dict]] = {}
        self.active = 0

    async def resolve(self, chat_id: int, msg_id: int,
                      force: bool = False) -> dict:
        key = (chat_id, msg_id)
        hit = self._props.get(key)
        if hit and not force and time.monotonic() - hit[0] < PROPS_TTL:
            return hit[1]
        msg = await self.client.get_messages(chat_id, msg_id)
        if not msg or getattr(msg, "empty", False):
            log.warning("stream resolve: no message %s/%s (empty=%r)",
                        chat_id, msg_id, getattr(msg, "empty", None))
            raise StreamFileNotFound
        media = next((getattr(msg, a, None) for a in _MEDIA_ATTRS), None)
        if not media and utils.get_peer_type(chat_id) == "channel":
            # A wrong-type stored peer makes get_messages take the bare
            # messages.GetMessages branch, which returns the message
            # shell WITHOUT media (proven live: right id + caption,
            # media None). Re-fetch channel-scoped, bypassing storage.
            log.warning("stream resolve: get_messages gave no media "
                        "%s/%s, trying direct channel fetch", chat_id, msg_id)
            fb = await self._fetch_channel_message(chat_id, msg_id)
            if fb is not None:
                msg = fb
                media = next((getattr(msg, a, None)
                              for a in _MEDIA_ATTRS), None)
                if media is not None:
                    log.info("stream resolve: direct channel fetch "
                             "recovered media %s/%s", chat_id, msg_id)
        if not media:
            # Diagnostic: is Telegram serving a different/stripped message
            # to this client than the catalog row describes?
            chat = getattr(msg, "chat", None)
            log.warning(
                "stream resolve: no media req=%s/%s got_msg_id=%r "
                "got_chat_id=%r text=%r caption=%r",
                chat_id, msg_id, getattr(msg, "id", None),
                getattr(chat, "id", None),
                (getattr(msg, "text", "") or "")[:80],
                (getattr(msg, "caption", "") or "")[:80])
            raise StreamFileNotFound
        fid = FileId.decode(media.file_id)
        props = {
            "chat_id": chat_id, "msg_id": msg_id,
            "media_id": fid.media_id, "access_hash": fid.access_hash,
            "file_reference": fid.file_reference, "dc_id": fid.dc_id,
            "file_size": getattr(media, "file_size", 0) or 0,
            "mime_type": getattr(media, "mime_type", "") or "",
            "file_name": getattr(media, "file_name", "") or "",
        }
        self._props[key] = (time.monotonic(), props)
        return props

    async def _fetch_channel_message(self, chat_id: int, msg_id: int):
        """Direct channel-scoped fetch, bypassing peer storage.

        Returns a Message or None. GetChannels with access_hash=0 is
        the same re-resolution resolve_peer does for unknown peers.
        """
        try:
            channel_id = utils.get_channel_id(chat_id)
            chats = await self.client.invoke(
                raw.functions.channels.GetChannels(id=[
                    raw.types.InputChannel(channel_id=channel_id,
                                           access_hash=0)]))
            log.warning("stream channel fallback: GetChannels %s/%s -> "
                        "%d chats", chat_id, msg_id, len(chats.chats))
            if not chats.chats:
                return None
            r = await self.client.invoke(
                raw.functions.channels.GetMessages(
                    channel=raw.types.InputChannel(
                        channel_id=channel_id,
                        access_hash=chats.chats[0].access_hash),
                    id=[raw.types.InputMessageID(id=msg_id)]))
            raw_first = r.messages[0] if r.messages else None
            raw_media = getattr(raw_first, "media", None)
            raw_doc = getattr(raw_media, "document", None)
            log.warning("stream channel fallback: GetMessages %s/%s -> "
                        "%d msgs, first type=%s media=%s doc=%s "
                        "doc_size=%r doc_mime=%r doc_attrs=%s",
                        chat_id, msg_id, len(r.messages),
                        type(raw_first).__name__ if raw_first else None,
                        type(raw_media).__name__ if raw_first else None,
                        type(raw_doc).__name__
                        if raw_doc is not None else None,
                        getattr(raw_doc, "size", None),
                        getattr(raw_doc, "mime_type", None),
                        [type(a).__name__ for a in
                         getattr(raw_doc, "attributes", [])]
                        if raw_doc is not None else None)
            parsed = await utils.parse_messages(self.client, r, replies=0)
            if parsed:
                p0 = parsed[0]
                log.warning("stream channel fallback: parsed %s/%s -> "
                            "attrs=%s caption=%r",
                            chat_id, msg_id,
                            [a for a in _MEDIA_ATTRS
                             if getattr(p0, a, None) is not None],
                            (getattr(p0, "caption", "") or "")[:80])
            return parsed[0] if parsed else None
        except Exception as e:
            log.warning("stream channel fallback failed %s/%s: %s",
                        chat_id, msg_id, type(e).__name__)
            return None

    async def media_session(self, dc_id: int) -> Session:
        client = self.client
        sess = client.media_sessions.get(dc_id)
        if sess is not None:
            return sess
        test_mode = await client.storage.test_mode()
        if dc_id != await client.storage.dc_id():
            sess = Session(client, dc_id,
                           await Auth(client, dc_id, test_mode).create(),
                           test_mode, is_media=True)
            await sess.start()
            for _ in range(6):
                exported = await client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                try:
                    await sess.send(raw.functions.auth.ImportAuthorization(
                        id=exported.id, bytes=exported.bytes))
                    break
                except AuthBytesInvalid:
                    continue
            else:
                await sess.stop()
                raise AuthBytesInvalid
        else:
            sess = Session(client, dc_id, await client.storage.auth_key(),
                           test_mode, is_media=True)
            await sess.start()
        client.media_sessions[dc_id] = sess
        return sess

    def _location(self, props: dict):
        return raw.types.InputDocumentFileLocation(
            id=props["media_id"], access_hash=props["access_hash"],
            file_reference=props["file_reference"], thumb_size="")

    async def _cdn_session(self, dc_id: int) -> Session:
        """Fresh auth session to a CDN DC (CDN sessions never reuse the
        main session's auth; mirrors Pyrogram's own download_media)."""
        client = self.client
        test_mode = await client.storage.test_mode()
        sess = Session(client, dc_id,
                       await Auth(client, dc_id, test_mode).create(),
                       test_mode, is_media=True, is_cdn=True)
        await sess.start()
        return sess

    async def _cdn_chunk(self, cdn_session, main_session, redirect,
                         offset: int) -> bytes | None:
        """One decrypted CDN chunk at offset, or None if unavailable."""
        while True:
            r2 = await cdn_session.invoke(raw.functions.upload.GetCdnFile(
                file_token=redirect.file_token, offset=offset,
                limit=CHUNK_SIZE))
            if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                try:
                    await main_session.invoke(
                        raw.functions.upload.ReuploadCdnFile(
                            file_token=redirect.file_token,
                            request_token=r2.request_token))
                except VolumeLocNotFound:
                    return None
                continue
            decrypted = aes.ctr256_decrypt(
                r2.bytes, redirect.encryption_key,
                bytearray(redirect.encryption_iv[:-4]
                          + (offset // 16).to_bytes(4, "big")))
            hashes = await main_session.invoke(
                raw.functions.upload.GetCdnFileHashes(
                    file_token=redirect.file_token, offset=offset))
            for i, h in enumerate(hashes):
                part = decrypted[h.limit * i: h.limit * (i + 1)]
                CDNFileHashMismatch.check(
                    h.hash == sha256(part).digest(),
                    "cdn chunk hash mismatch")
            return decrypted

    async def yield_range(self, props: dict, start: int, end: int):
        """Yield exactly bytes start..end (inclusive) of the file."""
        offset, first_cut, last_cut, part_count = chunk_plan(start, end)
        loc = self._location(props)
        sess = await self.media_session(props["dc_id"])
        current = 1
        refreshed = False
        redirect = None
        cdn_session = None
        try:
            while True:
                if redirect is None:
                    try:
                        try:
                            r = await sess.invoke(
                                raw.functions.upload.GetFile(
                                    location=loc, offset=offset,
                                    limit=CHUNK_SIZE),
                                timeout=30)
                        except TimeoutError:
                            # slow first chunk / flaky DC path: one retry
                            # at the same offset (safe: same bytes)
                            r = await sess.invoke(
                                raw.functions.upload.GetFile(
                                    location=loc, offset=offset,
                                    limit=CHUNK_SIZE),
                                timeout=30)
                    except FileReferenceExpired:
                        # saved file references go stale; re-resolve once
                        if refreshed:
                            raise
                        refreshed = True
                        props = await self.resolve(props["chat_id"],
                                                   props["msg_id"],
                                                   force=True)
                        loc = self._location(props)
                        sess = await self.media_session(props["dc_id"])
                        continue
                    if isinstance(r, raw.types.upload.FileCdnRedirect):
                        # popular files live on Telegram's CDN: switch to
                        # a CDN session instead of yielding nothing
                        redirect = r
                        cdn_session = await self._cdn_session(redirect.dc_id)
                        continue
                    if not isinstance(r, raw.types.upload.File) or not r.bytes:
                        break
                    data = r.bytes
                else:
                    data = await self._cdn_chunk(cdn_session, sess, redirect,
                                                 offset)
                    if not data:
                        break
                if part_count == 1:
                    yield data[first_cut:last_cut]
                elif current == 1:
                    yield data[first_cut:]
                elif current == part_count:
                    yield data[:last_cut]
                else:
                    yield data
                current += 1
                offset += CHUNK_SIZE
                if current > part_count:
                    break
        finally:
            if cdn_session is not None:
                try:
                    await cdn_session.stop()
                except Exception:
                    pass
