"""Offline tests for Mini App streaming: tokens, Range parsing, chunk math.

No Telegram access: the chunk-slicing test drives ByteStreamer.yield_range
with a stub media session that serves canned 1MB chunks.
"""
import asyncio
import unittest

from app.streamer import (ByteStreamer, chunk_plan, parse_range, sign_token,
                          stream_secret, verify_token)


class TokenTest(unittest.TestCase):
    def test_roundtrip(self):
        s = stream_secret("bot-token-123", "")
        tok = sign_token(s, 4242)
        self.assertEqual(verify_token(s, tok), 4242)

    def test_tampered_payload_rejected(self):
        s = stream_secret("bot-token-123", "")
        tok = sign_token(s, 4242)
        payload, sig = tok.split(".", 1)
        bad = ("A" if payload[0] != "A" else "B") + payload[1:]
        self.assertIsNone(verify_token(s, bad + "." + sig))

    def test_tampered_sig_rejected(self):
        s = stream_secret("bot-token-123", "")
        tok = sign_token(s, 4242)
        payload, sig = tok.split(".", 1)
        bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        self.assertIsNone(verify_token(s, payload + "." + bad_sig))

    def test_expired_rejected(self):
        s = stream_secret("bot-token-123", "")
        tok = sign_token(s, 4242, ttl=-10)
        self.assertIsNone(verify_token(s, tok))

    def test_wrong_secret_rejected(self):
        tok = sign_token(stream_secret("aaa", ""), 4242)
        self.assertIsNone(verify_token(stream_secret("bbb", ""), tok))

    def test_explicit_secret_wins(self):
        self.assertNotEqual(stream_secret("tok", "explicit"),
                            stream_secret("tok", ""))


class RangeTest(unittest.TestCase):
    def test_open_ended(self):
        self.assertEqual(parse_range("bytes=500-", 1000), (500, 999))

    def test_closed(self):
        self.assertEqual(parse_range("bytes=0-99", 1000), (0, 99))

    def test_suffix(self):
        self.assertEqual(parse_range("bytes=-200", 1000), (800, 999))

    def test_end_clamped(self):
        self.assertEqual(parse_range("bytes=900-5000", 1000), (900, 999))

    def test_bad(self):
        for h in ("bytes=1000-", "bytes=50-10", "items=0-10",
                  "bytes=0-10,20-30", "bytes=-0"):
            with self.assertRaises(ValueError, msg=h):
                parse_range(h, 1000)


class ChunkPlanTest(unittest.TestCase):
    def test_exact_chunk(self):
        # range ending exactly on a chunk boundary needs TWO parts
        self.assertEqual(chunk_plan(0, 1048576), (0, 0, 1, 2))

    def test_single_chunk(self):
        self.assertEqual(chunk_plan(0, 1048575), (0, 0, 1048576, 1))

    def test_mid_range(self):
        offset, first, last, parts = chunk_plan(5, 1048576)
        self.assertEqual((offset, first, last, parts), (0, 5, 1, 2))

    def test_aligned_middle(self):
        self.assertEqual(chunk_plan(1048576, 2097151),
                         (1048576, 0, 1048576, 1))


class FakeFile:
    """A real raw.types.upload.File wrapping canned bytes."""

    def __new__(cls, data):
        from pyrogram import raw
        return raw.types.upload.File(
            type=raw.types.storage.FileUnknown(), mtime=0, bytes=data)


class FakeSession:
    """Serves a fixed byte string in 1MB GetFile chunks."""

    def __init__(self, blob):
        self.blob = blob

    async def send(self, req):
        return FakeFile(self.blob[req.offset:req.offset + req.limit])


class YieldRangeTest(unittest.TestCase):
    def run_yield(self, blob, start, end):
        streamer = ByteStreamer(client=None)
        props = {"chat_id": 1, "msg_id": 2, "dc_id": 1, "media_id": 7,
                 "access_hash": 8, "file_reference": b"ref"}
        streamer.media_session = lambda dc_id: _async(FakeSession(blob))
        out = b"".join(asyncio.run(self._collect(streamer, props, start, end)))
        return out

    async def _collect(self, streamer, props, start, end):
        result = []
        async for chunk in streamer.yield_range(props, start, end):
            result.append(chunk)
        return result

    def test_exact_bytes(self):
        blob = bytes(range(256)) * (1024 * 9)  # 2.25MB
        for start, end in ((0, 99), (0, 1048575), (5, 1048576),
                           (1048576, 2097151), (100, 2355199)):
            got = self.run_yield(blob, start, end)
            self.assertEqual(got, blob[start:end + 1], f"{start}-{end}")
            self.assertEqual(len(got), end - start + 1)


def _async(value):
    async def _coro():
        return value
    return _coro()


class CdnYieldTest(unittest.TestCase):
    """A FileCdnRedirect must switch to a CDN session, decrypt and
    hash-verify instead of yielding nothing (the 206-with-0-bytes bug)."""

    def test_cdn_redirect_decrypts(self):
        import os
        from hashlib import sha256

        from pyrogram import raw
        from pyrogram.crypto import aes

        blob = bytes(range(256)) * (1024 * 9)  # 2.25MB
        key, iv = os.urandom(32), os.urandom(16)
        redirect = raw.types.upload.FileCdnRedirect(
            dc_id=4, file_token=b"tok", encryption_key=key,
            encryption_iv=iv, file_hashes=[])

        class MainSession:
            async def send(self, req):
                return redirect

            async def invoke(self, req):
                chunk = blob[req.offset:req.offset + 1024 * 1024]
                return [raw.types.FileHash(
                    offset=req.offset, limit=len(chunk),
                    hash=sha256(chunk).digest())]

        class CdnSession:
            async def invoke(self, req):
                chunk = blob[req.offset:req.offset + 1024 * 1024]
                enc = aes.ctr256_encrypt(
                    chunk, key,
                    bytearray(iv[:-4]
                              + (req.offset // 16).to_bytes(4, "big")))
                return raw.types.upload.CdnFile(bytes=enc)

            async def stop(self):
                pass

        streamer = ByteStreamer(client=None)
        props = {"chat_id": 1, "msg_id": 2, "dc_id": 1, "media_id": 7,
                 "access_hash": 8, "file_reference": b"ref"}
        streamer.media_session = lambda dc_id: _async(MainSession())
        streamer._cdn_session = lambda dc_id: _async(CdnSession())

        async def collect():
            out = []
            async for chunk in streamer.yield_range(props, 0, len(blob) - 1):
                out.append(chunk)
            return out

        self.assertEqual(b"".join(asyncio.run(collect())), blob)


if __name__ == "__main__":
    unittest.main()
