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
    (re.compile(r"\bx265\b|\bHEVC\b|\bh\.?265\b", re.I), "x265"),
    (re.compile(r"\bx264\b|\bAVC\b|\bh\.?264\b", re.I), "x264"),
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
# S01E02, S1E2, s01e02
SE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
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
    r"10BIT|8BIT|12BIT|HDR10\+?|HDR|DV|SDR|"
    r"NF|AMZN|DSNP|HULU|ATVP|HBO|PCOK|CRIT|"
    r"\bHQ\b|\bHD\b|"
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

    # series markers (checked before year so S01E02 isn't mangled)
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
    if p.kind == "movie":
        m = EP_ONLY_RE.search(name)
        if m and not re.search(r"\bseason\b", name, re.I):
            p.kind = "series"
            p.season = 1
            p.episode = int(m.group(1))
            name = name[: m.start()] + " " + name[m.end():]

    # drop anything in brackets/parens (release info, not title)
    name = re.sub(r"[\(\[].*?[\)\]]", " ", name)
    # also a trailing unclosed bracket: "Title Hq [Tam Tel"
    name = re.sub(r"\s*[\(\[][^\)\]]*$", " ", name)
    # leading site database ids: "1354295 Outlander 2008"
    name = re.sub(r"^\d{6,}\s+", " ", name)
    # drop leftover quality/codec/source tokens
    for rx, _ in RES_PATTERNS + CODEC_PATTERNS + SOURCE_PATTERNS:
        name = rx.sub(" ", name)
    name = UUID_RE.sub(" ", name)
    name = TAG_STRIP_RE.sub(" ", name)  # before flatten: catches DDP5.1, 5.1, H.265
    name = re.sub(r"[._\-+]+", " ", name)
    # release tags, then hash-like leftovers
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
    name = re.sub(r"\b(Season|Episode|Ep|Complete|Dual|Audio|Hindi|English|Engli|Eng|"
                r"Tam|Tel|Mal|Kan|Multi|ESub)\b", " ", name, flags=re.I)
    name = RELEASE_GROUPS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" -")

    p.title = name.strip().title() if name.strip() else _strip_ext(raw).strip()
    p.norm_title = normalize_title(p.title)
    return p
