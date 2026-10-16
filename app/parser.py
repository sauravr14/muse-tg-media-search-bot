"""Filename parsing and normalization.

Turns messy release filenames into structured metadata:
    "Breaking.Bad.S01E02.1080p.WEB-DL.x265.mkv"
      -> title="Breaking Bad", year=None, kind=series,
         season=1, episode=2, resolution="1080p", ...

No network calls here; pure string work, fully unit-tested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RESOLUTIONS = ("2160p", "1080p", "720p", "480p", "360p")
RES_PATTERNS = [
    (re.compile(r"\b2160p\b", re.I), "2160p"),
    (re.compile(r"\b4k\b|\bUHD\b", re.I), "2160p"),
    (re.compile(r"\b1080p\b", re.I), "1080p"),
    (re.compile(r"\b720p\b", re.I), "720p"),
    (re.compile(r"\b480p\b", re.I), "480p"),
    (re.compile(r"\b360p\b", re.I), "360p"),
]
YEAR_RE = re.compile(r"\b(19\d{2}|20[0-3]\d)\b")
CODEC_PATTERNS = [
    (re.compile(r"\bx265\b|\bHEVC\b|\bh\.?265\b|\bH\s*265\b", re.I), "x265"),
    (re.compile(r"\bx264\b|\bAVC\b|\bh\.?264\b|\bH\s*264\b", re.I), "x264"),
    (re.compile(r"\bXviD\b", re.I), "XviD"),
]
SOURCE_PATTERNS = [
    (re.compile(r"WEB[\s.-]?DL|WEBRip", re.I), "WEB-DL"),
    (re.compile(r"Blu[\s.-]?Ray|BRRip|BDRip", re.I), "BluRay"),
    (re.compile(r"HDRip", re.I), "HDRip"),
    (re.compile(r"HDTV", re.I), "HDTV"),
    (re.compile(r"DVDScr|DVDRip", re.I), "DVD"),
    (re.compile(r"CAM|HDCAM|TS|TeleSync", re.I), "CAM"),
]
# theatre prints — flagged, filtered by the "clean prints" toggle
CAM_PATTERNS = [
    re.compile(r"\bHDCAM\b", re.I),
    re.compile(r"\bCAMRIP\b", re.I),
    re.compile(r"\bCAM\b", re.I),
    re.compile(r"\bHDTS\b", re.I),
    re.compile(r"\bTELESYNC\b", re.I),
    re.compile(r"\bTSRIP\b", re.I),
    re.compile(r"\bPREDVD\b", re.I),
    re.compile(r"\bHDTC\b", re.I),
    re.compile(r"(?<![A-Za-z])TS(?![A-Za-z])"),
    re.compile(r"(?<![A-Za-z])TC(?![A-Za-z])"),
]
# S01E02, S1E2, s01e02
SE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
# season-pack markers: "S01 COMBINED", "Season 1 Complete", "[E01-04]"
PACK_RE = re.compile(r"\b(COMBINED|COMPLETE)\b", re.I)
PACK_RANGE_RE = re.compile(r"[\(\[\{]\s*[Ee](\d{1,3})\s*[-–_\s]\s*(?:[Ee])?(\d{1,3})\s*[\)\]\}]")
SEASON_NUM_RE = re.compile(r"\b[Ss](\d{1,2})\b")
SEASON_WORD_RE = re.compile(r"\b[Ss]eason\s+(\d{1,2})\b", re.I)


def _detect_pack(name: str, p: "ParsedName", pack_range: tuple | None = None) -> str:
    """Detect season-pack filenames ("S01 COMBINED", "[E01-04]").

    Sets p.kind/season/is_pack/pack_label. Returns the name with pack
    tokens removed. A bare "Complete" with no season/range evidence
    ("Complete Unknown") is not a pack. pack_range is read from the raw
    filename before "-" is flattened."""
    if not PACK_RE.search(name):
        return name
    sm = SEASON_NUM_RE.search(name) or SEASON_WORD_RE.search(name)
    if not sm and not pack_range:
        return name
    p.kind = "series"
    p.is_pack = True
    if sm:
        p.season = int(sm.group(1))
        name = name[: sm.start()] + " " + name[sm.end():]
    if pack_range:
        a, b = pack_range
        p.pack_label = f"E{a:02d}\u2013E{b:02d}"
    else:
        p.pack_label = "Complete"
    return PACK_RE.sub(" ", name)
# 1x02
X_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
# Season 1 Episode 2 / Season.1.Episode.2
SEASON_EP_WORDS_RE = re.compile(
    r"[Ss]eason[\s._-]*(\d{1,2})[\s._-]*[Ee]pisode[\s._-]*(\d{1,3})"
)
# trailing E02 (episode-only, e.g. "Show Name E02 720p")
EP_ONLY_RE = re.compile(r"[Ee](\d{1,3})\b")

RELEASE_GROUPS = re.compile(
    r"-(YTS|YIFY|RARBG|ETRG|PSA|GalaxyRG|TGx|ION10|NTb|SPARKS|FGT|HDT|EbP|PAHE|"
    r"inFaMoUs|DEFLATE|MEMENTO|W4NK3R|SMB|T0M|CtrlHD|TERMiNAL|EPSiLON)$",
    re.I,
)

# Release tags that are never part of the display title
TAG_STRIP_RE = re.compile(
    r"\b(REMASTERED|REMASTER|PROPER|REPACK|RERIP|UNRATED|EXTENDED|"
    r"DIRECTORS?[.\s_-]*CUT|THEATRICAL|IMAX|"
    r"10BIT|8BIT|12BIT|HDR10PLUS|HDR10\+?|HDR|DV|SDR|"
    r"NF|AMZN|DSNP|HULU|ATVP|HBO|PCOK|CRIT|"
    r"\bHQ\b|\bHD\b|"
    r"HEVX|JHS|"
    r"\b\d+\s*CH\b|"
    r"\bPSAOyeSale\b|\bHarsh\b|\bPSA\b|"
    r"AAC|AC3|EAC3|DTS(?:-HD)?|DDP?\s*2\s*0|DDP?\s*5\s*1|\b5\s*1\b|\b2\s*0\b|ATMOS|TRUEHD|"
    r"H\s*26[45]\b|"
    r"ESUBS?|SUBS|SUBBED|DUBBED|DUAL|MULTI|"
    r"MP4|MKV|AVI|M4V|WMV|FLV|WEBM|"
    r"CIMA4U|MYCIMA(?:\s*(?:TV|ME|TO|WORLD))?|EGYBEST|EGYDEAD(?:\s*COM)?|"
    r"AKWAM|FASELHD?|ALIRAQI|JOPULGA|OSN|SHAHID|"
    r"FULL[\s_-]*MOVIE|"
    r"MOVIESMOD|PAHE)\b",
    re.I,
)
# UUIDs and hash-like tokens are never part of a title
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
# long alphanumeric tokens with 3+ digits: hashes, not titles ("Nz87Gp0H5Au").
# Real titles with digits ("21JumpStreet", "Se7en") have <3 digits.
HASHY_RE = re.compile(r"\b(?=[A-Za-z0-9]{10,})(?:[A-Za-z]*\d){3,}[A-Za-z0-9]*\b")


@dataclass
class ParsedName:
    raw: str
    title: str = ""
    norm_title: str = ""
    year: int | None = None
    kind: str = "movie"  # movie | series
    season: int | None = None
    episode: int | None = None
    resolution: str = ""
    quality: str = ""
    codec: str = ""
    is_pack: bool = False  # season pack (episode is None)
    pack_label: str = ""  # "E01–E04" | "Complete"
    is_cam: bool = False  # theatre print (HDCAM/CAM/HDTS/PreDVD/...)
    extra: dict = field(default_factory=dict)


def _strip_ext(name: str) -> str:
    return re.sub(r"\.(mkv|mp4|avi|mov|m4v|wmv|flv|webm|ts|zip|rar|pdf|epub|srt)$", "", name, flags=re.I)


def normalize_title(title: str) -> str:
    """Lowercase alphanumeric key used for matching."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_filename(raw: str) -> ParsedName:
    p = ParsedName(raw=raw)
    name = _strip_ext(raw.strip())
    # UUIDs must go before separators are flattened ("a-b-c" -> "a b c")
    name = UUID_RE.sub(" ", name)
    # pack episode range like [E01-04]: read before "-" is flattened
    rm = PACK_RANGE_RE.search(name)
    pack_range = (int(rm.group(1)), int(rm.group(2))) if rm else None
    # flatten separators FIRST so \b-anchored patterns work on
    # underscore/dot filenames ("Lanterns_2026_S01E01_720p")
    name = re.sub(r"[._\-+]+", " ", name)

    # season packs ("S01 COMBINED", "[E01-04]") before series markers
    name = _detect_pack(name, p, pack_range)

    for rx, val in RES_PATTERNS:
        if rx.search(name):
            p.resolution = val
            break
    for rx, val in CODEC_PATTERNS:
        if rx.search(name):
            p.codec = val
            break
    for rx, val in SOURCE_PATTERNS:
        if rx.search(name):
            p.quality = val
            break
    for rx in CAM_PATTERNS:
        if rx.search(name):
            p.is_cam = True
            break

    # series markers (checked before year so S01E02 isn't mangled)
    if not p.is_pack:
        m = SE_RE.search(name) or X_RE.search(name) or SEASON_EP_WORDS_RE.search(name)
        if m:
            p.kind = "series"
            p.season = int(m.group(1))
            p.episode = int(m.group(2))
            name = name[: m.start()] + " " + name[m.end():]

    years = list(YEAR_RE.finditer(name))
    if years:
        # Prefer the LAST year-like token: the release year normally follows
        # the title ("2012.2009.1080p" -> title "2012", year 2009).
        m = years[-1]
        p.year = int(m.group(1))
        name = name[: m.start()] + " " + name[m.end():]

    # episode-only marker after year removal ("Show E02")
    if p.kind == "movie" and not p.is_pack:
        m = EP_ONLY_RE.search(name)
        if m and not re.search(r"\bseason\b", name, re.I):
            p.kind = "series"
            p.season = 1
            p.episode = int(m.group(1))
            name = name[: m.start()] + " " + name[m.end():]

    # drop anything in brackets/parens/braces (release info, not title)
    name = re.sub(r"[\(\[].*?[\)\]]", " ", name)
    name = re.sub(r"\{.*?\}", " ", name)
    # also a trailing unclosed bracket: "Title Hq [Tam Tel"
    name = re.sub(r"\s*[\(\[][^\)\]]*$", " ", name)
    # leading site database ids: "1354295 Outlander 2008"
    name = re.sub(r"^\d{6,}\s+", " ", name)
    # drop leftover quality/codec/source/cam tokens
    for rx, _ in RES_PATTERNS + CODEC_PATTERNS + SOURCE_PATTERNS:
        name = rx.sub(" ", name)
    for rx in CAM_PATTERNS:
        name = rx.sub(" ", name)
    name = TAG_STRIP_RE.sub(" ", name)
    name = HEX_RE.sub(" ", name)
    name = HASHY_RE.sub(" ", name)
    if p.year:
        # a year was found: trailing bare numbers are usually junk
        # ("Film 1992 ... 247"), but a single 1-9 can be the sequel number
        # ("Meatballs 2") and a leading number the title ("21 Jump Street")
        m = re.search(r"((?:\s*\b\d{1,4}\b)+)\s*$", name)
        if m:
            toks = m.group(1).split()
            if any(len(t) >= 2 or t == "0" or len(toks) > 1 for t in toks):
                name = name[: m.start()].strip()
    # trailing site-tag separator junk: "The Furious ... MoviesMod.At"
    # (a leading "At" can be legit: "At Close Range" — only strip trailing)
    name = re.sub(r"\s+\b[Aa][Tt]\b\s*$", " ", name)
    # truncated tag letter: "... 10bit N" (was "NF")
    name = re.sub(r"\s+[A-Z]\s*$", " ", name)
    name = re.sub(r"\b(Season|Episode|Ep|Dual|Audio|Hindi|English|Engli|Eng|"
                r"Tam|Tel|Mal|Kan|Multi|ESub)\b", " ", name, flags=re.I)
    name = RELEASE_GROUPS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" -")

    p.title = name.strip().title() if name.strip() else _strip_ext(raw).strip()
    p.norm_title = normalize_title(p.title)
    return p
