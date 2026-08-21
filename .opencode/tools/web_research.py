#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["scrapling[fetchers]", "ddgs", "trafilatura", "rank-bm25", "httpx"]
# ///
# -*- coding: utf-8 -*-
"""
Web Research Tool - Autonomous Search + Fetch + Report

Unified tool combining search and fetch into a single optimized workflow:
1. Search via DuckDuckGo + Brave (fallback) for maximum coverage
2. Filter and deduplicate URLs during search (early filtering)
3. Fetch content in parallel via Scrapling (TLS fingerprinting, anti-bot bypass)
4. Scrapling text extraction fallback for "Too short" pages
5. Search mode: full filtered text is written to a report file in
   tmp/webresearch/ (run-id.txt); stdout carries a compact technical
   index (FULL REPORT path first + stats + one line per page with
   @line/@hit locators) — the SAME index heads the report file itself,
   so a lost stdout digest is recoverable from the file. The model
   researches from the report file (read/grep by @line), not from stdout.
6. --url direct fetch: ONE URL per invocation. The full page is fetched
   (no output char cap; HTML extraction bounded by MAX_CONTENT_BYTES),
   quality-filtered, and saved to its own report file in
   tmp/webresearch/ (run-id.txt); stdout prints ONLY the absolute path.
   JS pages are rendered with a headless Chromium shell
   (chromium-headless-shell; official Google build on macOS/Windows,
   bundled-libs build on Linux; uv-managed, user-cache only,
   headless/background):
   auto by default (only when static fetch fails), --no-render to disable; the browser is auto-fetched on first use
   (one-time, ~100-110MB).
7. Search mode is static-only by design (benchmarked: browser escalation in
   search cost +44% fetch time and rescued ~0-2 pages — the headless shell's
   value is --url single-page JS rendering, not bulk search fetching). The
   browser machinery (probe/install/escalation) is used ONLY by --url mode.

Usage:
    python web_research.py "search query"
    python web_research.py "query" --sci | --med | --tech   # Domain bonus sources (arXiv, PubMed, HN/Stack Overflow/GitHub)
    python web_research.py --url https://example.com   # Save one URL's full page text to a report file (path printed)

(fixed tuned settings: search 30 results, fetch up to 20 pages, plain-text output)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import random
import re
import shutil
import string
import subprocess
import sys
import time
import types
import urllib.parse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from io import StringIO
from pathlib import Path
from typing import (
    AsyncIterator,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
)

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

# Suppress ALL library logging before any imports touch the root logger.
# Scrapling uses logging.info() (root logger) and named loggers — silence both.
logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)
logging.getLogger().setLevel(logging.CRITICAL)
for _lib in ("scrapling", "curl_cffi", "httpx", "hpack", "httpcore", "asyncio"):
    logging.getLogger(_lib).setLevel(logging.CRITICAL)

# Our own logger — restored to WARNING after imports
logger = logging.getLogger("web_research")
logger.setLevel(logging.WARNING)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(_handler)
logger.propagate = False

# =============================================================================
# CONSTANTS
# =============================================================================

# Fetch timeout for single-page fetches (seconds)
DEFAULT_TIMEOUT: int = 5

# Whole-run wall-clock timeout (seconds). Bounds the entire research run on ALL
# platforms: search mode wraps the async core in asyncio.wait_for (Windows has
# no SIGALRM), and the Unix SIGALRM watchdog covers the post-processing phase
# outside the async core. Both mechanisms use this same value.
# Env-overridable for ops/testing (read at import time).
def _wall_timeout_default() -> int:
    """Whole-run wall-clock timeout (seconds); env-overridable, fail-soft."""
    raw = os.environ.get("WEB_RESEARCH_TIMEOUT_SECONDS", "")
    if raw.isdigit():
        return int(raw)
    return 300


WALL_TIMEOUT: int = _wall_timeout_default()

BLOCKED_DOMAINS: Tuple[str, ...] = (
    "facebook.com", "tiktok.com", "instagram.com", "linkedin.com", "youtube.com",
    "msn.com",  # redirects to stub/privacy pages, no usable content
    # Consistently HTTP 403 — wasted fetch slots
    "forbes.com", "edmunds.com", "cars.com", "nytimes.com",
    "percona.com", "mctlaw.com", "zenodo.org", "amjmed.com", "dl.acm.org",
    "nejm.org", "cell.com", "sciencedirect.com", "onlinelibrary.wiley.com",
    "reddit.com",  # subreddit homepages are empty title-only stubs — no research value
    # twitter.com, x.com: unblocked — FxTwitter API for tweet text
    # reddit.com: blocked — subreddit pages are empty title-only stubs that give
    # no research benefit; the substring matcher below covers www./old./np./new.
    # and all other subdomains automatically
    # medium.com: unblocked — full articles extract cleanly
)

SKIP_URL_PATTERNS: Tuple[str, ...] = (
    r"\.jpg$", r"\.png$", r"\.gif$", r"\.svg$", r"\.webp$",
    r"/login", r"/signin", r"/signup", r"/cart", r"/checkout",
    r"/tag/", r"/tags/", r"/category/", r"/categories/",
    r"/archive/", r"/page/\d+",
    r"bing\.com/aclick",  # Bing ad redirects — marketing/booking noise
    r"www\.yahoo\.com/",  # EU privacy consent walls, no usable content
    r"finance\.yahoo\.com/",  # EU privacy consent walls
    r"www\.aol\.com/",  # cookie/privacy consent walls
    # tech.yahoo.com: unblocked — returns actual article content
    # .pdf: now handled via pdftotext extraction
)


# CAPTCHA/blocked page detection markers
BLOCKED_CONTENT_MARKERS: Tuple[str, ...] = (
    "verify you are human",
    "access to this page has been denied",
    "please complete the security check",
    "cloudflare ray id:",
    "checking your browser",
    "enable javascript and cookies",
    "unusual traffic from your computer",
    "are you a robot",
    "captcha",
    "perimeterx",
    "distil networks",
    "blocked by",
)

# Brave Search API key: set BRAVE_API_KEY env var, or place key in ~/.config/brave/api_key
BRAVE_API_KEY_PATH = Path(os.environ.get("BRAVE_API_KEY_FILE", str(Path.home() / ".config" / "brave" / "api_key")))

# =============================================================================
# COMPILED REGEX PATTERNS
# =============================================================================

# URL filtering - single combined pattern for performance
_BLOCKED_URL_PATTERN = re.compile(
    r'(?:' + '|'.join(re.escape(d) for d in BLOCKED_DOMAINS) + r')|(?:' + '|'.join(SKIP_URL_PATTERNS) + r')',
    re.IGNORECASE
)

# HTML extraction - simple fast patterns (optimized for speed)
RE_STRIP_TAGS = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript|iframe|svg|form)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
RE_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
RE_JSON_LD = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
RE_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|tr|article|section)>", re.IGNORECASE)
RE_LI = re.compile(r"<li[^>]*>", re.IGNORECASE)
RE_ALL_TAGS = re.compile(r"<[^>]+>")
RE_SPACES = re.compile(r"[ \t]+")
RE_LEADING_SPACE = re.compile(r"\n[ \t]+")
RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
RE_WHITESPACE = re.compile(r"\s+")
# Sentence boundary: period/exclamation/question + space + uppercase letter
# Handles common abbreviations by requiring 2+ chars before the period
RE_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')
# Wikipedia cleanup patterns
RE_WIKI_CITE = re.compile(r'\[\[?\d+\]?\](?:\(#cite_note[^)]*\))?')  # [[20]](#cite_note-22), [21]
RE_WIKI_CITE_NAMED = re.compile(r'\[\[?[a-z]\]?\](?:\(#cite_note[^)]*\))?')  # [[b]](#cite_note-b-13)
RE_WIKI_LINK = re.compile(r'\[([^\]]+)\]\(/wiki/[^)]+\)')  # [Battle](/wiki/Battle) -> Battle
RE_WIKI_REFLIST = re.compile(r'\n(?:\*\s*)?(?:\[?\d+\]?\s*)?(?:\^.*)?(?:ISBN|ISSN|doi:|JSTOR|S2CID|OCLC).*', re.IGNORECASE)
# Forum noise: lines that are pure metadata (likes, timestamps, user roles)
RE_FORUM_NOISE = re.compile(
    r'^\s*(?:'
    r'\d+\s+Likes?\b'               # "1 Like", "2 Likes"
    r'|Like\s*$'                     # standalone "Like"
    r'|\d+\s*(?:yr|mo|hr|min|sec)s?\s+ago\b'  # "2 yr ago"
    r'|(?:Community\s+Expert|Author|Moderator|Admin)\s*$'  # user roles
    r'|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\d*\s*(?:yr|mo)?\s*$'  # "March 19, 20232 yr"
    r'|\d{1,2}\s+(?:hours?|minutes?|days?|weeks?|months?|years?)\s+ago'  # "8 hours ago"
    r'|said:\s*$'                    # "X said:"
    r'|Quote\s*$'                    # standalone "Quote"
    r'|Share\s*$'                    # standalone "Share"
    r'|Reply\s*$'                    # standalone "Reply"
    r'|Report\s*$'                   # standalone "Report"
    r')',
    re.MULTILINE | re.IGNORECASE,
)

# Domains where curl_cffi c-ares DNS resolver fails (Windows).
# Populated at runtime; domains in this set skip straight to httpx fallback.
_CURL_DNS_FAIL_DOMAINS: set = set()

# External tool availability (checked once at import)
PDFTOTEXT_PATH = shutil.which("pdftotext")

# ---------------------------------------------------------------------------
# Unified browser backend: chromium-headless-shell on ALL platforms
# (uv-managed, headless, background only; binaries in user cache dirs):
#   - macOS / Windows: official Google build (self-contained natively),
#     downloaded via the Chrome-for-Testing last-known-good JSON.
#   - Linux (incl. UI-less servers): Aletherium bundled-libs build (browser +
#     NSS/NSPR/expat libs) — the only way to run on clean hosts with no root,
#     no apt, no system modification (loaded via LD_LIBRARY_PATH from
#     user-writable dirs). Download ~107MB per arch, into the same cache dir.
# ---------------------------------------------------------------------------
_SYSTEM: str = platform.system().lower()          # "darwin" | "linux" | "windows"
_IS_WINDOWS: bool = _SYSTEM == "windows"
_IS_MACOS: bool = _SYSTEM == "darwin"
_IS_LINUX: bool = _SYSTEM == "linux"


def _shell_cache_root() -> Path:
    """Cache root per platform (user dirs only):
    macOS ~/Library/Caches, Linux $XDG_CACHE_HOME|~/.cache, Windows
    %LOCALAPPDATA% — always + /webresearch/headless-shell."""
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif _IS_MACOS:
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return Path(base) / "webresearch" / "headless-shell"


_SHELL_CACHE_ROOT: Path = _shell_cache_root()


def _shell_cft_platform() -> str:
    """Chrome-for-Testing platform id (JSON `platform` key):
    mac-arm64/mac-x64/win32/win64/linux64 from the machine."""
    machine = platform.machine().lower()
    if _IS_WINDOWS:
        return "win64" if machine in ("amd64", "x86_64") else "win32"
    if _IS_MACOS:
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    return "linux64"


_SHELL_CFT_PLATFORM: str = _shell_cft_platform()

# Linux arch id for the Aletherium release archives: amd64 | arm64
_SHELL_ARCH: str = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
# Chromium version pinned by the upstream release; update together with the URL.
_SHELL_RELEASE: str = "chromedp-148.0.7778.97"
_SHELL_BROWSER_URL: str = (
    "https://github.com/Aletherium/chromium-headless-shell/releases/download/"
    f"{_SHELL_RELEASE}/chromium-headless-shell-linux-{_SHELL_ARCH}.tar.gz"
)
_SHELL_LIBS_URL: str = (
    "https://github.com/Aletherium/chromium-headless-shell/releases/download/"
    f"{_SHELL_RELEASE}/chromium-headless-shell-libs-linux-{_SHELL_ARCH}.tar.gz"
)
_SHELL_BROWSER_SHA_URL: str = _SHELL_BROWSER_URL + ".sha256"
_SHELL_LIBS_SHA_URL: str = _SHELL_LIBS_URL + ".sha256"

if _IS_LINUX:
    # Linux shell cache layout: <cache>/webresearch/headless-shell/{browser,libs}
    _SHELL_BROWSER_DIR: Path = _SHELL_CACHE_ROOT / "browser"
    _SHELL_LIBS_DIR: Path = _SHELL_CACHE_ROOT / "libs"
    _SHELL_EXE: Path = _SHELL_BROWSER_DIR / "headless-shell"
else:
    # mac/win layout: <root>/<version>/chrome-headless-shell-<platform>/
    # chrome-headless-shell(.exe). Version = Stable channel version from the
    # Chrome-for-Testing JSON at install time; pinned fallback below is used
    # when the JSON cannot be fetched, and _SHELL_EXE is updated to the
    # JSON-resolved version after a fresh install.
    _SHELL_VERSION_FALLBACK: str = "152.0.7977.42"
    _SHELL_EXE_NAME: str = "chrome-headless-shell.exe" if _IS_WINDOWS else "chrome-headless-shell"
    _SHELL_BROWSER_DIR: Optional[Path] = None
    _SHELL_LIBS_DIR: Optional[Path] = None

    def _resolve_shell_exe() -> Path:
        """Newest already-installed version in the cache, else the pinned
        fallback path. The installer updates _SHELL_EXE after a fresh
        download, so a later run must not re-download a JSON-resolved newer
        version: scanning the cache at import time keeps the path stable."""
        try:
            if _SHELL_CACHE_ROOT.is_dir():
                installed = [
                    p / f"chrome-headless-shell-{_SHELL_CFT_PLATFORM}" / _SHELL_EXE_NAME
                    for p in _SHELL_CACHE_ROOT.iterdir()
                    if p.is_dir()
                ]
                installed = [p for p in installed if p.is_file()]
                if installed:
                    installed.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    return installed[0]
        except Exception:
            pass
        return (
            _SHELL_CACHE_ROOT / _SHELL_VERSION_FALLBACK
            / f"chrome-headless-shell-{_SHELL_CFT_PLATFORM}" / _SHELL_EXE_NAME
        )

    _SHELL_EXE: Path = _resolve_shell_exe()

# Browser rendering availability. Cached after the first check so the preflight
# fetch runs at most once per run.
_BROWSER_AVAILABLE: bool = False
_BROWSER_CHECKED: bool = False

# Cap concurrent browser fetches (safety for future parallel use; single-URL now)
_BROWSER_SEMAPHORE_ASYNC = asyncio.Semaphore(2)

# Errors worth escalating to the browser in "auto" mode. A browser cannot fix a
# clean 404 or a DNS failure — those stay static-only (mirrors the old ladder's
# BRIGHTDATA_RETRY_ERRORS). "Too short" is retry-worthy: it often means the
# static fetch got a JS shell the browser would render.
BROWSER_RETRY_ERRORS: frozenset = frozenset({
    "HTTP 403", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503",
    "CAPTCHA/blocked", "Timeout", "Too short",
})

# Per-run browser-escalation counter (search mode only; reset at run start).
_BROWSER_ESCALATIONS: int = 0

# =============================================================================
# SMART CONTENT FILTERS (tunable)
# =============================================================================
# These constants tune the four content filters applied during compression:
#   F1 boilerplate filter, F2 fact-density boost, F3 cross-page dedup, F4 sections.
# Values were tuned against the real-search corpus (tmp/bm25-stats/corpus.jsonl).

# F1 - boilerplate sentence patterns. Each pattern matches a junk sentence
# (cookie/privacy banners, subscribe/share invites, copyright, nav fragments,
# date-only lines). Tuned conservatively: only unambiguous junk phrases.
BOILERPLATE_PATTERNS: Tuple[re.Pattern, ...] = (
    # Cookie / privacy / consent banners
    re.compile(
        r"\b(?:we use cookies?|accept(?: all)? cookies?|manage cookies?|"
        r"cookie (?:policy|settings|notice|preferences?|consent)|"
        r"privacy (?:policy|notice)|gdpr)\b",
        re.IGNORECASE,
    ),
    # Subscribe / signup / newsletter invites
    re.compile(
        r"\b(?:subscribe(?: to| now| for)?|sign\s?up(?: for| to| now)?|newsletter|"
        r"join our (?:newsletter|mailing list|community)|get the latest|"
        r"delivered to your inbox)\b",
        re.IGNORECASE,
    ),
    # Follow / share / social invites
    re.compile(
        r"\b(?:follow us(?: on)?|follow @|share (?:this|on|via|the)|tweet this|"
        r"pin it|like us on|add us on|connect with us)\b",
        re.IGNORECASE,
    ),
    # Copyright / rights lines
    re.compile(r"\b(?:copyright|\u00a9|\(c\)|all rights reserved|rights reserved)\b", re.IGNORECASE),
    # Download / app / CTA junk
    re.compile(
        r"\b(?:download (?:our |the )?(?:app|application)|get (?:the )?app|"
        r"available on (?:the )?(?:app store|google play)|read more|continue reading|"
        r"click here|learn more|view all|see all|watch now|shop now|buy now|"
        r"sign in|log in|register (?:now|free)?|create (?:an )?account)\b",
        re.IGNORECASE,
    ),
    # Navigation / breadcrumb / related-content junk
    re.compile(
        r"\b(?:you are here|back to top|back to (?:the )?(?:home|article|top)|"
        r"next (?:article|page|story)|previous (?:article|page|story)|"
        r"related (?:posts|articles|stories)|recommended for you|you might also like|"
        r"more from|top stories|trending now|most (?:read|popular)|"
        r"popular (?:posts|articles|news)|latest (?:news|articles|posts)|"
        r"table of contents|search results)\b",
        re.IGNORECASE,
    ),
    # Date-only lines
    re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$"),
    re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$"),
    re.compile(r"^[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4},?\s*(?:\d{1,2}:\d{2})?\s*(?:AM|PM)?$", re.IGNORECASE),
    re.compile(r"^\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}$", re.IGNORECASE),
)

# F2 - fact-density boost: score = blend * (1 + alpha * fact_density)
FACT_DENSITY_ALPHA: float = 0.4

# Common capitalized words that are NOT entity-like (F2 fact token exceptions)
_FACT_CAP_EXCEPTIONS = frozenset(
    "the this that these those it its we you they i my me our us your their his her he she "
    "and but or not of in on at to from with for as by than then so there here when where why "
    "how what which who whom while until during between about after before because although "
    "however therefore also more most some such any all both each few other another one two "
    "first second next last recent new many much same only just very too".split()
)

# F4 - heading-aware section selection: drop body sections whose heading matches
# fewer than this fraction of query content words. 0.0 = drop only zero-overlap.
HEADING_MATCH_THRESHOLD: float = 0.0

# F3 - cross-page near-duplicate sentence dedup (batch path)
CROSS_PAGE_JACCARD_THRESHOLD: float = 0.85   # Jaccard above this = near-dup; 0.0 = exact only
CROSS_PAGE_DEDUP_MIN_LEN: int = 30           # only dedup lines at least this long

# Minimum scoring pool size after F4/F1 filtering. Below this the BM25 idf
# collapses (terms in 1 of N docs score ~0 with small N) and the page would
# fall back to full-length output — so filtering gives up instead.
MIN_SCORING_POOL: int = 15

# =============================================================================
# DIGEST + REPORT FILE (search-path output)
# =============================================================================
# Search mode writes the full filtered text to a report file under
# <REPO_ROOT>/tmp/webresearch/ and prints a compact digest to stdout; the
# model researches from the file (read/grep), not from the inline output.

REPORT_DIR_NAME: str = "webresearch"        # subdirectory of <REPO_ROOT>/tmp/
REPORT_PAGE_CEILING: int = 200_000          # per-page text ceiling in the file (memory sanity; digest is a line-per-page index, see _build_digest)
REPORT_MAX_AGE_DAYS: int = 7                # rotation: delete files older than this
REPORT_MAX_FILES: int = 30                  # rotation: keep only the N newest

# F4 - section-level junk headings. Whole sections under these headings are
# dropped when their heading shares no query tokens (see _filter_sections_by_heading).
# Deliberately conservative: generic CONTENT headings (Abstract, Introduction,
# Results, Methods, Ingredients, Instructions, FAQ, Pros/Cons, ...) are NOT listed —
# real pages use creative headings that legitimately lack query words, and the
# naive token-overlap filter was observed to delete real content on the corpus.
SECTION_DROP_HEADING_PATTERNS: Tuple[re.Pattern, ...] = (
    # Comments / discussion
    re.compile(r"\b(?:comments?|commentary|join the discussion|leave a (?:comment|reply)|responses?|replies?)\b", re.IGNORECASE),
    # Related / recommended / popular / latest / trending
    re.compile(
        r"\b(?:related (?:articles?|posts?|stories|reading)|you might also like|recommended (?:for you|reading)|"
        r"more (?:from|to read)|popular (?:posts?|articles?|now)|trending(?: now)?|most (?:read|popular)|"
        r"latest (?:news|articles?|posts?)|top (?:stories|posts?|articles?)|further reading|external links)\b",
        re.IGNORECASE,
    ),
    # Nav / breadcrumb / TOC
    re.compile(r"\b(?:table of contents|contents|search(?: results)?|site ?map|you are here|back to (?:top|home)|breadcrumbs?|next (?:article|post|page)|previous (?:article|post|page))\b", re.IGNORECASE),
    # Author / about / contact boxes
    re.compile(r"\b(?:about (?:the )?author|author bio|written by|byline|about us|contact us|our team|our writers)\b", re.IGNORECASE),
    # Newsletter / subscribe / social
    re.compile(r"\b(?:newsletter|subscribe|sign\s?up|join our (?:newsletter|mailing list)|share (?:this|the (?:article|post))|follow us|social (?:media|sharing))\b", re.IGNORECASE),
    # Academic nav blocks (PubMed/arXiv/EuropePMC chrome)
    re.compile(
        r"\b(?:references|citations?|bibliography|works cited|footnotes?|endnotes?|linkout|full text sources|"
        r"mesh terms|publication types|medical\s*$|miscellaneous|conflict of interest|grants? and funding|"
        r"submission history|current browse context|other literature sources|author information|"
        r"similar articles|related information|figures|supplementary)\b",
        re.IGNORECASE,
    ),
    # Ads / sponsorship / legal
    re.compile(
        r"\b(?:advertisements?|sponsored|paid (?:promotion|post)|affiliate (?:links?|disclosure)|"
        r"footer|cookie (?:policy|settings|notice)|privacy (?:policy|notice)|terms (?:of (?:use|service))?|"
        r"legal|disclaimer)\b",
        re.IGNORECASE,
    ),
    # Tag / category / archive lists
    re.compile(r"\b(?:categories?|tags?|archives?)\b", re.IGNORECASE),
)

# =============================================================================
# QUALITY FILTERS F5, F7 (tunable)
# =============================================================================
# Two quality filters layered on top of F1-F4, tuned against the same corpus
# (tmp/bm25-stats/corpus.jsonl) and the motivating junk reports
# (tmp/webresearch/20260814-*deepseek-harness*). All fail-soft: on any error the
# filter does nothing and the pipeline keeps the unfiltered page set.

# F5 - cross-domain syndication / content-farm detection (batch path, page level)
FARM_PAGE_JACCARD_THRESHOLD: float = 0.85   # page-level shingle Jaccard = near-dup
FARM_GROUP_MIN_PAGES: int = 3               # group must have this many pages
FARM_GROUP_MIN_DOMAINS: int = 3             # ... across this many distinct registrable domains
FARM_MIN_SIGNALS: int = 2                   # signals required to drop redundant copies
FARM_HARD_SIGNALS: int = 3                  # signals required to drop the whole group (all copies)
FARM_SIG_CHARS: int = 4000                  # chars of normalized body used for the page signature

# F5 byline/author patterns: same author repeating across unrelated domains
FARM_BYLINE_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bAbout\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", re.IGNORECASE),
    re.compile(r"\bBy\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", re.IGNORECASE),
    re.compile(r"\bBy\s+(?:Dr|Prof|Mr|Ms|Mrs|Miss|Sir|Dame|Fr|Rev)\.?\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", re.IGNORECASE),
    re.compile(r"\bwritten\s+by\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", re.IGNORECASE),
    re.compile(r"\bI'?m\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s*[—\-–]", re.IGNORECASE),
    re.compile(r"\bAuthor\s*:\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", re.IGNORECASE),
)

# F5 farm-network boilerplate phrases (maxgrowthagency-style PBN self-promotion)
FARM_NETWORK_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\balso on our network\b", re.IGNORECASE),
    re.compile(r"\bkeep reading\b", re.IGNORECASE),
    re.compile(r"\bmore from our network\b", re.IGNORECASE),
    re.compile(r"\bread more at\b", re.IGNORECASE),
    re.compile(r"\bwant my best\b", re.IGNORECASE),
    re.compile(r"\bget access here\b", re.IGNORECASE),
    re.compile(r"\bget inside\b", re.IGNORECASE),
    re.compile(r"\bget my best\b", re.IGNORECASE),
    re.compile(r"\bjoin my (?:private |free )?(?:group|community|course)\b", re.IGNORECASE),
    re.compile(r"\bbook a free strategy session\b", re.IGNORECASE),
    re.compile(r"\bDA\d+\s*[–—-]\s*\$", re.IGNORECASE),      # DA-tier pricing menus
    re.compile(r"\b(?:free )?AI (?:Course|Community)\b", re.IGNORECASE),
)

# F7 - recency filter (search path)
RECENCY_MAX_AGE_DAYS: int = 3 * 365         # drop pages older than this on recency-sensitive queries
RECENCY_UNDATED_MAX_FRACTION: float = 0.5   # ≥50% undated pages → recency non-applicable (fail-soft)
RECENCY_EVERGREEN_WORDS: Tuple[str, ...] = (
    "history", "origins", "why do", "how to", "explained", "ethics",
    "classic", "famous", "evolution", "what is", "tutorial", "guide",
)
RECENCY_SENSITIVE_WORDS: Tuple[str, ...] = (
    "breaking changes", "new features", "release", "launch", "benchmark",
    "update", "updated", "version", "comparison", "vs", "latest", "news",
    "price", "today", "announce", "2026",
)

# Master toggle: WEB_RESEARCH_QUALITY=0 disables F5/F7 entirely (search mode).
QUALITY_FILTERS_ENABLED: bool = os.environ.get("WEB_RESEARCH_QUALITY", "1") != "0"

# Stub-page rule: a page is a stub when its extracted content is small AND holds
# nothing beyond its own title/heading (forum/social homepages, empty product
# listings). Calibrated on the corpus (tmp/bm25-stats/corpus.jsonl, 389 pages):
# the smallest legit page is 641 chars (body 611), so total < 200 with body < 80
# sits 441 chars below the smallest legit page while catching the observed
# title-only stubs (68-103 chars). The AND keeps real small pages with actual
# body text: a 180-char page with a 150-char body is not a stub.
STUB_MAX_TOTAL_CHARS: int = 200
STUB_MAX_BODY_CHARS: int = 80

# =============================================================================
# REQUIRED DEPENDENCIES (managed by uv)
# =============================================================================

from scrapling.fetchers import AsyncFetcher
from ddgs import DDGS

# Scrapling adds its own StreamHandler at INFO — remove it post-import
_scrapling_logger = logging.getLogger("scrapling")
_scrapling_logger.handlers.clear()
_scrapling_logger.setLevel(logging.CRITICAL)

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ResearchConfig:
    """Configuration for research workflow."""
    query: str
    fetch_count: int = 20
    max_content_length: int = 10000
    timeout: int = DEFAULT_TIMEOUT
    quiet: bool = False
    min_content_length: int = 600
    max_concurrent: int = 50  # Match default search count
    search_results: int = 30
    scientific: bool = False
    medical: bool = False
    tech: bool = False
    escalation_budget: int = 3  # max browser escalations in --url auto mode (search is static-only)


@dataclass
class FetchResult:
    """Single fetch result."""
    url: str
    success: bool
    content: str = ""
    title: str = ""
    error: Optional[str] = None
    source: str = "scrapling"


@dataclass
class ResearchStats:
    """Statistics for research run."""
    query: str = ""
    urls_searched: int = 0
    urls_fetched: int = 0
    urls_filtered: int = 0
    content_chars: int = 0
    bonus_sources: dict = None  # {source_name: count}


def _quality_fields(results: Optional[List[FetchResult]]) -> dict:
    """Extract quality-related fields from fetch results."""
    if not results:
        return {"short_pages": 0, "domains": []}
    return {
        "short_pages": sum(1 for r in results if r.success and len(r.content) < 200),
        "domains": list({urllib.parse.urlparse(r.url).netloc for r in results if r.success}),
    }


def log_usage(event: dict) -> None:
    """Append one JSONL event to ~/.web-research/usage.jsonl."""
    try:
        log_dir = os.path.join(os.path.expanduser("~"), ".web-research")
        os.makedirs(log_dir, exist_ok=True)
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(log_dir, "usage.jsonl"), "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def print_usage_stats(quality: bool = False) -> None:
    """Print usage statistics from ~/.web-research/usage.jsonl."""
    log_path = os.path.join(os.path.expanduser("~"), ".web-research", "usage.jsonl")
    if not os.path.exists(log_path):
        print("No usage data yet", file=sys.stderr)
        sys.exit(0)

    from collections import Counter
    from datetime import datetime, timedelta

    cutoff = datetime.now().astimezone() - timedelta(days=30)
    events = []
    errors: Counter = Counter()
    modes: Counter = Counter()
    days: Counter = Counter()
    domain_ok: Counter = Counter()    # domain → successful fetches

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(ev["ts"])
                if ts < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
            events.append(ev)
            modes[ev.get("mode", "unknown")] += 1
            day = ev["ts"][:10]
            days[day] += 1
            if not ev.get("ok") and ev.get("error"):
                errors[ev["error"]] += 1
            for d in ev.get("domains", []):
                domain_ok[d] += 1

    if not events:
        print("No usage data in last 30 days")
        sys.exit(0)

    total = len(events)
    ok_count = sum(1 for e in events if e.get("ok"))
    avg_ms = sum(e.get("ms", 0) for e in events) / total
    timeouts = sum(1 for e in events if e.get("timeout"))
    avg_fetched = sum(e.get("urls_fetched", 0) for e in events) / total
    avg_chars = sum(e.get("content_chars", 0) for e in events) / total
    total_short = sum(e.get("short_pages", 0) for e in events)
    total_fetched = sum(e.get("urls_fetched", 0) for e in events)
    print(f"Web Research Usage (last 30 days)")
    print(f"{'='*40}")
    print(f"Total searches:    {total}")
    print(f"Success rate:      {ok_count}/{total} ({100*ok_count/total:.0f}%)")
    print(f"Avg latency:       {avg_ms/1000:.1f}s")
    print(f"Timeouts:          {timeouts}")
    print()
    print(f"Mode breakdown:")
    for mode, count in modes.most_common():
        print(f"  {mode:15s} {count:4d} ({100*count/total:.0f}%)")
    print()
    print(f"Fetch efficiency:")
    print(f"  Avg URLs fetched:  {avg_fetched:.1f}")
    print(f"  Avg content chars: {avg_chars:.0f}")

    if quality:
        print()
        print(f"Output quality:")
        print(f"  Short pages (<200 chars): {total_short}/{total_fetched}" +
              (f" ({100*total_short/total_fetched:.0f}%)" if total_fetched else ""))
        print()
        print(f"Top domains (by fetch count):")
        for domain, count in domain_ok.most_common(10):
            print(f"  {count:4d}x {domain}")

    if errors:
        print()
        print(f"Top errors:")
        for err, count in errors.most_common(5):
            print(f"  {count:4d}x {err[:80]}")

    if days:
        print()
        print(f"Busiest days:")
        for day, count in days.most_common(5):
            print(f"  {day}  {count} searches")


# =============================================================================
# PROGRESS REPORTER (Unified)
# =============================================================================

class ProgressReporter:
    """Progress reporting with timing and per-URL diagnostics."""

    def __init__(self, quiet: bool = False, verbose: bool = False):
        self.quiet = quiet
        self.verbose = verbose
        self._last_line_len = 0
        self._phase_start: float = 0
        self._total_start: float = time.monotonic()
        self._ok_count = 0
        self._failures: List[Tuple[str, str, float]] = []  # (url, error, elapsed)

    def message(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def phase_start(self, name: str) -> None:
        self._phase_start = time.monotonic()

    def phase_end(self, name: str) -> None:
        elapsed = time.monotonic() - self._phase_start
        if not self.quiet:
            print(f"  [{name}] {elapsed:.1f}s", file=sys.stderr)

    def url_result(self, url: str, success: bool, elapsed: float, error: str = "") -> None:
        if success:
            self._ok_count += 1
            if self.verbose and not self.quiet:
                domain = urllib.parse.urlparse(url).netloc
                print(f"    OK  {elapsed:4.1f}s  {domain}", file=sys.stderr)
        else:
            self._failures.append((url, error, elapsed))
            if self.verbose and not self.quiet:
                domain = urllib.parse.urlparse(url).netloc
                print(f"    --  {elapsed:4.1f}s  {domain} ({error})", file=sys.stderr)

    def update(self, phase: str, current: int, total: int) -> None:
        if self.quiet or self.verbose:
            return
        elapsed = time.monotonic() - self._phase_start
        line = f"\r    {phase}: {current}/{total} ({self._ok_count} ok, {elapsed:.0f}s)"
        padding = max(0, self._last_line_len - len(line))
        print(f"{line}{' ' * padding}", end="", file=sys.stderr)
        self._last_line_len = len(line)

    def newline(self) -> None:
        if not self.quiet and not self.verbose:
            print(file=sys.stderr)
            self._last_line_len = 0

    def summary(self, fetched_ok: int, total: int, chars: int) -> None:
        if self.quiet:
            return
        total_elapsed = time.monotonic() - self._total_start
        rate = (fetched_ok / total * 100) if total > 0 else 0
        rate_indicator = ""
        if rate < 50:
            rate_indicator = " !! LOW"
        elif rate < 70:
            rate_indicator = " !"
        print(f"  Done: {fetched_ok}/{total} ok ({rate:.0f}%{rate_indicator}) -- {chars:,} chars in {total_elapsed:.1f}s", file=sys.stderr)

        if self._failures:
            by_error: dict[str, int] = {}
            slow: List[Tuple[str, float]] = []
            for url, error, elapsed in self._failures:
                by_error[error] = by_error.get(error, 0) + 1
                if elapsed >= 5.0:
                    slow.append((url, elapsed))
            parts = [f"{count} {err}" for err, count in sorted(by_error.items(), key=lambda x: -x[1])]
            print(f"  Skipped: {', '.join(parts)}", file=sys.stderr)
            if slow:
                print(f"  Slow (>5s):", file=sys.stderr)
                for url, elapsed in sorted(slow, key=lambda x: -x[1])[:5]:
                    domain = urllib.parse.urlparse(url).netloc
                    print(f"    {elapsed:4.1f}s  {domain}", file=sys.stderr)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def clean_text(text: str) -> str:
    """Clean HTML entities and normalize whitespace."""
    if not text:
        return ""
    text = unescape(text)
    text = RE_ALL_TAGS.sub("", text)
    text = RE_WHITESPACE.sub(" ", text)
    return text.strip()


def is_blocked_url(url: str) -> bool:
    """Check if URL should be blocked (optimized single-regex check)."""
    return bool(_BLOCKED_URL_PATTERN.search(url))


def is_valid_url(url: str) -> bool:
    """Validate URL format."""
    try:
        result = urllib.parse.urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


def is_blocked_content(content: str) -> bool:
    """Check if content is a CAPTCHA/blocked page (returns True if blocked)."""
    if not content or len(content) < 50:
        return False
    content_lower = content[:2000].lower()  # Only check first 2KB for speed
    return any(marker in content_lower for marker in BLOCKED_CONTENT_MARKERS)


def _strip_wiki_tables(html: str) -> str:
    """Remove Wikipedia infobox/navbox tables (may contain nested tables)."""
    for css_class in ("infobox", "navbox"):
        pattern = re.compile(
            rf'<table\b[^>]*class="[^"]*{css_class}[^"]*"[^>]*>',
            re.IGNORECASE,
        )
        while True:
            m = pattern.search(html)
            if not m:
                break
            # Find matching </table> accounting for nesting
            depth = 1
            pos = m.end()
            while depth > 0 and pos < len(html):
                next_open = html.find("<table", pos)
                next_close = html.find("</table>", pos)
                if next_close == -1:
                    break
                if next_open != -1 and next_open < next_close:
                    depth += 1
                    pos = next_open + 6
                else:
                    depth -= 1
                    pos = next_close + 8
            html = html[:m.start()] + html[pos:]
    return html

def _extract_with_trafilatura(html: str) -> str:
    """Extract article text using trafilatura (content-area detection + boilerplate removal)."""
    # Strip Wikipedia infobox/navbox tables before extraction (they render as messy pipe-tables)
    html = _strip_wiki_tables(html)
    import trafilatura
    text = trafilatura.extract(
        html,
        include_links=True,
        include_formatting=True,
        include_tables=True,
        include_comments=False,
        output_format="txt",
    )
    return text or ""


def _extract_with_regex(html: str) -> str:
    """Fallback: extract text from HTML using regex (for when trafilatura returns nothing)."""
    # Strip boilerplate tags
    html = RE_STRIP_TAGS.sub("", html)
    html = RE_COMMENTS.sub("", html)

    html = RE_BR.sub("\n", html)
    html = RE_BLOCK_END.sub("\n\n", html)
    html = RE_LI.sub("\u2022 ", html)

    text = RE_ALL_TAGS.sub(" ", html)
    text = unescape(text)
    text = RE_SPACES.sub(" ", text)
    text = RE_LEADING_SPACE.sub("\n", text)
    return RE_MULTI_NEWLINE.sub("\n\n", text)


def extract_text(html: str) -> str:
    """Extract readable text from HTML. Trafilatura primary, regex fallback."""
    text = _extract_with_trafilatura(html)

    if not text or len(text) < 100:
        text = _extract_with_regex(html)

    # Extract title for prepending
    title_match = RE_TITLE.search(html)
    if title_match:
        raw_title = unescape(title_match.group(1).strip())
        title = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', raw_title)
    else:
        title = ""

    text = text.strip()
    # Strip forum noise lines (likes, timestamps, user roles)
    text = RE_FORUM_NOISE.sub("", text)
    # Clean Wikipedia artifacts: citation refs, internal links, reference lists
    text = RE_WIKI_CITE.sub("", text)
    text = RE_WIKI_CITE_NAMED.sub("", text)
    text = RE_WIKI_LINK.sub(r"\1", text)
    text = RE_WIKI_REFLIST.sub("", text)
    text = RE_MULTI_NEWLINE.sub("\n\n", text)
    # Prepend title if not already present
    if title and not text.startswith(f"# {title}"):
        text = f"# {title}\n\n{text}"
    return text


def extract_title_from_content(content: str) -> str:
    """Extract title from markdown-formatted content."""
    if content.startswith("# "):
        newline = content.find("\n")
        if newline > 0:
            return content[2:newline]
    return ""


MAX_CONTENT_BYTES = 2_000_000  # 2MB max content size

def _jsonld_entries(data) -> List[dict]:
    """Flatten a parsed JSON-LD block into a list of entity dicts.

    Handles @graph arrays and nested @type arrays (the tool previously only
    read data[0] of a list, missing Article data nested in @graph).
    """
    if isinstance(data, list):
        out: List[dict] = []
        for item in data:
            out.extend(_jsonld_entries(item))
        return out
    if not isinstance(data, dict):
        return []
    out = [data]
    graph = data.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            out.extend(_jsonld_entries(item))
    return out


def extract_jsonld_metadata(html: str) -> str:
    """Extract only high-value metadata from JSON-LD that page text doesn't provide:
    datePublished (recency signal), dateModified (staleness signal) and FAQPage
    Q&A pairs (hard to parse from DOM)."""
    blocks = RE_JSON_LD.findall(html)
    if not blocks:
        return ""

    for raw in blocks:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        for entry in _jsonld_entries(data):
            if not isinstance(entry, dict):
                continue

            ld_type = entry.get("@type", "")
            if isinstance(ld_type, list):
                ld_type = ld_type[0] if ld_type else ""

            parts = []

            # FAQPage: Q&A pairs are genuinely hard to extract from rendered HTML
            if ld_type == "FAQPage":
                entities = entry.get("mainEntity", [])
                # Flatten nested lists (e.g. AWS uses [[{...}, {...}]])
                if entities and isinstance(entities[0], list):
                    entities = [e for sub in entities for e in sub]
                for entity in entities[:5]:
                    if not isinstance(entity, dict):
                        continue
                    q = entity.get("name", "")
                    a_obj = entity.get("acceptedAnswer", {})
                    a = a_obj.get("text", "") if isinstance(a_obj, dict) else ""
                    if q and a:
                        parts.append(f"Q: {q}")
                        parts.append(f"A: {a[:300]}")

            # datePublished: recency signal not always visible in page text
            date_pub = entry.get("datePublished", "")
            if date_pub:
                if "T" in str(date_pub):
                    date_pub = str(date_pub).split("T")[0]
                parts.append(f"published: {date_pub}")

            # dateModified: staleness signal not always visible in page text
            date_mod = entry.get("dateModified", "")
            if date_mod:
                if "T" in str(date_mod):
                    date_mod = str(date_mod).split("T")[0]
                parts.append(f"updated: {date_mod}")

            if parts:
                return "[meta] " + " | ".join(parts) + "\n\n" if len(parts) == 1 else "[meta]\n" + "\n".join(parts) + "\n[/meta]\n\n"

    return ""


# =============================================================================
# URL FETCHER (Scrapling-based)
# =============================================================================

# =============================================================================
# SMART CONTENT FILTERS
# =============================================================================

def _is_boilerplate(sentence: str) -> bool:
    """Model-free deterministic boilerplate detection (F1).

    True for cookie/privacy banners, subscribe/share invites, copyright lines,
    nav/breadcrumb fragments, date-only lines, and pure-punctuation/emoji/separator
    lines. Applied to sentences BEFORE BM25 scoring so junk cannot steal budget
    via the centrality weight (boilerplate repeats the most, so it scores high).
    """
    s = sentence.strip()
    if not s:
        return True
    # Pure punctuation / emoji / separator lines (no letters or digits in any script)
    if not re.search(r"[^\W_]", s, re.UNICODE):
        return True
    if len(s) <= 2:
        return True
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(s):
            return True
    return False


def _fact_density(sentence: str) -> float:
    """Fraction of fact-like tokens: numbers, percentages, years, currency, entity caps (F2)."""
    tokens = re.findall(r"[A-Za-z0-9$€£¥%]+(?:\.\d+)?|[\u3400-\u9FFF]+", sentence)
    if not tokens:
        return 0.0
    fact = 0
    for t in tokens:
        if re.search(r"\d|[$€£¥%]", t):
            fact += 1
        elif t[0].isupper() and len(t) >= 3 and t.lower() not in _FACT_CAP_EXCEPTIONS:
            fact += 1
    return fact / len(tokens)


def _heading_query_tokens(query: str) -> Set[str]:
    """Content words of the query used for heading matching (F4)."""
    return {
        w for w in re.findall(r"[a-zA-Z0-9\u00C0-\u024F]+", query.lower())
        if len(w) >= 3 and w not in _STOP_WORDS
    }


def _heading_score(heading: str, q_tokens: Set[str]) -> float:
    """Fraction of query content words appearing in a heading."""
    if not q_tokens:
        return 1.0
    words = set(re.findall(r"[a-zA-Z0-9\u00C0-\u024F]+", heading.lower()))
    return sum(1 for t in q_tokens if t in words) / len(q_tokens)


def _is_junk_heading(heading: str) -> bool:
    """True if the heading is a junk/nav section label (comments, related, references, ...)."""
    return any(pat.search(heading) for pat in SECTION_DROP_HEADING_PATTERNS)


def _filter_sections_by_heading(blocks: List[str], query: str, threshold: float) -> Tuple[List[str], int]:
    """Drop body sections under junk headings that share no query tokens with the query (F4).

    Runs before sentence-level selection to remove big irrelevant chunks early.
    Only sections whose heading matches a junk/nav pattern are eligible for
    dropping — generic content headings (Abstract, Introduction, Results, recipe
    steps, ...) are always kept even when they lack query words. A dropped heading
    drops all blocks until the next heading. Returns (kept_blocks, dropped_sections).
    """
    q_tokens = _heading_query_tokens(query)
    if not q_tokens:
        return blocks, 0
    kept: List[str] = []
    section_active = True  # blocks before any heading stay
    dropped = 0
    for block in blocks:
        stripped = block.strip()
        if stripped.startswith("# ") or stripped.startswith("## ") or stripped.startswith("### "):
            if _is_junk_heading(stripped):
                section_active = _heading_score(stripped, q_tokens) > threshold
            else:
                section_active = True
            if not section_active:
                dropped += 1
                continue
            kept.append(block)
            continue
        if section_active:
            kept.append(block)
    return kept, dropped


def _filter_junk_sections(blocks: List[str]) -> Tuple[List[str], int]:
    """Drop body sections under junk headings (F4, no-query variant).

    Same SECTION_DROP_HEADING_PATTERNS as the search path, but without a query
    the overlap gate is vacuous (there is no query to overlap) — junk sections
    are dropped outright. Generic content headings are never dropped, so real
    content survives even when it has no query words. Blocks before the first
    heading stay. Returns (kept_blocks, dropped_sections).
    """
    kept: List[str] = []
    section_active = True  # blocks before any heading stay
    dropped = 0
    for block in blocks:
        stripped = block.strip()
        if stripped.startswith("# ") or stripped.startswith("## ") or stripped.startswith("### "):
            section_active = not _is_junk_heading(stripped)
            if not section_active:
                dropped += 1
            else:
                kept.append(block)
            continue
        if section_active:
            kept.append(block)
    return kept, dropped


def _prepare_sentences(
    content: str,
    query: str,
    *,
    use_sections: bool = True,
    use_boilerplate: bool = True,
    use_fact: bool = True,
    heading_threshold: float = HEADING_MATCH_THRESHOLD,
    fact_alpha: float = FACT_DENSITY_ALPHA,
) -> dict:
    """Parse content into header + scored body sentences (single source of truth).

    Applies the smart filters before BM25 scoring:
      F4 - drop body sections whose heading shares no query tokens
      F1 - drop boilerplate sentences
      F2 - fact-density boost on the 70/30 BM25+centrality blend
    Returns dict with header_parts, sentences, scores, boilerplate_dropped, sections_dropped.
    """
    from rank_bm25 import BM25Okapi

    # Split into paragraphs first to identify headers
    blocks = content.split("\n\n")
    header_parts: List[str] = []
    body_text_parts: List[str] = []
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if not body_text_parts and (stripped.startswith("# ") or stripped.startswith("[meta")):
            header_parts.append(stripped)
        else:
            body_text_parts.append(stripped)

    result = {
        "header_parts": header_parts,
        "sentences": [],
        "scores": [],
        "boilerplate_dropped": 0,
        "sections_dropped": 0,
        "pool_fallback": False,
    }
    if not body_text_parts:
        return result

    all_sentences = _split_sentences("\n\n".join(body_text_parts))
    if not all_sentences:
        return result

    candidate = all_sentences
    if use_sections:
        section_blocks, result["sections_dropped"] = _filter_sections_by_heading(
            body_text_parts, query, heading_threshold
        )
        if section_blocks:
            candidate = _split_sentences("\n\n".join(section_blocks))
        else:
            candidate = []
    if use_boilerplate and candidate:
        kept: List[str] = []
        for s in candidate:
            if _is_boilerplate(s):
                result["boilerplate_dropped"] += 1
            else:
                kept.append(s)
        candidate = kept

    # Fail-soft: if filtering leaves too small a pool, BM25 idf collapses and the
    # page would fall back to full-length output — use the unfiltered pool instead.
    if len(candidate) < MIN_SCORING_POOL:
        result["boilerplate_dropped"] = 0
        result["sections_dropped"] = 0
        result["pool_fallback"] = True
        candidate = all_sentences
    sentences = candidate
    if not sentences:
        return result

    # BM25 rank sentences by query relevance
    tokenized = [s.lower().split() for s in sentences]
    bm25 = BM25Okapi(tokenized)
    bm25_scores = bm25.get_scores(query.lower().split())

    # Centrality scoring: sentences similar to many others are "hub" sentences
    # (captures important context that BM25 misses when it lacks query terms)
    # Cap at 200 sentences to keep O(n²) manageable (~40K comparisons max)
    word_sets = [set(t) for t in tokenized]
    n = len(sentences)
    centrality = [0.0] * n
    n_cap = min(n, 200)
    if n_cap > 1:
        for i in range(n_cap):
            if not word_sets[i]:
                continue
            total_sim = 0.0
            for j in range(n_cap):
                if i == j or not word_sets[j]:
                    continue
                # Jaccard similarity
                intersection = len(word_sets[i] & word_sets[j])
                union = len(word_sets[i] | word_sets[j])
                if union:
                    total_sim += intersection / union
            centrality[i] = total_sim / (n_cap - 1)

    # Blend: 70% BM25 relevance + 30% centrality importance
    max_bm25 = max(bm25_scores) if max(bm25_scores) > 0 else 1.0
    max_cent = max(centrality) if max(centrality) > 0 else 1.0
    scores = [
        0.7 * (b / max_bm25) + 0.3 * (c / max_cent)
        for b, c in zip(bm25_scores, centrality)
    ]

    # F2: fact-density boost — factual sentences carry more value per token
    if use_fact:
        scores = [
            s * (1.0 + fact_alpha * _fact_density(sent))
            for s, sent in zip(scores, sentences)
        ]

    result["sentences"] = sentences
    result["scores"] = scores
    return result


def _select_sentences(scores: List[float], sentences: List[str], budget: int) -> Tuple[List[str], int]:
    """Select top-scoring sentences within budget (existing selection rule).

    Returns (selected_texts_in_original_order, chars_used).
    """
    if not sentences:
        return [], 0
    ranked = sorted(zip(scores, range(len(sentences)), sentences), reverse=True)
    selected: List[Tuple[int, str]] = []  # (original_index, text)
    chars = 0
    # Minimum score threshold: at least 10% of max blended score
    min_score = 0.1
    for score, idx, sent in ranked:
        if score < min_score or chars >= budget:
            break
        selected.append((idx, sent))
        chars += len(sent) + 1

    selected.sort(key=lambda x: x[0])
    return [s for _, s in selected], chars


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences. Two-level: split on newlines, then on sentence boundaries."""
    sentences: List[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Short lines (headings, list items) stay as-is
        if len(line) < 150:
            sentences.append(line)
        else:
            # Split long lines on sentence boundaries
            parts = RE_SENT_SPLIT.split(line)
            sentences.extend(p.strip() for p in parts if p.strip())
    return sentences


def _compress_with_bm25(
    content: str,
    query: str,
    max_length: int,
    *,
    use_sections: bool = True,
    use_boilerplate: bool = True,
    use_fact: bool = True,
    heading_threshold: float = HEADING_MATCH_THRESHOLD,
    fact_alpha: float = FACT_DENSITY_ALPHA,
) -> str:
    """Query-focused extraction: keep sentences most relevant to query via BM25.

    Smart filters (all default on, fail-soft to unfiltered behavior):
      F4 - drop body sections whose heading matches nothing in the query
      F1 - drop boilerplate sentences before scoring (junk can't steal budget)
      F2 - boost sentences with numbers/currency/entity-like facts
    """
    prep = _prepare_sentences(
        content, query,
        use_sections=use_sections, use_boilerplate=use_boilerplate, use_fact=use_fact,
        heading_threshold=heading_threshold, fact_alpha=fact_alpha,
    )
    header_parts = prep["header_parts"]
    sentences = prep["sentences"]
    scores = prep["scores"]

    if not sentences:
        return content[:max_length]

    budget = max_length - sum(len(h) + 2 for h in header_parts)
    selected_sents, chars = _select_sentences(scores, sentences, budget)

    if not selected_sents:
        return content[:max_length]

    parts = header_parts + selected_sents
    result = "\n".join(parts)
    if chars >= budget:
        result += "\n[Compressed...]"
    return result


def _filter_direct_fetch(content: str) -> str:
    """Order-preserving junk removal for the direct URL-fetch path (no query).

    Applies F4 (junk-section drop) then F1 (boilerplate sentence drop) with the
    SAME pattern sets as the search path; the caller applies the length cap.
    No re-ranking — content stays in original page order (the user asked for the
    page, not a query digest). Fail-soft: never empties the page; if filtering
    leaves fewer than MIN_SCORING_POOL sentences the page is returned unchanged
    (a page that is mostly junk can't be helped, and dropping it risks destroying
    content). On any error falls back to the raw content.
    """
    try:
        blocks = content.split("\n\n")
        header_parts: List[str] = []
        body_text_parts: List[str] = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                continue
            if not body_text_parts and (stripped.startswith("# ") or stripped.startswith("[meta")):
                header_parts.append(stripped)
            else:
                body_text_parts.append(stripped)
        if not body_text_parts:
            return content

        # F4: drop body sections under junk headings (no-query variant)
        section_blocks, _ = _filter_junk_sections(body_text_parts)
        if not section_blocks:
            return content

        # F1: drop boilerplate sentences, original order preserved
        sentences = _split_sentences("\n\n".join(section_blocks))
        kept = [s for s in sentences if not _is_boilerplate(s)]
        if len(kept) < MIN_SCORING_POOL:
            return content

        result = "\n".join(header_parts + kept).strip()
        return result if result else content
    except Exception:
        return content


def _filter_page_text(content: str) -> str:
    """Filter-only path for the report file: F4 + F1, original order, no BM25.

    The report file carries a page's full filtered text (no 10k budget cut, no
    re-ranking, no fact-density boost) so grep can find terms the query never
    mentioned. Reuses the direct-fetch F4/F1 filtering; adds a fail-soft guard
    for the report contract: if filtering leaves the page nearly empty
    (< ~500 chars), the original text is returned unchanged — never an empty page.
    """
    filtered = _filter_direct_fetch(content)
    if filtered is not content and filtered and len(filtered) < 500:
        return content
    return filtered


def _create_fetch_result(
    url: str,
    content: Optional[str],
    min_length: int,
    max_length: int,
    query: str = "",
) -> FetchResult:
    """Create FetchResult from content, applying length checks and truncation."""
    if content and len(content) >= min_length:
        if query:
            if len(content) > max_length:
                content = _compress_with_bm25(content, query, max_length)
        else:
            # Direct fetch (--url): F1+F4 filters first so the cap holds
            # cleaner content; filters never remove content that would have
            # survived the cap (they only drop junk), fail-soft to raw.
            content = _filter_direct_fetch(content)
            if len(content) > max_length:
                content = content[:max_length] + "\n\n[Truncated...]"
        return FetchResult(
            url=url,
            success=True,
            content=content,
            title=extract_title_from_content(content),
        )
    return FetchResult(url=url, success=False, error="Too short")


def _extract_with_scrapling_fallback(page, min_length: int) -> str:
    """Try Scrapling's get_all_text() when w3m/regex extraction is too short.

    This handles JS-heavy pages where our regex extraction strips too much
    but Scrapling's DOM parser preserves the text content.
    """
    try:
        text = page.get_all_text(separator='\n', strip=True)
        if text and len(text) >= min_length:
            # Add title if available
            title = ""
            title_el = page.css('title')
            if title_el:
                raw_title = title_el[0].text.strip() if hasattr(title_el[0], 'text') else ""
                if raw_title:
                    title = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', raw_title)
            if title:
                return f"# {title}\n\n{text}"
            return text
    except Exception:
        pass
    return ""


def _is_pdf(raw: str, url: str) -> bool:
    """Detect PDF content by magic bytes (not URL — .pdf URLs may return HTML 404)."""
    return "%PDF" in raw[:50]


def _extract_pdf(raw_bytes: bytes) -> str:
    """Extract text from PDF using pdftotext (poppler). Writes to temp file since pdftotext needs seekable input."""
    if not PDFTOTEXT_PATH:
        return ""
    import tempfile
    tmp_path = None
    try:
        # Use delete=False and manual cleanup: on Windows, NamedTemporaryFile(delete=True)
        # keeps the file locked, preventing pdftotext from reading it
        f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_path = f.name
        f.write(raw_bytes)
        f.close()
        result = subprocess.run(
            [PDFTOTEXT_PATH, "-layout", tmp_path, "-"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return ""


def _extract_content(raw_html: str) -> Tuple[str, str]:
    """CPU-bound: extract text + JSON-LD from HTML. Runs in process pool."""
    try:
        structured = extract_jsonld_metadata(raw_html)
    except Exception:
        structured = ""
    content = extract_text(raw_html)
    return content, structured


# Shared process pool for CPU-bound text extraction (avoids blocking event loop)
_extract_pool: Optional[ProcessPoolExecutor] = None


def _get_extract_pool() -> ProcessPoolExecutor:
    global _extract_pool
    if _extract_pool is None:
        _extract_pool = ProcessPoolExecutor(max_workers=4)
    return _extract_pool


def _shutdown_extract_pool() -> None:
    """Shut down process pool to prevent hang on exit."""
    global _extract_pool
    if _extract_pool is not None:
        _extract_pool.shutdown(wait=False, cancel_futures=True)
        _extract_pool = None


import atexit
atexit.register(_shutdown_extract_pool)


RE_WIKIPEDIA_URL = re.compile(r'https?://(\w+)\.wikipedia\.org/wiki/(.+?)(?:#.*)?$')
RE_GITHUB_REPO_URL = re.compile(r'https?://github\.com/([^/]+)/([^/]+?)(?:/?|/tree/[^/]+/?)?$')
RE_ARXIV_URL = re.compile(r'https?://arxiv\.org/(?:abs|pdf)/(\d+\.\d+)')
RE_TWITTER_URL = re.compile(r'https?://(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)')
RE_SEMANTIC_SCHOLAR_URL = re.compile(r'https?://(?:www\.)?semanticscholar\.org/paper/(?:.+/)?([a-f0-9]{40})')

def _fetch_wikipedia_api(lang: str, title: str, max_length: int) -> Optional[str]:
    """Fetch clean text from Wikipedia API (no scraping needed)."""
    import urllib.request
    api_url = f"https://{lang}.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(title)}&prop=extracts&explaintext=true&format=json"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        pages = data.get("query", {}).get("pages", {})
        for page_data in pages.values():
            text = page_data.get("extract", "")
            if text:
                page_title = page_data.get("title", title)
                return f"# {page_title}\n\n{text[:max_length]}"
    except Exception:
        pass
    return None

def _fetch_github_readme(owner: str, repo: str, max_length: int) -> Optional[str]:
    """Fetch README from GitHub API as rendered HTML, then extract text."""
    import urllib.request
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        req = urllib.request.Request(api_url, headers={
            "Accept": "application/vnd.github.html+json",
            "User-Agent": "web-research-tool/1.0",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        if not html:
            return None
        # Use our existing text extraction on the rendered HTML
        text = _extract_with_regex(html)
        text = RE_MULTI_NEWLINE.sub("\n\n", text).strip()
        if text:
            return f"# {owner}/{repo}\n\n{text[:max_length]}"
    except Exception:
        pass
    return None

def _fetch_arxiv_api(paper_id: str, max_length: int) -> Optional[str]:
    """Fetch ArXiv paper metadata + abstract via Atom API."""
    import urllib.request
    import xml.etree.ElementTree as ET
    api_url = f"http://export.arxiv.org/api/query?id_list={paper_id}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_data = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return None
        title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        abstract = (entry.findtext("atom:summary", "", ns) or "").strip()
        authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]
        published = (entry.findtext("atom:published", "", ns) or "")[:10]
        categories = [c.get("term", "") for c in entry.findall("atom:category", ns)]

        parts = [f"# {title}\n"]
        if authors:
            parts.append(f"Authors: {', '.join(authors[:10])}")
        if published:
            parts.append(f"Published: {published}")
        if categories:
            parts.append(f"Categories: {', '.join(categories[:5])}")
        parts.append(f"\n## Abstract\n\n{abstract}")

        text = "\n".join(parts)
        return text[:max_length] if text else None
    except Exception:
        pass
    return None

def _fetch_semantic_scholar_api(paper_hash: str, max_length: int) -> Optional[str]:
    """Fetch Semantic Scholar paper metadata + abstract via API (free, no key)."""
    import urllib.request
    api_url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_hash}?fields=title,abstract,authors,year,citationCount,venue"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        title = data.get("title", "Unknown")
        abstract = data.get("abstract") or ""
        authors = [a.get("name", "") for a in (data.get("authors") or [])]
        year = data.get("year")
        citations = data.get("citationCount")
        venue = data.get("venue") or ""
        parts = [f"# {title}\n"]
        if authors:
            parts.append(f"Authors: {', '.join(authors[:10])}")
        if year:
            parts.append(f"Year: {year}")
        if venue:
            parts.append(f"Venue: {venue}")
        if citations is not None:
            parts.append(f"Citations: {citations}")
        if abstract:
            parts.append(f"\n## Abstract\n\n{abstract}")
        text = "\n".join(parts)
        return text[:max_length] if text else None
    except Exception:
        pass
    return None


def _fetch_twitter_api(screen_name: str, tweet_id: str, max_length: int) -> Optional[str]:
    """Fetch tweet text via FxTwitter API (no auth required)."""
    import urllib.request
    api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        tweet = data.get("tweet", {})
        author = tweet.get("author", {})
        name = author.get("name", screen_name)
        handle = author.get("screen_name", screen_name)
        text = tweet.get("text", "")
        created = tweet.get("created_at", "")
        likes = tweet.get("likes", 0)
        retweets = tweet.get("retweets", 0)
        replies = tweet.get("replies", 0)
        if not text:
            return None
        parts = [f"# @{handle} ({name})\n"]
        if created:
            parts.append(f"Date: {created}")
        parts.append(f"Likes: {likes} | Retweets: {retweets} | Replies: {replies}\n")
        parts.append(text)
        quote = tweet.get("quote", {})
        if quote and quote.get("text"):
            q_handle = quote.get("author", {}).get("screen_name", "?")
            parts.append(f"\n> Quoting @{q_handle}:\n> {quote['text']}")
        result = "\n".join(parts)
        return result[:max_length]
    except Exception:
        pass
    return None

def _fetch_wayback_fallback(url: str, max_length: int) -> Optional[str]:
    """Try Wayback Machine for a recent cached version of the page."""
    import urllib.request
    api_url = f"https://archive.org/wayback/available?url={urllib.parse.quote(url, safe='')}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        snapshot = data.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return None
        archive_url = snapshot.get("url", "")
        if not archive_url:
            return None
        req = urllib.request.Request(archive_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        text = _extract_with_regex(html)
        text = RE_MULTI_NEWLINE.sub("\n\n", text).strip()
        if text and len(text) > 200:
            return text[:max_length]
    except Exception:
        pass
    return None


def _probe_browser_launch() -> bool:
    """Actual launch probe — runs in a dedicated thread (never inside the
    event loop, where asyncio.run() is illegal). Unified backend: the
    chromium-headless-shell binary; LD_LIBRARY_PATH is set only on Linux
    (the bundled NSS/NSPR/expat libs live in the user cache).

    The version banner differs by source: the Aletherium Linux build reports
    "Chromium ...", the official Google build reports "Google Chrome for
    Testing ..." (verified live) — accept either token."""
    try:
        env = os.environ.copy()
        if _IS_LINUX:
            env["LD_LIBRARY_PATH"] = str(_SHELL_LIBS_DIR)
        result = subprocess.run(
            [
                str(_SHELL_EXE), "--no-sandbox", "--headless", "--disable-gpu",
                "--version",
            ],
            env=env,
            capture_output=True, timeout=30,
        )
        return (
            result.returncode == 0
            and (b"Chromium" in result.stdout or b"Chrome" in result.stdout)
        )
    except Exception:
        return False


def _browser_available() -> bool:
    """Cached check: can the browser backend actually launch on this system?

    Existence of the binary is NOT enough — on UI-less Linux servers a browser
    often cannot run at all (missing system libs like libX11-xcb, libnss3 too
    old). A failed launch probe marks the browser unavailable for the whole run,
    so search/--url escalation skips the browser instead of burning 5-10s per
    doomed launch. Probe result cached module-wide, checked once per run; runs
    in a thread so it is safe from sync and async call contexts alike.
    """
    global _BROWSER_AVAILABLE, _BROWSER_CHECKED
    if _BROWSER_CHECKED:
        return _BROWSER_AVAILABLE
    _BROWSER_CHECKED = True
    try:
        if not _SHELL_EXE.is_file():
            _BROWSER_AVAILABLE = False
            return _BROWSER_AVAILABLE
    except Exception:
        _BROWSER_AVAILABLE = False
        return _BROWSER_AVAILABLE
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as _pool:
            # Bounded wait: the probe subprocess has its own 30s timeout, but
            # .result() must not block the event loop indefinitely if the
            # thread hangs on teardown. 35s covers the 30s probe + margin.
            _BROWSER_AVAILABLE = _pool.submit(_probe_browser_launch).result(timeout=35)
        if not _BROWSER_AVAILABLE:
            print(
                "Browser present but cannot launch on this system "
                "(missing system libraries?) — browser rendering disabled, "
                "static fetch only",
                file=sys.stderr,
            )
    except Exception:
        _BROWSER_AVAILABLE = False
    return _BROWSER_AVAILABLE


def _ensure_shell_downloaded() -> bool:
    """Download + extract the headless Chromium shell (unified backend).

    Linux: Aletherium bundled-libs build (browser + libs tarballs, sha256-
    verified). macOS/Windows: official Google build (zip resolved from the
    Chrome-for-Testing last-known-good JSON, pinned fallback). All into the
    user cache (same pattern as the old browser fetch: download into a
    user-writable dir, no root, no system modification). Returns True
    on success.
    """
    if _IS_LINUX:
        return _ensure_shell_downloaded_linux()
    return _ensure_shell_downloaded_google()


def _verify_sha256(archive: Path, sha_url: str) -> bool:
    """Verify a downloaded archive against its .sha256 sidecar file.

    The sidecar is small (114 bytes), format "<hash>  <filename>"; compare
    the first token to the archive's SHA-256. Fail-soft: any error (missing
    sidecar, bad hash, network) returns False."""
    import hashlib
    import urllib.request
    try:
        req = urllib.request.Request(sha_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            sidecar = resp.read().decode("utf-8", errors="replace").strip()
        expected = sidecar.split()[0] if sidecar else ""
        if not expected:
            return False
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return digest == expected
    except Exception:
        return False


def _ensure_shell_downloaded_linux() -> bool:
    """Download + extract the Aletherium bundled-libs headless-shell (Linux).

    Fetches the browser and bundled-libs archives (sha256-verified) from the
    pinned upstream release into the user cache. Returns True on success.
    """
    import tarfile
    import tempfile
    import urllib.request

    def _download(url: str, dest: Path) -> bool:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "web-research-tool/1.0"})
            with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
            return True
        except Exception:
            return False

    try:
        _SHELL_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(_SHELL_CACHE_ROOT)) as _tmp:
            _tmp_dir = Path(_tmp)
            browser_arc = _tmp_dir / "browser.tar.gz"
            libs_arc = _tmp_dir / "libs.tar.gz"
            if not _download(_SHELL_BROWSER_URL, browser_arc):
                return False
            if not _verify_sha256(browser_arc, _SHELL_BROWSER_SHA_URL):
                return False
            if not _download(_SHELL_LIBS_URL, libs_arc):
                return False
            if not _verify_sha256(libs_arc, _SHELL_LIBS_SHA_URL):
                return False
            browser_stage = _tmp_dir / "browser"
            libs_stage = _tmp_dir / "libs"
            browser_stage.mkdir()
            libs_stage.mkdir()
            with tarfile.open(browser_arc, "r:gz") as tf:
                tf.extractall(str(browser_stage))
            with tarfile.open(libs_arc, "r:gz") as tf:
                tf.extractall(str(libs_stage))
            # Atomically swap into place (old dirs may exist from a prior run)
            if _SHELL_BROWSER_DIR.exists():
                shutil.rmtree(str(_SHELL_BROWSER_DIR), ignore_errors=True)
            if _SHELL_LIBS_DIR.exists():
                shutil.rmtree(str(_SHELL_LIBS_DIR), ignore_errors=True)
            shutil.move(str(browser_stage), str(_SHELL_BROWSER_DIR))
            shutil.move(str(libs_stage), str(_SHELL_LIBS_DIR))
        return _SHELL_EXE.is_file()
    except Exception:
        return False


def _shell_google_download_url() -> Tuple[str, str]:
    """Resolve (version, zip_url) for the official Google chrome-headless-shell.

    Fetches the Chrome-for-Testing last-known-good JSON (30s timeout) and
    picks the Stable channel's chrome-headless-shell entry for this platform.
    On ANY failure falls back to the pinned version + known URL pattern
    (e.g. 152.0.7977.42 for mac-arm64/mac-x64/win32/win64)."""
    import urllib.request
    version = _SHELL_VERSION_FALLBACK
    url = (
        "https://storage.googleapis.com/chrome-for-testing-public/"
        f"{version}/{_SHELL_CFT_PLATFORM}/chrome-headless-shell-{_SHELL_CFT_PLATFORM}.zip"
    )
    try:
        json_url = (
            "https://googlechromelabs.github.io/chrome-for-testing/"
            "last-known-good-versions-with-downloads.json"
        )
        req = urllib.request.Request(json_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        downloads = data["channels"]["Stable"]["downloads"]["chrome-headless-shell"]
        entry = next(
            (d for d in downloads if d.get("platform") == _SHELL_CFT_PLATFORM), None
        )
        if entry and entry.get("url"):
            return data["channels"]["Stable"]["version"], entry["url"]
    except Exception:
        pass
    return version, url


def _ensure_shell_downloaded_google() -> bool:
    """Download + extract the official Google chrome-headless-shell (mac/win).

    Resolves the Stable-channel version from the Chrome-for-Testing JSON,
    downloads the ~100MB zip into a staging dir, extracts with zipfile and
    atomically moves it to <root>/<version>/chrome-headless-shell-<platform>/.
    Updates the module _SHELL_EXE to the installed binary. CPython's zipfile
    does not restore the zip entry's executable bit on extraction (verified
    empirically on macOS), so the binary is chmod +x'd explicitly. Any
    failure returns False (fail-soft, static fallback)."""
    import os as _os
    import tempfile
    import urllib.request
    import zipfile
    global _SHELL_EXE
    try:
        version, url = _shell_google_download_url()
        install_dir = (
            _SHELL_CACHE_ROOT / version
            / f"chrome-headless-shell-{_SHELL_CFT_PLATFORM}"
        )
        _SHELL_EXE = install_dir / _SHELL_EXE_NAME
        if _SHELL_EXE.is_file():
            _os.chmod(_SHELL_EXE, 0o755)   # repair a stale non-executable extract
            return True
        _SHELL_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(_SHELL_CACHE_ROOT)) as _tmp:
            _tmp_dir = Path(_tmp)
            zip_path = _tmp_dir / "shell.zip"
            req = urllib.request.Request(url, headers={"User-Agent": "web-research-tool/1.0"})
            with urllib.request.urlopen(req, timeout=600) as resp, open(zip_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(str(_tmp_dir))
            stage = _tmp_dir / f"chrome-headless-shell-{_SHELL_CFT_PLATFORM}"
            if not (stage / _SHELL_EXE_NAME).is_file():
                return False
            # Atomically swap into place (old version dirs may exist)
            if install_dir.exists():
                shutil.rmtree(str(install_dir), ignore_errors=True)
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage), str(install_dir))
        _os.chmod(_SHELL_EXE, 0o755)   # zipfile strips the exec bit (see docstring)
        return _SHELL_EXE.is_file()
    except Exception:
        return False


def _ensure_browser() -> None:
    """Preflight: fetch the browser once if missing (best-effort).

    Runs OUTSIDE the timed fetch block — the first download (~100-110MB
    headless-shell) can take minutes and must not count against the
    wall clock. On any failure prints a warning to stderr and leaves browser
    rendering disabled (static fallback).
    """
    if _browser_available():
        return
    print("Installing headless Chromium shell (~100-110MB, one-time)...", file=sys.stderr)
    installed = _ensure_shell_downloaded()
    if not installed:
        print("Browser install failed; falling back to static fetch", file=sys.stderr)
        return
    # Force a re-check so the cached availability reflects the new install
    global _BROWSER_CHECKED
    _BROWSER_CHECKED = False
    if not _browser_available():
        print("Browser install failed; falling back to static fetch", file=sys.stderr)


async def _fetch_browser_page_async(
    url: str, timeout_ms: int = 30000
) -> Tuple[str, str, str, Optional[int]]:
    """Unified chromium-headless-shell backend: render page.

    Launches the shell via Playwright with executable_path. On Linux the
    bundled system libraries are passed in LD_LIBRARY_PATH and --no-sandbox
    is required (VMs/containers where unprivileged user namespaces are
    disabled); macOS/Windows use the self-contained official build as-is.
    Returns (html, innerText, title, status). status is the final HTTP
    status code from goto() (None if no Response was produced — e.g. a
    same-document navigation); network/navigation errors still raise from
    goto() and are caught by the caller's broad except.
    """
    from playwright.async_api import async_playwright

    async with _BROWSER_SEMAPHORE_ASYNC:
        async with async_playwright() as _pw:
            launch_env = None
            if _IS_LINUX:
                launch_env = {**os.environ, "LD_LIBRARY_PATH": str(_SHELL_LIBS_DIR)}
            browser = await _pw.chromium.launch(
                executable_path=str(_SHELL_EXE),
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                env=launch_env,
            )
            try:
                page = await browser.new_page()
                resp = await page.goto(
                    url, timeout=timeout_ms, wait_until="domcontentloaded"
                )
                status = resp.status if resp is not None else None
                await page.wait_for_timeout(4000)   # SPA render settle time — MANDATORY
                raw_html = await page.content()
                # Capture the DOM-text fallback while the page is still alive
                # (page methods raise after the browser context closes).
                dom_text = await page.evaluate("document.body.innerText")
                dom_title = await page.title()
            finally:
                await browser.close()
    return raw_html, dom_text, dom_title, status


async def _fetch_with_browser_async(
    url: str,
    timeout: int,
    min_content_length: int,
    max_content_length: int,
    progress: Optional[ProgressReporter] = None,
) -> FetchResult:
    """Fetch a URL with a headless browser (JS-rendered) and build a FetchResult.

    Unified backend: chromium-headless-shell (official Google build on
    macOS/Windows; bundled-libs build on Linux incl. UI-less servers — no
    root, no system modification, runs from user-writable dirs).
    Mirrors the static path's guards/fallbacks (PDF, blocked content, extraction
    with a DOM innerText fallback), but builds the FetchResult DIRECTLY —
    un-filtered content so the caller's file path can apply its own filters.
    HTTP >= 400 responses are gated on the returned status (goto() resolves
    on 4xx/5xx instead of raising); network errors still surface as
    exceptions from goto() and are caught by the broad except below.
    """
    t0 = time.monotonic()
    try:
        # Browser fetches need more time than the 5s static timeout; floor at
        # 15s while honoring a larger configured timeout.
        raw_html, dom_text, dom_title, status = await _fetch_browser_page_async(
            url, timeout_ms=max(timeout, 15) * 1000
        )
        elapsed = time.monotonic() - t0

        if status is not None and status >= 400:
            if progress:
                progress.url_result(url, False, elapsed, f"HTTP {status}")
            return FetchResult(url=url, success=False, error=f"HTTP {status}")

        if len(raw_html) > MAX_CONTENT_BYTES:
            raw_html = raw_html[:MAX_CONTENT_BYTES]

        if _is_pdf(raw_html, url):
            # PDF is handled by the static path only — if the browser returned a
            # PDF shell, give up (the static result stays authoritative).
            if progress:
                progress.url_result(url, False, elapsed, "PDF extraction failed")
            return FetchResult(url=url, success=False, error="PDF extraction failed")

        if is_blocked_content(raw_html):
            if progress:
                progress.url_result(url, False, elapsed, "CAPTCHA/blocked")
            return FetchResult(url=url, success=False, error="CAPTCHA/blocked")

        # Extract text + JSON-LD in process pool (CPU-bound, don't block event loop)
        loop = asyncio.get_running_loop()
        content, structured = await loop.run_in_executor(
            _get_extract_pool(), _extract_content, raw_html
        )

        # Fallback: browser DOM innerText when primary extraction is too short.
        # (No Scrapling Response object exists here, so the Scrapling DOM parser
        # fallback is replaced with page.evaluate("document.body.innerText").)
        if len(content) < min_content_length:
            if dom_text and len(dom_text) > len(content):
                content = dom_text
                if dom_title:
                    title = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', dom_title.strip())
                    content = f"# {title}\n\n{content}"

        # Prepend structured data to content
        if structured:
            content = structured + content

        # Min-length gate (mirrors the static path's "Too short" semantics):
        # an empty page / blank JS shell must not count as a successful fetch.
        if len(content) < min_content_length:
            if progress:
                progress.url_result(url, False, elapsed, "Too short")
            return FetchResult(url=url, success=False, error="Too short")

        # Build FetchResult DIRECTLY (un-filtered): the caller applies the
        # report-file filters, so content must not be truncated here.
        result = FetchResult(
            url=url,
            success=True,
            content=content,
            title=extract_title_from_content(content),
            source="browser",
        )
        if progress:
            progress.url_result(url, result.success, elapsed, result.error or "")
        return result
    except Exception:
        elapsed = time.monotonic() - t0
        if progress:
            progress.url_result(url, False, elapsed, "Browser error")
        return FetchResult(url=url, success=False, error="Browser error")


async def _maybe_escalate(
    url: str,
    result: FetchResult,
    timeout: int,
    min_content_length: int,
    max_content_length: int,
    progress: Optional[ProgressReporter],
    render: str,
    escalation_budget: int,
    elapsed: float,
) -> FetchResult:
    """Escalation ladder: retry a static failure once with the headless browser.

    Used by --url mode: retry-worthy failures (HTTP 403/429/5xx, CAPTCHA,
    Timeout and Too short) get one browser retry, budget-capped. Search mode
    never calls this (static-only by design, per module docstring item 7).
    Non-retry-worthy failures (404, DNS, PDF...) return unchanged. The browser
    path reports its own progress (one entry); the static result is reported
    here (once) when it wins — each URL is reported exactly once where it was
    before the restructure. The budget check-then-increment is race-free: no
    await between the check and the `+= 1` (single-threaded event loop; the
    await for the browser fetch happens after the increment).
    """
    if render == "auto" and _browser_available():
        global _BROWSER_ESCALATIONS
        retry_worthy = (
            not result.success and result.error in BROWSER_RETRY_ERRORS
        )
        if retry_worthy and _BROWSER_ESCALATIONS < escalation_budget:
            _BROWSER_ESCALATIONS += 1
            browser_result = await _fetch_with_browser_async(
                url, timeout, min_content_length, max_content_length, progress=progress,
            )
            if browser_result.success:
                return browser_result
    if progress:
        progress.url_result(url, result.success, elapsed, result.error or "")
    return result


async def fetch_single_async(
    url: str,
    timeout: int,
    min_content_length: int,
    max_content_length: int,
    progress: Optional[ProgressReporter] = None,
    query: str = "",
    escalation_budget: int = 3,
    render: str = "off",
) -> FetchResult:
    """Fetch single URL using Scrapling's AsyncFetcher (TLS fingerprinting).

    render: "off" | "auto". "auto" retries failed fetches with a
    headless-Chromium-shell render (--url default); "off" is static-only
    (--no-render / search mode).
    escalation_budget: max browser escalations per run in "auto" mode
    (single URL per --url run, so effectively 1).
    """
    # The --url full mode passes max_content_length=None (no cap). _create_fetch_result
    # would crash on `len(content) > None`, so a sentinel huge value disables
    # truncation while keeping the static path code unchanged.
    if max_content_length is None:
        max_content_length = 10 ** 12
    t0 = time.monotonic()
    try:
        # API fast-path: use native APIs for sites that produce cleaner output than scraping
        api_content = None
        loop = asyncio.get_running_loop()
        wiki_match = RE_WIKIPEDIA_URL.match(url)
        if wiki_match:
            lang, title = wiki_match.group(1), wiki_match.group(2)
            api_content = await loop.run_in_executor(
                None, _fetch_wikipedia_api, lang, title, max_content_length
            )
        gh_match = RE_GITHUB_REPO_URL.match(url) if not api_content else None
        if gh_match:
            owner, repo = gh_match.group(1), gh_match.group(2)
            api_content = await loop.run_in_executor(
                None, _fetch_github_readme, owner, repo, max_content_length
            )
        arxiv_match = RE_ARXIV_URL.match(url) if not api_content else None
        if arxiv_match:
            paper_id = arxiv_match.group(1)
            api_content = await loop.run_in_executor(
                None, _fetch_arxiv_api, paper_id, max_content_length
            )
        # Semantic Scholar: try regex first, then fallback to string extraction
        if not api_content and 'semanticscholar.org/paper/' in url:
            s2_match = RE_SEMANTIC_SCHOLAR_URL.match(url)
            paper_hash = s2_match.group(1) if s2_match else None
            if not paper_hash:
                # Fallback: extract last path segment as paperId
                path = url.split('semanticscholar.org/paper/')[-1].split('?')[0].rstrip('/')
                candidate = path.split('/')[-1]
                if len(candidate) == 40 and all(c in '0123456789abcdef' for c in candidate.lower()):
                    paper_hash = candidate
            if paper_hash:
                api_content = await loop.run_in_executor(
                    None, _fetch_semantic_scholar_api, paper_hash, max_content_length
                )
            if not api_content:
                # S2 website returns HTTP 202 for programmatic access — skip Scrapling
                elapsed = time.monotonic() - t0
                s2_domain = urllib.parse.urlparse(url).netloc
                if progress:
                    progress.message(f"    --  {elapsed:5.1f}s  {s2_domain} (S2 API unavailable)")
                return FetchResult(url=url, success=False, error="S2 API unavailable")
        tw_match = RE_TWITTER_URL.match(url) if not api_content else None
        if tw_match:
            screen_name, tweet_id = tw_match.group(1), tw_match.group(2)
            api_content = await loop.run_in_executor(
                None, _fetch_twitter_api, screen_name, tweet_id, max_content_length
            )
        # For API-routed domains, if API failed, scraping won't help — bail early
        api_only = tw_match
        if api_content:
            elapsed = time.monotonic() - t0
            result = _create_fetch_result(url, api_content, min_content_length, max_content_length, query=query)
            if progress:
                progress.url_result(url, result.success, elapsed, result.error or "")
            return result
        if api_only:
            elapsed = time.monotonic() - t0
            result = FetchResult(url=url, success=False, error="API extraction failed")
            if progress:
                progress.url_result(url, False, elapsed, "API failed")
            return result
        _host = urllib.parse.urlparse(url).hostname or ""
        _use_httpx = _host in _CURL_DNS_FAIL_DOMAINS
        # Shared budget across the static attempt AND the httpx fallback: the
        # fallback may never re-spend the full timeout (5+5=10s on DNS-failing
        # domains). httpx gets the remainder of the original timeout, floor 1s.
        _static_deadline = time.monotonic() + timeout
        if not _use_httpx:
            try:
                page = await asyncio.wait_for(
                    AsyncFetcher.get(url, timeout=timeout, stealthy_headers=True),
                    timeout=min(timeout, 5),  # hard cutoff — DNS should resolve in <1s
                )
            except (asyncio.TimeoutError, Exception) as _fetch_err:
                if isinstance(_fetch_err, asyncio.TimeoutError) or "Resolving timed out" in str(_fetch_err):
                    _CURL_DNS_FAIL_DOMAINS.add(_host)
                    _use_httpx = True
                else:
                    raise
        if _use_httpx:
            # curl_cffi c-ares DNS fails for this domain — use httpx (system DNS)
            import httpx
            # Keep the total at ~timeout+1s: scrapling may have burned the whole
            # budget on a slow peer; httpx still gets a short window (floor 1s).
            _remaining = max(1.0, _static_deadline - time.monotonic())
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=min(timeout, _remaining),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            ) as _hx:
                _resp = await _hx.get(url)
            page = types.SimpleNamespace(
                status=_resp.status_code,
                html_content=_resp.text,
                body=_resp.content,
            )
        elapsed = time.monotonic() - t0

        if page.status != 200:
            return await _maybe_escalate(
                url,
                FetchResult(url=url, success=False, error=f"HTTP {page.status}"),
                timeout, min_content_length, max_content_length,
                progress, render, escalation_budget, elapsed,
            )

        try:
            raw_html = page.html_content
        except (UnicodeDecodeError, AttributeError):
            # Scrapling failed to decode — try common encodings on raw bytes
            raw_bytes = page.body if hasattr(page, 'body') else b""
            raw_html = ""
            for enc in ("utf-8", "latin-1", "windows-1252", "iso-8859-1"):
                try:
                    raw_html = raw_bytes.decode(enc, errors="replace")
                    break
                except Exception:
                    continue
            if not raw_html:
                if progress:
                    progress.url_result(url, False, elapsed, "Encoding error")
                return FetchResult(url=url, success=False, error="Encoding error")
        if len(raw_html) > MAX_CONTENT_BYTES:
            # Truncate HTML but still try to extract text
            raw_html = raw_html[:MAX_CONTENT_BYTES]

        if _is_pdf(raw_html, url):
            # PDF: extract via pdftotext in process pool
            raw_body = page.body if isinstance(page.body, bytes) else raw_html.encode("utf-8", errors="replace")
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(
                _get_extract_pool(), _extract_pdf, raw_body
            )
            if not content:
                if progress:
                    progress.url_result(url, False, elapsed, "PDF extraction failed")
                return FetchResult(url=url, success=False, error="PDF extraction failed")
            result = _create_fetch_result(url, content, min_content_length, max_content_length, query=query)
            if progress:
                progress.url_result(url, result.success, elapsed, result.error or "")
            return result

        if is_blocked_content(raw_html):
            return await _maybe_escalate(
                url,
                FetchResult(url=url, success=False, error="CAPTCHA/blocked"),
                timeout, min_content_length, max_content_length,
                progress, render, escalation_budget, elapsed,
            )

        # Extract text + JSON-LD in process pool (CPU-bound, don't block event loop)
        loop = asyncio.get_running_loop()
        content, structured = await loop.run_in_executor(
            _get_extract_pool(), _extract_content, raw_html
        )

        # Fallback: Scrapling's DOM parser when primary extraction is too short
        if len(content) < min_content_length:
            scrapling_content = _extract_with_scrapling_fallback(page, min_content_length)
            if scrapling_content:
                content = scrapling_content

        # Prepend structured data to content
        if structured:
            content = structured + content

        result = _create_fetch_result(url, content, min_content_length, max_content_length, query=query)
        # Wayback Machine fallback for failed/paywalled content
        if not result.success:
            wb_content = await loop.run_in_executor(
                None, _fetch_wayback_fallback, url, max_content_length
            )
            if wb_content:
                result = _create_fetch_result(url, wb_content, min_content_length, max_content_length, query=query)
        # Escalation ladder: retry-worthy failures (HTTP 403/429/5xx, CAPTCHA,
        # Timeout, Too short) get one retry with the headless Chromium shell,
        # budget-capped in "auto" mode. The static result is reported once by
        # the helper when it wins.
        return await _maybe_escalate(
            url,
            result,
            timeout, min_content_length, max_content_length,
            progress, render, escalation_budget, elapsed,
        )

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        return await _maybe_escalate(
            url,
            FetchResult(url=url, success=False, error="Timeout"),
            timeout, min_content_length, max_content_length,
            progress, render, escalation_budget, elapsed,
        )
    except Exception as e:
        elapsed = time.monotonic() - t0
        error_msg = str(e)[:50] if str(e) else type(e).__name__
        logger.debug(f"Fetch error for {url}: {e}")
        return await _maybe_escalate(
            url,
            FetchResult(url=url, success=False, error=error_msg),
            timeout, min_content_length, max_content_length,
            progress, render, escalation_budget, elapsed,
        )


# =============================================================================
# SEARCH BACKENDS
# =============================================================================

def _load_brave_api_key() -> Optional[str]:
    """Load Brave Search API key from env var or config file."""
    key = os.environ.get("BRAVE_API_KEY", "")
    if key:
        return key
    try:
        return BRAVE_API_KEY_PATH.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


_RE_HTML_TAGS = re.compile(r"<[^>]+>")

def _snippet_relevance(query: str, title: str, snippet: str) -> float:
    """Score snippet relevance to query by substring match. Returns 0.0-1.0.

    Uses substring matching instead of word-set intersection because DDG's
    ddgs library strips <b> tags without adding spaces, concatenating adjacent
    words (e.g. "theCRISPRsicklecelltherapy"). Substring match handles this.
    """
    query_words = set(query.lower().split())
    text = _RE_HTML_TAGS.sub(" ", (title + " " + snippet)).lower()
    if not query_words:
        return 1.0
    return sum(1 for w in query_words if w in text) / len(query_words)


class BraveSearch:
    """Brave Search API backend."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(
        self,
        query: str,
        num_results: int = 20,
    ) -> Iterator[Tuple[str, str, str]]:
        """Search Brave and yield (url, title, snippet) tuples."""
        import urllib.request

        encoded = urllib.parse.quote_plus(query)
        url = f"https://api.search.brave.com/res/v1/web/search?q={encoded}&count={min(num_results, 20)}"
        req = urllib.request.Request(url, headers={
            "X-Subscription-Token": self.api_key,
            "Accept": "application/json",
        })

        seen_urls: Set[str] = set()
        count = 0
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for r in data.get("web", {}).get("results", []):
                result_url = r.get("url", "")
                if result_url and result_url not in seen_urls and is_valid_url(result_url) and not is_blocked_url(result_url):
                    seen_urls.add(result_url)
                    yield result_url, r.get("title", ""), r.get("description", "")
                    count += 1
                    if count >= num_results:
                        return
        except Exception as e:
            logger.debug(f"Brave search failed: {e}")
            return


_ACADEMIC_STRONG = (
    "paper", "papers", "preprint", "arxiv", "pubmed", "doi:",
    "meta-analysis", "systematic review", "clinical trial",
    "literature review", "peer-review", "journal article",
)
_ACADEMIC_WEAK = (
    "research", "study", "studies", "algorithm", "neural",
    "genome", "protein", "quantum", "theorem", "benchmark",
    "dataset", "experiment", "hypothesis", "molecular",
    "computational", "optimization", "evaluation", "survey",
    "simulation", "methodology", "technique",
    "prediction", "detection", "classification",
    "learning", "training", "computing",
    "correction", "encoding", "decoding", "synthesis",
    "imaging", "catalyst", "receptor", "enzyme",
    "biodiversity", "ecosystem", "acidification",
    "emission", "photovoltaic", "semiconductor",
    "neuroscience", "cortex", "cognitive",
    "clinical", "therapeutic", "diagnostic",
    "mechanism", "architecture", "model",
    "reinforcement", "robotics", "autonomous",
    "generative", "diffusion", "transformer",
)


def _is_academic_query(query: str) -> bool:
    """Heuristic: does this query likely seek academic/scientific content?
    Strong signals (any 1): paper, arxiv, clinical trial, etc.
    Weak signals (need 2+): research, algorithm, neural, model, etc."""
    q = query.lower()
    if any(s in q for s in _ACADEMIC_STRONG):
        return True
    return sum(1 for w in _ACADEMIC_WEAK if w in q) >= 2


def _detect_ddg_region(query: str) -> Optional[str]:
    """Detect DDG region from query script (Unicode ranges). Returns None for Latin."""
    scripts = {"ja": 0, "zh": 0, "ko": 0}
    for ch in query:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:  # Hiragana + Katakana
            scripts["ja"] += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:  # CJK Unified
            scripts["zh"] += 1  # tentative — overridden by ja if kana present
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:  # Hangul
            scripts["ko"] += 1
    # If kana detected, CJK chars are also Japanese
    if scripts["ja"] > 0:
        scripts["ja"] += scripts["zh"]
        scripts["zh"] = 0
    top = max(scripts, key=scripts.get)
    if scripts[top] == 0:
        return None
    region_map = {"ja": "jp-jp", "zh": "zh-cn", "ko": "kr-kr"}
    return region_map.get(top)


class DuckDuckGoSearch:
    """DuckDuckGo search with early URL filtering."""

    def search(
        self,
        query: str,
        num_results: int = 50,
        region: Optional[str] = None,
    ) -> Iterator[Tuple[str, str, str]]:
        """Search DuckDuckGo and yield (url, title, snippet) tuples."""
        seen_urls: Set[str] = set()
        count = 0

        ddg = DDGS(verify=False)
        ddg_kwargs = {}
        if region:
            ddg_kwargs["region"] = region
        # Request exactly `num_results` (not 2x): paginating a doubled count costs
        # ~2-3s and most extra hits are duplicates/blocked anyway — the count
        # loop already caps the yield, and MultiSearch supplements shortfall
        # with Brave.
        for r in ddg.text(query, max_results=num_results, **ddg_kwargs):
            url = r.get("href", "")
            if url and url not in seen_urls and is_valid_url(url) and not is_blocked_url(url):
                seen_urls.add(url)
                yield url, r.get("title", ""), r.get("body", "")
                count += 1
                if count >= num_results:
                    return


class MultiSearch:
    """Combined search: DDG primary, Brave fallback for coverage gaps."""

    def __init__(self):
        self._brave_key = _load_brave_api_key()

    def search(
        self,
        query: str,
        num_results: int = 20,
    ) -> Iterator[Tuple[str, str, str]]:
        """Search DDG first. If under target, supplement with Brave."""
        seen_urls: Set[str] = set()
        count = 0
        region = _detect_ddg_region(query)

        # Phase 1: DuckDuckGo (primary)
        ddg = DuckDuckGoSearch()
        try:
            for url, title, snippet in ddg.search(query, num_results, region=region):
                if url not in seen_urls:
                    seen_urls.add(url)
                    yield url, title, snippet
                    count += 1
        except Exception as e:
            logger.debug(f"DDG search failed: {e}")
            print(f"  DDG failed ({type(e).__name__}), trying Brave...", file=sys.stderr)

        # Phase 2: Brave (supplement if DDG fell short)
        shortfall = num_results - count
        if shortfall > 0 and self._brave_key:
            brave = BraveSearch(self._brave_key)
            for url, title, snippet in brave.search(query, shortfall + 5):
                if url not in seen_urls:
                    seen_urls.add(url)
                    yield url, title, snippet
                    count += 1
                    if count >= num_results:
                        return


# =============================================================================
# RESEARCH WORKFLOW
# =============================================================================

async def run_research_async(
    config: ResearchConfig,
    progress: ProgressReporter,
    global_seen_urls: Optional[Set[str]] = None,
) -> AsyncIterator[FetchResult]:
    """
    Async streaming research workflow.
    Yields FetchResult objects as they complete.
    Pass global_seen_urls to dedup across multiple parallel queries.
    """
    progress.message(f'Researching: "{config.query}"')

    urls: List[str] = []
    fetch_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    result_queue: asyncio.Queue[Optional[FetchResult]] = asyncio.Queue()
    stats = ResearchStats(query=config.query)

    async def search_producer() -> None:
        loop = asyncio.get_running_loop()
        searcher = MultiSearch()
        t0 = time.monotonic()

        skipped = 0

        def search_and_stream():
            nonlocal skipped
            enqueued = 0
            seen_in_search: Set[str] = set()
            for url, title, snippet in searcher.search(config.query, config.search_results):
                if global_seen_urls is not None:
                    if url in global_seen_urls:
                        continue
                    global_seen_urls.add(url)
                seen_in_search.add(url)
                urls.append(url)
                stats.urls_searched = len(urls)
                # Snippet relevance gate: skip URLs with zero query word overlap
                # Always enqueue at least 5 URLs (safety net for edge cases)
                relevance = _snippet_relevance(config.query, title, snippet)
                if relevance == 0 and enqueued >= 5:
                    if progress.verbose:
                        _host = urllib.parse.urlparse(url).hostname or url
                        print(f"  [skip] {_host} relevance=0 | {title[:50]}", flush=True)
                    skipped += 1
                    continue
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)
                enqueued += 1

            # Supplement with topic-specific bonus sources (not in web search)
            stats.bonus_sources = {}

            def _enqueue_bonus(url: str, source: str = "") -> None:
                nonlocal enqueued
                if url in seen_in_search or not is_valid_url(url) or is_blocked_url(url):
                    return
                if global_seen_urls is not None:
                    if url in global_seen_urls:
                        return
                    global_seen_urls.add(url)
                seen_in_search.add(url)
                urls.append(url)
                stats.urls_searched = len(urls)
                if source:
                    stats.bonus_sources[source] = stats.bonus_sources.get(source, 0) + 1
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)
                enqueued += 1

            # Run bonus searches in parallel (topic-specific sources).
            # NOTE: DDG is hit exactly ONCE per run (the main text search).
            # The former DDG news bonus was removed: a second hit on the same
            # hostname per run doubles the chance of DDG IP rate-limiting and
            # contributed ~1 URL per run (often failing with DDGSException).
            def _bonus_arxiv():
                """Search arXiv API, fallback to Semantic Scholar if arXiv fails."""
                arxiv_ok = False
                try:
                    import urllib.request
                    import xml.etree.ElementTree as ET
                    # Strip dates/filler, use AND between core terms for precision
                    _arxiv_skip = {"recent", "latest", "new", "advances", "applications",
                                   "current", "overview", "update", "the", "and", "for", "with", "from"}
                    words = [w for w in config.query.split()
                             if not re.match(r'^\d{4}$', w) and w.lower() not in _arxiv_skip]
                    core = words[:4]  # max 4 key terms
                    arxiv_query = " AND ".join(f"all:{w}" for w in core) if core else config.query
                    encoded = urllib.parse.quote_plus(arxiv_query)
                    api_url = f"http://export.arxiv.org/api/query?search_query={encoded}&start=0&max_results=5&sortBy=relevance"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        xml_data = resp.read().decode("utf-8", errors="replace")
                    root = ET.fromstring(xml_data)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall("atom:entry", ns):
                        for link in entry.findall("atom:link", ns):
                            href = link.get("href", "")
                            if "arxiv.org/abs/" in href:
                                _enqueue_bonus(href, "arxiv")
                                arxiv_ok = True
                                break
                except Exception:
                    pass
                # Fallback: Semantic Scholar if arXiv returned nothing
                if not arxiv_ok:
                    try:
                        import urllib.request
                        encoded = urllib.parse.quote_plus(config.query)
                        api_url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={encoded}&limit=10&fields=url,externalIds"
                        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                        with urllib.request.urlopen(req, timeout=3) as resp:
                            data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        for paper in (data.get("data") or []):
                            ext_ids = paper.get("externalIds") or {}
                            arxiv_id = ext_ids.get("ArXiv")
                            if arxiv_id:
                                _enqueue_bonus(f"https://arxiv.org/abs/{arxiv_id}", "scholar")
                            else:
                                paper_id = paper.get("paperId", "")
                                if paper_id:
                                    _enqueue_bonus(f"https://www.semanticscholar.org/paper/{paper_id}", "scholar")
                    except Exception:
                        pass

            def _bonus_pubmed():
                """Search PubMed via NCBI E-utilities (free, no key)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={encoded}&retmax=5&retmode=json&sort=relevance"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for pmid in (data.get("esearchresult", {}).get("idlist") or []):
                        _enqueue_bonus(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "pubmed")
                except Exception:
                    pass

            def _bonus_openalex():
                """Search OpenAlex for papers (free, no key for basic search)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://api.openalex.org/works?search={encoded}&per_page=5&mailto=web-research-tool@example.com"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for work in (data.get("results") or []):
                        # Prefer open access URL, then DOI, then landing page
                        oa = work.get("open_access") or {}
                        url = oa.get("oa_url")
                        if not url:
                            doi = work.get("doi")
                            if doi:
                                url = doi  # DOI URLs like https://doi.org/10.1234/...
                        if not url:
                            loc = work.get("primary_location") or {}
                            url = loc.get("landing_page_url")
                        if url:
                            _enqueue_bonus(url, "openalex")
                except Exception:
                    pass

            def _bonus_europepmc():
                """Search Europe PMC for papers (free, no key, more OA full-text than PubMed)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={encoded}&format=json&pageSize=5&sort=CITED%20desc"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for result in (data.get("resultList", {}).get("result") or []):
                        # Prefer full-text URL, then DOI, then Europe PMC page
                        url = None
                        doi = result.get("doi")
                        if doi:
                            url = f"https://doi.org/{doi}"
                        if not url:
                            pmcid = result.get("pmcid")
                            if pmcid:
                                url = f"https://europepmc.org/article/PMC/{pmcid}"
                            else:
                                pmid = result.get("pmid")
                                if pmid:
                                    url = f"https://europepmc.org/article/MED/{pmid}"
                        if url:
                            _enqueue_bonus(url, "europepmc")
                except Exception:
                    pass

            def _bonus_hackernews():
                """Search Hacker News via Algolia API (free, no key, 10K/hr)."""
                try:
                    import urllib.request
                    # Use top 3 key terms to avoid zero-result long queries
                    _hn_skip = {"best", "practices", "latest", "recent", "new", "how", "what",
                                "the", "and", "for", "with", "from", "using", "guide", "tutorial"}
                    words = [w for w in config.query.split() if w.lower() not in _hn_skip][:3]
                    hn_query = " ".join(words) if words else config.query
                    encoded = urllib.parse.quote_plus(hn_query)
                    api_url = f"https://hn.algolia.com/api/v1/search?query={encoded}&tags=story&hitsPerPage=5"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for hit in (data.get("hits") or []):
                        url = hit.get("url")
                        if url:
                            _enqueue_bonus(url, "hackernews")
                        else:
                            # Ask HN / Show HN posts without external URL — link to HN discussion
                            story_id = hit.get("objectID")
                            if story_id:
                                _enqueue_bonus(f"https://news.ycombinator.com/item?id={story_id}", "hackernews")
                except Exception:
                    pass

            def _bonus_stackoverflow():
                """Search Stack Overflow API (free, no key, 300/day unauth)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://api.stackexchange.com/2.3/search/excerpts?order=desc&sort=relevance&q={encoded}&site=stackoverflow&pagesize=5&filter=default"
                    req = urllib.request.Request(api_url, headers={
                        "User-Agent": "web-research-tool/1.0",
                        "Accept-Encoding": "gzip",
                    })
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        # SO API always returns gzip
                        raw = resp.read()
                        if resp.headers.get("Content-Encoding") == "gzip":
                            import gzip
                            raw = gzip.decompress(raw)
                        data = json.loads(raw.decode("utf-8", errors="replace"))
                    for item in (data.get("items") or []):
                        qid = item.get("question_id")
                        if qid:
                            _enqueue_bonus(f"https://stackoverflow.com/questions/{qid}", "stackoverflow")
                except Exception:
                    pass

            def _bonus_devto():
                """Search Dev.to API for articles (free, no key)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    # Dev.to doesn't have keyword search in API, but per_page+tag works
                    # Use page=1&per_page=5 with the query as tag approximation
                    # Actually, the /articles endpoint does support a hidden 'q' param via Forem
                    api_url = f"https://dev.to/api/articles?per_page=5&top=365"
                    # Try tag-based search with first keyword
                    words = config.query.split()
                    if words:
                        tag = re.sub(r'[^a-zA-Z0-9]', '', words[0]).lower()
                        if tag:
                            api_url += f"&tag={tag}"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for article in (data if isinstance(data, list) else []):
                        url = article.get("url")
                        if url:
                            _enqueue_bonus(url, "devto")
                except Exception:
                    pass

            def _bonus_github_repos():
                """Search GitHub repositories API (free, no key, 10/min unauth)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&per_page=5"
                    req = urllib.request.Request(api_url, headers={
                        "User-Agent": "web-research-tool/1.0",
                        "Accept": "application/vnd.github.v3+json",
                    })
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for repo in (data.get("items") or []):
                        url = repo.get("html_url")
                        if url:
                            _enqueue_bonus(url, "github")
                except Exception:
                    pass

            bonus_fns = []
            if config.scientific:
                bonus_fns.extend([_bonus_arxiv, _bonus_openalex])
            if config.medical:
                bonus_fns.extend([_bonus_pubmed, _bonus_europepmc])
                if not config.scientific:
                    bonus_fns.append(_bonus_openalex)
            if config.tech:
                bonus_fns.extend([_bonus_hackernews, _bonus_stackoverflow, _bonus_devto, _bonus_github_repos])
            if bonus_fns:
                with ThreadPoolExecutor(max_workers=len(bonus_fns)) as bonus_pool:
                    list(bonus_pool.map(lambda f: f(), bonus_fns))

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                await loop.run_in_executor(executor, search_and_stream)

            search_elapsed = time.monotonic() - t0
            source_info = f"{stats.urls_searched} URLs"
            if searcher._brave_key:
                source_info += " (DDG+Brave)"
            else:
                source_info += " (DDG)"
            if stats.bonus_sources:
                bonus_parts = [f"{v} {k}" for k, v in sorted(stats.bonus_sources.items())]
                source_info += f" + bonus: {', '.join(bonus_parts)}"
            if skipped:
                source_info += f", {skipped} filtered"
            progress.message(f"  [search] {source_info} in {search_elapsed:.1f}s")
        except Exception as e:
            search_elapsed = time.monotonic() - t0
            progress.message(f"  [search] failed after {search_elapsed:.1f}s: {e}")
        finally:
            await fetch_queue.put(None)

    async def fetch_consumer() -> None:
        semaphore = asyncio.Semaphore(config.max_concurrent)
        pending: List[asyncio.Task] = []
        fetch_limit = config.fetch_count

        async def fetch_one(url: str) -> None:
            async with semaphore:
                result = await fetch_single_async(
                    url, config.timeout,
                    config.min_content_length, config.max_content_length,
                    progress=progress, query=config.query,
                )
                await result_queue.put(result)

        while True:
            url = await fetch_queue.get()
            if url is None:
                break
            if fetch_limit == 0 or len(pending) < fetch_limit:
                pending.append(asyncio.create_task(fetch_one(url)))

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await result_queue.put(None)

    progress.phase_start("fetch")
    # Fresh per-run escalation budget (module counter shared across fetch tasks)
    global _BROWSER_ESCALATIONS
    _BROWSER_ESCALATIONS = 0
    asyncio.create_task(search_producer())
    asyncio.create_task(fetch_consumer())

    fetched = 0
    while True:
        result = await result_queue.get()
        if result is None:
            break
        fetched += 1
        if result.success:
            stats.urls_fetched += 1
            stats.content_chars += len(result.content)
        progress.update("fetch", fetched, stats.urls_searched or fetched)
        yield result

    progress.newline()
    progress.summary(stats.urls_fetched, stats.urls_searched, stats.content_chars)


# =============================================================================
# CROSS-PAGE DEDUPLICATION
# =============================================================================

# Common English stop words for fuzzy dedup signatures
_STOP_WORDS = frozenset(
    "a an the and or but in on at to for of is it its be by as was were are been "
    "has have had do does did will would shall should can could may might this that "
    "these those with from not no nor so if then than too also very just about above "
    "after before between each few more most other some such only own same through "
    "during until while into over under again further once here there when where why "
    "how all any both each which what who whom".split()
)


def _normalize_sentence(s: str) -> str:
    """Normalize a sentence for exact dedup: lowercase, strip punctuation, collapse whitespace."""
    s = s.lower().strip()
    s = re.sub(r'[^\w\s]', '', s)
    return RE_WHITESPACE.sub(' ', s)


def _content_signature(s: str) -> str:
    """Content-word signature for fuzzy dedup. Strips stop words, sorts remaining → key.
    Two sentences with the same content words in any order match."""
    words = sorted(w for w in s.lower().split() if w not in _STOP_WORDS and len(w) > 2)
    return " ".join(words)


@dataclass
class DedupStats:
    """Track dedup savings."""
    chars_before: int = 0
    chars_after: int = 0
    exact_dupes: int = 0
    fuzzy_dupes: int = 0
    pages_dropped: int = 0


def _dedup_results(
    results: List[FetchResult],
    seen: Optional[Set[str]] = None,
    seen_fuzzy: Optional[Set[str]] = None,
) -> Tuple[List[FetchResult], DedupStats]:
    """Remove duplicate sentences across pages (exact + fuzzy).
    Pass shared sets to dedup across multiple calls."""
    if seen is None:
        seen = set()
    if seen_fuzzy is None:
        seen_fuzzy = set()
    deduped: List[FetchResult] = []
    stats = DedupStats()

    for r in results:
        if not r.success:
            deduped.append(r)
            continue

        stats.chars_before += len(r.content)
        lines = r.content.split("\n")
        kept: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            # Always keep headers and metadata
            if stripped.startswith("# ") or stripped.startswith("[meta"):
                kept.append(line)
                continue
            # Skip very short lines (not worth deduping)
            if len(stripped) < 40:
                kept.append(line)
                continue
            # Exact dedup (normalized)
            norm = _normalize_sentence(stripped)
            if norm in seen:
                stats.exact_dupes += 1
                continue
            seen.add(norm)
            # Fuzzy dedup (content-word signature)
            sig = _content_signature(norm)
            if sig and len(sig) > 10 and sig in seen_fuzzy:
                stats.fuzzy_dupes += 1
                continue
            if sig and len(sig) > 10:
                seen_fuzzy.add(sig)
            kept.append(line)

        new_content = "\n".join(kept).strip()
        stats.chars_after += len(new_content)
        if len(new_content) < 50:
            stats.pages_dropped += 1
            continue
        deduped.append(FetchResult(
            url=r.url,
            success=True,
            content=new_content,
            title=r.title,
            source=r.source,
        ))

    # Log dedup effectiveness at debug level
    if stats.chars_before > 0 and (stats.exact_dupes or stats.fuzzy_dupes):
        saved_pct = 100 * (1 - stats.chars_after / stats.chars_before)
        logger.debug(
            f"Dedup: {stats.chars_before:,} → {stats.chars_after:,} chars "
            f"({saved_pct:.0f}% saved, {stats.exact_dupes} exact + {stats.fuzzy_dupes} fuzzy dupes, "
            f"{stats.pages_dropped} pages dropped)"
        )

    return deduped, stats


def _shingles(s: str, k: int = 3) -> Set[str]:
    """Word k-grams of a normalized sentence; whole sentence as one shingle if shorter."""
    words = s.split()
    if len(words) < k:
        return {s}
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def _dedup_cross_page_sentences(
    results: List[FetchResult],
    jaccard_threshold: float = CROSS_PAGE_JACCARD_THRESHOLD,
    min_len: int = CROSS_PAGE_DEDUP_MIN_LEN,
) -> Tuple[List[FetchResult], dict]:
    """Cross-page near-duplicate sentence removal (F3), applied batch-wide in page order.

    Normalized exact matching always applies; shingle-Jaccard near-dup detection
    applies above jaccard_threshold (0.0 disables it). Only sentences already seen
    on EARLIER pages are dropped — legitimate within-page repeats are kept. Applies
    to verbatim pages too (they may contain cross-page mirrors). Never empties a
    page: at least its first non-header body line survives.
    Returns (results, stats dict).
    """
    seen_norm: Set[str] = set()
    seen_shingles: Dict[str, List[int]] = {}  # shingle -> ids of kept sentences sharing it
    stored_shingles: List[Set[str]] = []      # shingle set per kept sentence (id = index)
    stats = {"chars_before": 0, "chars_after": 0, "exact_dropped": 0, "near_dropped": 0}

    out: List[FetchResult] = []
    for r in results:
        if not r.success:
            out.append(r)
            continue
        lines = r.content.split("\n")
        kept_lines: List[str] = []
        page_norms: Set[str] = set()   # norms added by THIS page (never checked against)
        page_ids: Set[int] = set()     # ids added by THIS page (never checked against)
        stats["chars_before"] += len(r.content)

        for line in lines:
            stripped = line.strip()
            if not stripped:
                kept_lines.append(line)
                continue
            # Headers, metadata and short lines pass through
            if stripped.startswith("# ") or stripped.startswith("[meta"):
                kept_lines.append(line)
                continue
            if len(stripped) < min_len:
                kept_lines.append(line)
                continue
            norm = _normalize_sentence(stripped)
            if not norm:
                kept_lines.append(line)
                continue
            if norm in seen_norm:
                if norm in page_norms:
                    kept_lines.append(line)  # within-page repeat — keep
                else:
                    stats["exact_dropped"] += 1
                continue
            if jaccard_threshold > 0.0:
                sh = _shingles(norm)
                candidates: Set[int] = set()
                for shg in sh:
                    for cid in seen_shingles.get(shg, ()):
                        if cid not in page_ids:
                            candidates.add(cid)
                dup = False
                for cid in candidates:
                    a = stored_shingles[cid]
                    inter = len(sh & a)
                    union = len(sh | a)
                    if union and inter / union > jaccard_threshold:
                        dup = True
                        break
                if dup:
                    stats["near_dropped"] += 1
                    continue
                if sh:
                    sid = len(stored_shingles)
                    for shg in sh:
                        seen_shingles.setdefault(shg, []).append(sid)
                    stored_shingles.append(sh)
                    page_ids.add(sid)
            seen_norm.add(norm)
            page_norms.add(norm)
            kept_lines.append(line)

        # Never empty a page: ensure at least one non-header body line survives
        if not any(l.strip() and not l.strip().startswith(("# ", "[meta")) for l in kept_lines):
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith(("# ", "[meta")):
                    kept_lines.append(line)
                    break
        new_content = "\n".join(kept_lines).strip()
        stats["chars_after"] += len(new_content)
        if not new_content:
            continue
        out.append(FetchResult(
            url=r.url, success=True, content=new_content,
            title=r.title, source=r.source,
        ))

    # Log dedup effectiveness at debug level
    if stats["chars_before"] > 0 and (stats["exact_dropped"] or stats["near_dropped"]):
        saved_pct = 100 * (1 - stats["chars_after"] / stats["chars_before"])
        logger.debug(
            f"Cross-page dedup: {stats['chars_before']:,} → {stats['chars_after']:,} chars "
            f"({saved_pct:.2f}% saved, {stats['exact_dropped']} exact + "
            f"{stats['near_dropped']} near-dup drops)"
        )

    return out, stats


# =============================================================================
# QUALITY FILTERS F5, F7 (batch path)
# =============================================================================
# F5: cross-domain content-farm / syndication detection (page-level).
# F7: recency filter (stale pages on recency-sensitive queries).
# All are conservative, fail-soft (errors -> no filtering), and operate
# ONLY on the batch search path (never on single --url fetches).


def _registrable_domain(netloc: str) -> str:
    """Registrable domain: last two labels (www.example.co.uk -> co.uk is coarse
    but consistent with the tool's existing domain handling)."""
    labels = netloc.lower().split(".")
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return netloc


def _page_body_signature(content: str, limit: int = FARM_SIG_CHARS) -> str:
    """Normalized body text used for page-level similarity: strips header/meta
    lines, lowercases, removes punctuation, collapses whitespace, caps length."""
    lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("# ") or stripped.startswith("[meta"):
            continue
        lines.append(stripped)
    body = " ".join(lines)[:limit]
    body = _normalize_sentence(body)
    return body


def _shingle_jaccard(a: str, b: str) -> float:
    """Shingle-Jaccard similarity between two normalized texts."""
    if not a or not b:
        return 0.0
    sa = _shingles(a)
    sb = _shingles(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    if not union:
        return 0.0
    return inter / union


def _extract_byline_names(content: str) -> Set[str]:
    """Author names from byline/author-bio patterns ('By X', 'About X',
    'written by X', 'I'm X —', 'By Dr. X'). Lowercased, deduped."""
    names: Set[str] = set()
    for pat in FARM_BYLINE_PATTERNS:
        for m in pat.finditer(content[:4000]):
            raw = m.group(1).strip()
            name = raw.lower()
            # Require a plausible personal name (at least 2 alpha words or 1 long word)
            words = [w for w in re.split(r"\s+", name) if w.isalpha()]
            if not words:
                continue
            if len(words) >= 2 or len(name) >= 5:
                names.add(name)
    return names


def _has_farm_boilerplate(content: str) -> bool:
    """True if the page carries farm-network self-promotion phrases."""
    return any(pat.search(content) for pat in FARM_NETWORK_PATTERNS)


def _page_cross_links(content: str, group_netlocs: List[str]) -> bool:
    """True if the page text mentions another group member's registrable domain
    (the 'read on our network' cross-link pattern)."""
    lower = content.lower()
    for netloc in group_netlocs:
        reg = _registrable_domain(netloc)
        # Match bare domain or a URL containing it
        if reg in lower and reg not in {"com", "net", "org"}:
            return True
    return False


def _dedup_farm_pages(
    results: List[FetchResult],
    jaccard_threshold: float = FARM_PAGE_JACCARD_THRESHOLD,
    min_pages: int = FARM_GROUP_MIN_PAGES,
    min_domains: int = FARM_GROUP_MIN_DOMAINS,
    min_signals: int = FARM_MIN_SIGNALS,
    hard_signals: int = FARM_HARD_SIGNALS,
) -> Tuple[List[FetchResult], dict]:
    """F5: drop cross-domain content-farm / syndication groups (page level).

    Groups pages whose body-text shingle-Jaccard similarity exceeds
    jaccard_threshold AND whose registrable domains differ. A group is a farm
    candidate only when it spans >= min_pages pages across >= min_domains
    distinct domains (near-identical content across unrelated domains). To
    actually DROP pages, the group must carry >= min_signals independent
    signals: (a) multi-domain near-dup (inherent), (b) byline/author
    repetition, (c) farm boilerplate phrases, (d) cross-links between members.

    Canonical-origin rule: with 2+ signals the redundant copies are dropped but
    the first-fetched representative (canonical) is kept — a copy that looks
    like legitimate syndication (same article mirrored on a partner site) is
    never the sole survivor. With >= hard_signals (confirmed PBN: byline +
    boilerplate + cross-links all present) the whole group is dropped.
    When in doubt (fewer than min_signals) nothing is dropped.

    Fail-soft: on any error returns the input unchanged.
    Returns (results, stats dict).
    """
    try:
        if not QUALITY_FILTERS_ENABLED:
            return results, {"farm_dropped": 0, "groups": 0}
        ok = [r for r in results if r.success]
        if len(ok) < min_pages:
            return results, {"farm_dropped": 0, "groups": 0}

        # Signatures for the pages we can compare
        sigs: List[Optional[str]] = []
        netlocs: List[str] = []
        for r in results:
            if r.success:
                sigs.append(_page_body_signature(r.content))
                netlocs.append(urllib.parse.urlparse(r.url).netloc)
            else:
                sigs.append(None)
                netlocs.append("")

        # Union-find over near-dup pairs from DIFFERENT registrable domains
        parent = list(range(len(results)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(results)):
            if not results[i].success or not sigs[i]:
                continue
            for j in range(i + 1, len(results)):
                if not results[j].success or not sigs[j]:
                    continue
                if _registrable_domain(netlocs[i]) == _registrable_domain(netlocs[j]):
                    continue
                if _shingle_jaccard(sigs[i], sigs[j]) >= jaccard_threshold:
                    union(i, j)

        # Group members by root
        groups: Dict[int, List[int]] = {}
        for i in range(len(results)):
            if results[i].success:
                groups.setdefault(find(i), []).append(i)

        dropped: Set[int] = set()
        group_count = 0
        for members in groups.values():
            if len(members) < min_pages:
                continue
            domains = {_registrable_domain(netlocs[i]) for i in members}
            if len(domains) < min_domains:
                continue
            group_count += 1

            # Signal (a): multi-domain near-dup — inherent for eligible groups.
            # Signal (b): byline repetition — same author on >= 2 members.
            byline_sets = [_extract_byline_names(results[i].content) for i in members]
            common_bylines: Set[str] = set()
            for s in byline_sets:
                for name in s:
                    if sum(1 for s2 in byline_sets if name in s2) >= 2:
                        common_bylines.add(name)
            signal_b = bool(common_bylines)

            # Signal (c): farm boilerplate on any member.
            signal_c = any(_has_farm_boilerplate(results[i].content) for i in members)

            # Signal (d): cross-links — any member mentions another member's domain.
            member_netlocs = [netlocs[i] for i in members]
            signal_d = any(
                _page_cross_links(results[i].content, [n for n in member_netlocs if n != netlocs[i]])
                for i in members
            )

            signals = (1 if signal_b else 0) + (1 if signal_c else 0) + (1 if signal_d else 0)
            if signals >= hard_signals:
                dropped.update(members)
            elif signals >= min_signals:
                # Keep the first-fetched (canonical) representative; drop copies.
                dropped.update(members[1:])

        if not dropped:
            return results, {"farm_dropped": 0, "groups": group_count}

        out: List[FetchResult] = []
        for i, r in enumerate(results):
            if i in dropped:
                continue
            out.append(r)
        return out, {"farm_dropped": len(dropped), "groups": group_count}
    except Exception:
        return results, {"farm_dropped": 0, "groups": 0}


# --- Stub-page rule (structural) --------------------------------------------

def _is_stub_page(content: str) -> bool:
    """True when a page has essentially no body: the extracted content is small
    AND contains nothing beyond its own title/heading (forum/social
    homepages, empty product listings). Both conditions must hold so a small
    page with real body text is never treated as a stub. Fail-soft: content
    that cannot be measured is kept."""
    if not isinstance(content, str) or not content:
        return False
    total = len(content)
    if total >= STUB_MAX_TOTAL_CHARS:
        return False
    body_chars = 0
    in_meta = False
    for line in content.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("[meta"):
            in_meta = True
            continue
        if in_meta:
            if "[/meta]" in s:
                in_meta = False
            continue
        if s.startswith(("# ", "## ", "### ")):
            continue
        body_chars += len(s)
    return body_chars < STUB_MAX_BODY_CHARS


def _drop_stub_pages(
    results: List[FetchResult],
) -> Tuple[List[FetchResult], dict]:
    """Drop stub pages (structural, no model). Gated by QUALITY_FILTERS_ENABLED,
    batch path only. Fail-soft: on any error the page set is unchanged.
    Returns (results, stats dict with 'stub_dropped')."""
    stats: dict = {"stub_dropped": 0}
    try:
        if not QUALITY_FILTERS_ENABLED:
            return results, stats
        drop_ids = {id(r) for r in results if r.success and _is_stub_page(r.content)}
        if not drop_ids:
            return results, stats
        stats["stub_dropped"] = len(drop_ids)
        return [r for r in results if id(r) not in drop_ids], stats
    except Exception:
        logger.debug("stub filter failed; skipping")
        return results, stats


# --- F7: recency filter -------------------------------------------------------

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_RE_MONTH = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\b",
    re.IGNORECASE,
)
_RE_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_RE_MONTH_DAY_YEAR = re.compile(
    r"\b([A-Z][a-z]{2,8})\s+(\d{1,2}),?\s+(\d{4})\b"
)
_RE_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\s+(\d{4})\b"
)
_RE_URL_DATE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/")
_RE_QUERY_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _extract_page_date(content: str, url: str) -> Optional[object]:
    """Best-effort publication date from the page content (recency filter).

    Priority chain (all conservative, fail-soft): [meta] published/updated
    lines written by the JSON-LD extractor -> visible month-name byline dates
    near the top -> URL path /YYYY/MM/DD. Never trusts HTTP Last-Modified (CDNs
    reset it) and never parses arbitrary YYYY-MM-DD tokens in the body (they
    match version strings, citations, junk). Returns a datetime.date or None.
    """
    from datetime import datetime, timedelta, date
    now = datetime.now()
    head = content[:2000]

    # 1. [meta] blocks produced by extract_jsonld_metadata (datePublished/dateModified).
    #    Two emitted shapes: single-part "[meta] published: YYYY-MM-DD" (one part,
    #    no closer) and multi-part "[meta]\npublished: ...\nupdated: ...\n[/meta]"
    #    (parts on separate lines, closed with [/meta]). Scan each whole block so
    #    dates on later lines are found; published keeps priority over updated.
    #    The block boundary ([/meta] closer, or the line end for the single-part
    #    form) keeps body-text dates from matching. They are normally prepended
    #    at the top, but scan the whole content — the marker prefix makes false
    #    matches in body text essentially impossible.
    for pat in (
        re.compile(r"\bpublished:\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.IGNORECASE),
        re.compile(r"\bupdated:\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.IGNORECASE),
    ):
        pos = 0
        while True:
            s = content.find("[meta]", pos)
            if s == -1:
                break
            closer = content.find("[/meta]", s)
            if closer != -1:
                block = content[s:closer + len("[/meta]")]
                pos = closer + len("[/meta]")
            else:
                nl = content.find("\n", s)
                block = content[s:nl if nl != -1 else len(content)]
                pos = nl if nl != -1 else len(content)
            m = pat.search(block)
            if m:
                try:
                    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                    return d
                except ValueError:
                    pass

    # 2. Visible byline dates near the top ("March 19, 2026", "19 March 2026")
    m = _RE_MONTH_DAY_YEAR.search(head)
    if m and m.group(1).lower()[:3] in {mo[:3].lower() for mo in _MONTH_NAMES}:
        try:
            month = next(i + 1 for i, mo in enumerate(_MONTH_NAMES) if mo.lower()[:3] == m.group(1).lower()[:3])
            d = datetime(int(m.group(3)), month, int(m.group(2))).date()
            return d
        except ValueError:
            pass
    m = _RE_DAY_MONTH_YEAR.search(head)
    if m and m.group(2).lower()[:3] in {mo[:3].lower() for mo in _MONTH_NAMES}:
        try:
            month = next(i + 1 for i, mo in enumerate(_MONTH_NAMES) if mo.lower()[:3] == m.group(2).lower()[:3])
            d = datetime(int(m.group(3)), month, int(m.group(1))).date()
            return d
        except ValueError:
            pass

    # 3. URL path /YYYY/MM/DD (common on news/blog platforms)
    m = _RE_URL_DATE.search(url)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            return d
        except ValueError:
            pass

    return None


def _is_recency_sensitive_query(query: str) -> bool:
    """True when the query demands fresh content (recency filter applies).

    Evergreen queries (history, why do, how to, explained, ethics, tutorial,
    guide, what is...) never get recency drops. Otherwise a year in the query
    or a time-sensitive word (news, latest, update, release, benchmark,
    breaking changes, vs, ...) makes the query recency-sensitive.
    """
    q = query.lower()
    if any(w in q for w in RECENCY_EVERGREEN_WORDS):
        return False
    if _RE_QUERY_YEAR.search(query):
        return True
    return any(w in q for w in RECENCY_SENSITIVE_WORDS)


def _recency_filter(results: List[FetchResult], query: str) -> Tuple[List[FetchResult], dict]:
    """F7: drop stale pages on recency-sensitive queries.

    Pages with an extractable date older than RECENCY_MAX_AGE_DAYS are dropped
    (or older than the year named in the query when the query names one).
    Pages with NO extractable date are always kept (fail-soft: an undated page
    cannot be proven stale). If >= 50% of the batch is undated, recency is
    non-applicable and nothing is dropped (matches the tool's fail-soft
    philosophy — never empty the digest on missing data).
    Returns (results, stats dict).
    """
    stats: dict = {"stale_dropped": 0, "recency_skipped": False}
    try:
        if not QUALITY_FILTERS_ENABLED or not query:
            return results, stats
        if not _is_recency_sensitive_query(query):
            return results, stats

        pages = [r for r in results if r.success]
        if len(pages) < 2:
            return results, stats

        dates = [_extract_page_date(r.content, r.url) for r in pages]
        undated = sum(1 for d in dates if d is None)
        if undated / len(pages) >= RECENCY_UNDATED_MAX_FRACTION:
            stats["recency_skipped"] = True
            return results, stats

        from datetime import datetime, timedelta
        now = datetime.now()
        cutoff_days = RECENCY_MAX_AGE_DAYS
        q_year = None
        ym = _RE_QUERY_YEAR.search(query)
        if ym:
            q_year = int(ym.group(0))

        drop_idx: Set[int] = set()
        for r, d in zip(pages, dates):
            if d is None:
                continue
            age_days = (now.date() - d).days
            if age_days < 0 or age_days > 60 * 365:  # clamp: future/junk dates
                continue
            if q_year:
                # Older than the year named in the query — with a 1-year grace so
                # a 2025 page on a "2026" query (fresh, months old) is never dropped.
                stale = d.year < q_year - 1
            else:
                stale = age_days > cutoff_days
            if stale:
                drop_idx.add(id(r))

        if not drop_idx:
            return results, stats
        stats["stale_dropped"] = len(drop_idx)
        return [r for r in results if id(r) not in drop_idx], stats
    except Exception:
        logger.debug("recency filter failed; skipping")
        return results, stats


# =============================================================================
# BATCH OUTPUT FORMATTERS (for non-streaming mode)
# =============================================================================

def format_batch_raw(results: List[FetchResult]) -> str:
    """Format all results as raw text (optimized with StringIO)."""
    buffer = StringIO()
    for r in results:
        if r.success:
            buffer.write(f"=== {r.url} ===\n")
            buffer.write(r.content)
            buffer.write("\n\n")
    return buffer.getvalue()


# =============================================================================
# DIGEST + REPORT FILE OUTPUT (search path)
# =============================================================================

def _report_root() -> Path:
    """Repo root resolved from the script location: .opencode/tools/ -> two dirs up."""
    return Path(__file__).resolve().parent.parent.parent


def _report_dir() -> Path:
    """Report directory: <REPO_ROOT>/tmp/webresearch/."""
    return _report_root() / "tmp" / REPORT_DIR_NAME


def _query_slug(query: str) -> str:
    """Sanitized query for the run-id: lowercase alnum, '-' separators, max ~40 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    return slug[:40] or "query"


def _make_run_id(query: str) -> str:
    """YYYYMMDD-HHMMSS-<query-slug>-<rand4>; rand4 = random alnum for parallel-safety."""
    rand4 = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{_query_slug(query)}-{rand4}"


def _rotate_report_files() -> None:
    """Prune <REPO_ROOT>/tmp/webresearch/*.txt: delete files older than 7 days,
    keep only the 30 newest regardless. Fail-soft."""
    try:
        report_dir = _report_dir()
        if not report_dir.is_dir():
            return
        files = sorted(report_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        cutoff = time.time() - REPORT_MAX_AGE_DAYS * 86400
        fresh: List[Path] = []
        for f in files:
            try:
                is_old = f.stat().st_mtime < cutoff
            except OSError:
                is_old = False
            if is_old:
                try:
                    f.unlink()
                except OSError:
                    pass
            else:
                fresh.append(f)
        for f in fresh[REPORT_MAX_FILES:]:
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _ensure_report_dir() -> None:
    """mkdir the report dir at startup + rotate old files. Fail-soft."""
    try:
        _report_dir().mkdir(parents=True, exist_ok=True)
        _rotate_report_files()
    except Exception:
        pass


def _prepare_report_pages(results: List[FetchResult]) -> List[Tuple[FetchResult, str, bool]]:
    """FILTERED FULL TEXT per successful result, in final (deduped) order.

    Only quality-preserving filters: F4 junk-section removal + F1 boilerplate
    sentence removal, original order. No BM25 selection, no 10k budget cut, no
    fact-density re-ranking — the file must be grep-safe for terms the query
    never mentioned. Per-page safety ceiling at REPORT_PAGE_CEILING chars.
    Third tuple element = whether the text was truncated at that ceiling.
    """
    pages: List[Tuple[FetchResult, str, bool]] = []
    for r in results:
        if not r.success:
            continue
        text = _filter_page_text(r.content)
        if not text:
            continue
        truncated = len(text) > REPORT_PAGE_CEILING
        if truncated:
            text = text[:REPORT_PAGE_CEILING] + "\n[truncated at 200k chars]"
        pages.append((r, text, truncated))
    return pages


def _build_report_file(pages: List[Tuple[FetchResult, str, bool]]) -> Tuple[str, List[int]]:
    """Full report text: `=== <url> ===` + filtered text per page.

    Returns (content, start_lines): start_lines[i] is the 1-based line number
    of the i-th page's `=== <url> ===` header within the returned content.
    Line accounting must be exact — the digest's @line/@hit locators are
    computed from these values (never by the model).
    """
    buffer = StringIO()
    start_lines: List[int] = []
    line = 1
    for r, text, _ in pages:
        start_lines.append(line)
        segment = f"=== {r.url} ===\n" + text + "\n\n"
        buffer.write(segment)
        # splitlines() counts consumed lines exactly (trailing-newline cases
        # and empty separator lines included) — @line/@hit depend on this.
        line += len(segment.splitlines())
    return buffer.getvalue(), start_lines


def _human_chars(n: int) -> str:
    """Compact char count for the digest: 14200 -> '14.2K', 900 -> '900'."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


QUERY_STOPWORDS = frozenset(
    """the a an and or but for in on of to with from by at as is are was were be been
    being do does did can could should would may might will shall not no yes so if
    then than that this these those it its you your we our they their he she him her
    them i me my what which who whom when where why how about into over under between
    through during before after above below again further once here there all any both
    each few more most other some such only own same too very just also has have had""".split()
)


def _query_key_terms(query: str) -> List[str]:
    r"""Up to 2 significant query terms for @hit locators; [] when none.

    `\w` (unicode-aware) keeps non-English queries working (Cyrillic/CJK);
    the 3-char minimum skips particles, and `+`/non-word chars split tokens
    (so "C++" yields no term by itself but "C++ pitfalls" yields "pitfalls").
    """
    terms: List[str] = []
    for word in re.findall(r"\w[\w-]{2,}", query.lower()):
        if word not in QUERY_STOPWORDS and word not in terms:
            terms.append(word)
        if len(terms) == 2:
            break
    return terms


def _find_hit_line(text: str, terms: List[str]) -> Optional[int]:
    """1-based line within `text` of the first line containing any key term."""
    if not terms:
        return None
    for idx, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(t in low for t in terms):
            return idx
    return None


def _build_digest(
    query: str,
    pages: List[Tuple[FetchResult, str, bool]],
    failures: List[FetchResult],
    report_path: Path,
    quality_stats: Optional[dict] = None,
    start_lines: Optional[List[int]] = None,
    terms: Optional[List[str]] = None,
    line_offset: int = 0,
) -> str:
    """Technical index digest: FULL REPORT path FIRST and LAST, stats, one
    technical line per page (`N. [size] [trunc] @line L @hit H — Title — URL`).

    start_lines: 1-based `=== <url> ===` header lines relative to the report
    content; line_offset: lines the digest + separator occupy at the top of the
    final file — so @line/@hit values are absolute in the written report file.
    The digest is small by design (~25 lines): nothing to trim.

    quality_stats (from F5/F7) is appended to the stats line as extra
    counters; the base format is unchanged so downstream consumers keep working.
    """
    total_chars = sum(len(text) for _, text, _ in pages)
    reasons = list(dict.fromkeys(r.error for r in failures if r.error))
    reason_part = f" ({', '.join(reasons)})" if reasons else ""
    quality_part = ""
    if quality_stats:
        qparts = []
        if quality_stats.get("farm_dropped"):
            qparts.append(f"farm-dropped: {quality_stats['farm_dropped']}")
        if quality_stats.get("stub_dropped"):
            qparts.append(f"stub-dropped: {quality_stats['stub_dropped']}")
        if quality_stats.get("stale_dropped"):
            qparts.append(f"stale-dropped: {quality_stats['stale_dropped']}")
        # Always visible (even at 0): pages dropped below the 50-char floor by
        # cross-page dedup used to be completely silent; hiding the counter at
        # 0 would recreate that silence in the common case. Appended; existing
        # counters unchanged (backward-compatible).
        qparts.append(f"dedup-dropped: {quality_stats.get('dedup_dropped', 0)}")
        if qparts:
            quality_part = " | " + " | ".join(qparts)
    full_report_line = f"FULL REPORT: {report_path} — grep or read this file for full content"
    lines = [
        full_report_line,
        f"RESEARCH: {query}",
        f"Results: {len(pages)} fetched, {len(failures)} failed{reason_part} | {total_chars:,} chars{quality_part} | ordered by relevance",
    ]
    for i, (r, text, truncated) in enumerate(pages, 1):
        size = f"[{_human_chars(len(text))}{' trunc' if truncated else ''}]"
        base = start_lines[i - 1] + line_offset if start_lines else 0
        hit = _find_hit_line(text, terms) if terms else None
        hit_part = f" @hit {base + hit}" if hit else ""
        if r.title:
            lines.append(f"{i}. {size} @line {base}{hit_part} — {r.title} — {r.url}")
        else:
            lines.append(f"{i}. {size} @line {base}{hit_part} — {r.url}")
    lines.append(full_report_line)
    return "\n".join(lines)


def _assemble_report(
    query: str,
    pages: List[Tuple[FetchResult, str, bool]],
    failures: List[FetchResult],
    report_path: Path,
    quality_stats: Optional[dict],
) -> Tuple[str, str]:
    """Return (digest, report_content) for one run.

    The report file = the digest (index) + a `---` separator + the page
    content, so the artifact is self-contained: the stdout digest and the file
    head are identical, and @line/@hit values are absolute in the final file.
    The offset (digest lines + separator) is computed from a probe digest —
    line COUNT does not depend on the values, so the second build is exact.
    """
    content, start_lines = _build_report_file(pages)
    terms = _query_key_terms(query)
    probe = _build_digest(query, pages, failures, report_path, quality_stats, start_lines, terms, 0)
    offset = len(probe.splitlines()) + 1  # +1 = the `---` separator line
    digest = _build_digest(query, pages, failures, report_path, quality_stats, start_lines, terms, offset)
    return digest, digest + "\n---\n" + content


def _write_report_file(path: Path, content: str) -> bool:
    """Write the report file. Returns False on failure (caller falls back to inline raw)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


# =============================================================================
# MAIN ENTRY POINTS
# =============================================================================

def run_research(config: ResearchConfig) -> Optional[List[FetchResult]]:
    """Execute research and output results."""
    progress = ProgressReporter(quiet=config.quiet)

    # Batch mode: collect all results, then format
    results: List[FetchResult] = []
    _wall_t0 = time.monotonic()

    async def collect_async():
        try:
            async for result in run_research_async(config, progress):
                results.append(result)
        finally:
            _shutdown_extract_pool()

    # Whole-run bound for the async core on ALL platforms (Windows has no
    # SIGALRM, so the Unix watchdog in main() does not apply there). This
    # covers every documented unbounded vector: hung ProcessPoolExecutor
    # extraction workers, the synchronous Scrapling fallback, and all gather
    # points. On timeout the inner task is cancelled promptly even when a pool
    # worker is stuck; we then os._exit(1) exactly like the SIGALRM watchdog —
    # a plain exit could hang interpreter teardown joining the hung worker.
    try:
        asyncio.run(asyncio.wait_for(collect_async(), timeout=WALL_TIMEOUT))
    except asyncio.TimeoutError:
        log_usage({
            "query": config.query, "mode": "search",
            "urls_searched": 0, "urls_fetched": 0, "content_chars": 0,
            "ok": False, "error": "wall-clock timeout",
            "ms": int((time.monotonic() - _wall_t0) * 1000), "timeout": True,
            "short_pages": 0, "domains": [],
        })
        print(f"\nwall-clock timeout ({WALL_TIMEOUT}s) — exiting", file=sys.stderr)
        os._exit(1)  # kills child processes (ProcessPoolExecutor workers)

    return results


def main() -> None:
    """Main entry point."""
    # Force UTF-8 stdout on Windows (avoids cp1251/charmap encoding errors)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Web Research Tool - Search + Fetch with TLS fingerprinting (Scrapling)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_research.py "Mac Studio M3 Ultra LLM performance"
  python web_research.py --url https://example.com   # Fetch one URL: full page saved to a report file, path printed
  python web_research.py --url https://example.com --no-render  # Pure static fetch (no browser)

Search: DDG primary + Brave fallback (set BRAVE_API_KEY env var or ~/.config/brave/api_key)
Fetch: Scrapling AsyncFetcher (TLS fingerprinting); browser rendering (headless Chromium shell — chromium-headless-shell; official Google build on macOS/Windows, bundled-libs build on Linux; uv-managed, user-cache only, headless/background) auto-retries failed fetches for JS pages
Extract: trafilatura > regex > Scrapling DOM parser (tiered fallback)
Full page saved to tmp/webresearch/, path printed to stdout
Blocked domains: facebook.com, tiktok.com, instagram.com, linkedin.com, youtube.com, msn.com, forbes.com, edmunds.com, cars.com, nytimes.com, percona.com, mctlaw.com, zenodo.org, amjmed.com, dl.acm.org, nejm.org, cell.com, sciencedirect.com, onlinelibrary.wiley.com, reddit.com
        """
    )

    parser.add_argument("query", nargs="?", help="Search query (omit if using --url)")
    parser.add_argument("-u", "--url", nargs="+", metavar="URL",
                        help="Fetch one URL directly: full page saved to a report file (skip search)")
    parser.add_argument("--no-render", action="store_true",
                        help="Disable browser rendering entirely (pure static path)")
    parser.add_argument("--usage", action="store_true",
                        help="Show usage statistics (last 30 days)")
    parser.add_argument("--sci", action="store_true",
                        help="Enable scientific bonus sources (arXiv, OpenAlex)")
    parser.add_argument("--med", action="store_true",
                        help="Enable medical bonus sources (PubMed, Europe PMC, OpenAlex)")
    parser.add_argument("--tech", action="store_true",
                        help="Enable tech bonus sources (Hacker News, Stack Overflow, Dev.to, GitHub)")
    parser.add_argument("--quality", action="store_true",
                        help="Include output quality analysis (with --usage)")

    args = parser.parse_args()

    if args.usage or args.quality:
        print_usage_stats(quality=args.quality)
        sys.exit(0)

    # URL-fetch mode: ONE URL per invocation. Fetch the page in FULL (no char
    # cap), apply quality filters, write the full plain text to its own report
    # file, print ONLY the absolute path of that file to stdout.
    if args.url:
        if len(args.url) > 1:
            parser.error("only one URL per invocation")
        url = args.url[0]

        # Render mode: default auto (browser only when static fetch fails);
        # --no-render disables it entirely. WEB_RESEARCH_NO_BROWSER=1 overrides
        # everything (pure static path).
        if os.environ.get("WEB_RESEARCH_NO_BROWSER") == "1":
            render_mode = "off"
        elif args.no_render:
            render_mode = "off"
        else:
            render_mode = "auto"

        _ensure_report_dir()

        # Browser preflight runs OUTSIDE the timed block: the first headless
        # Chromium shell fetch (~100-110MB) can take minutes and must not
        # count against the wall clock. Fetch is one-time; availability is
        # cached.
        if render_mode != "off":
            _ensure_browser()
        global _BROWSER_ESCALATIONS
        _BROWSER_ESCALATIONS = 0
        progress = ProgressReporter()

        async def _fetch_url_single(u: str) -> FetchResult:
            return await fetch_single_async(
                u, DEFAULT_TIMEOUT, 100, None, progress=progress, render=render_mode,
            )

        t0 = time.monotonic()
        try:
            # Whole-run bound: --url shares the exact extraction vectors of
            # search mode (hung pool worker / stuck Scrapling parse), and this
            # branch exits before the SIGALRM watchdog below is reached, so no
            # other wall-clock bound applies to it.
            result = asyncio.run(asyncio.wait_for(_fetch_url_single(url), timeout=WALL_TIMEOUT))
        except asyncio.TimeoutError:
            log_usage({
                "query": "", "mode": "url-fetch",
                "urls_searched": 0, "urls_fetched": 0,
                "content_chars": 0, "ok": False,
                "error": "wall-clock timeout",
                "ms": int((time.monotonic() - t0) * 1000), "timeout": True,
            })
            print(f"\nwall-clock timeout ({WALL_TIMEOUT}s) — exiting", file=sys.stderr)
            os._exit(1)  # kills child processes (ProcessPoolExecutor workers)
        except KeyboardInterrupt:
            sys.exit(130)

        if not result.success:
            log_usage({
                "query": "", "mode": "url-fetch", "urls_searched": 0,
                "urls_fetched": 0, "content_chars": 0,
                "ok": False, "error": result.error,
                "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
                **_quality_fields([result]),
            })
            print(f"Failed to fetch {url}: {result.error}", file=sys.stderr)
            sys.exit(1)

        # Full-page text: F4 + F1 quality filters, original order, no length cap.
        # Fail-soft: never empties the page (<500-char guard returns original).
        text = _filter_page_text(result.content)
        path = _report_dir() / (_make_run_id(url) + ".txt")
        try:
            path.write_text(f"=== {url} ===\n\n{text}\n", encoding="utf-8")
        except OSError as e:
            log_usage({
                "query": "", "mode": "url-fetch", "urls_searched": 0,
                "urls_fetched": 1, "content_chars": len(text),
                "ok": False, "error": f"write failed: {e}",
                "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
                **_quality_fields([result]),
            })
            print(f"Failed to write report file {path}: {e}", file=sys.stderr)
            sys.exit(1)
        log_usage({
            "query": "", "mode": "url-fetch", "urls_searched": 0,
            "urls_fetched": 1, "content_chars": len(text),
            "ok": True, "error": None,
            "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
            **_quality_fields([result]),
        })
        # Exactly ONE line on stdout: the absolute path of the saved file.
        print(f"Full web page saved at: {path}")
        sys.exit(0)

    if not args.query:
        parser.error("query is required (or use --url for direct fetch)")

    # Prepare report dir at startup: mkdir + rotation (both fail-soft)
    _ensure_report_dir()

    # NOTE: search mode is static-only by design (benchmarked: browser
    # escalation cost +44% fetch time and rescued ~0-2 pages — the browser
    # value is --url single-page rendering). No browser preflight here.

    queries = [args.query]

    def make_config(query: str) -> ResearchConfig:
        return ResearchConfig(
            query=query,
            scientific=args.sci,
            medical=args.med,
            tech=args.tech,
        )

    # Hard wall-clock timeout: kill the entire process after WALL_TIMEOUT.
    # Unix belt (SIGALRM) — bounds the WHOLE process including the
    # post-processing phase outside run_research (quality filters, report
    # build), which wait_for inside run_research does not cover. The wait_for
    # bound covers the async core on ALL platforms (incl. Windows, which has
    # no SIGALRM). Both use the same WALL_TIMEOUT value; whichever fires first
    # on Unix produces identical behavior (message + os._exit(1)).
    import signal
    _wall_t0 = time.monotonic()
    def _timeout_handler(signum, frame):
        for q in queries:
            log_usage({
                "query": q, "mode": "search",
                "urls_searched": 0, "urls_fetched": 0, "content_chars": 0,
                "ok": False, "error": "wall-clock timeout",
                "ms": int((time.monotonic() - _wall_t0) * 1000), "timeout": True,
                "short_pages": 0, "domains": [],
            })
        print(f"\nwall-clock timeout ({WALL_TIMEOUT}s) — exiting", file=sys.stderr)
        os._exit(1)  # kills child processes (ProcessPoolExecutor workers)
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(WALL_TIMEOUT)

    try:
        # Single query: original behavior
        config = make_config(queries[0])
        t0 = time.monotonic()
        results = run_research(config)
        ok = [r for r in (results or []) if r.success]
        # Telemetry fields are computed from the PRE-filter fetch results
        # (unchanged semantics); only "ok" below reflects post-filter survival.
        fetched_chars = sum(len(r.content) for r in (results or []))
        quality_fields = _quality_fields(results)
        pages: List[Tuple[FetchResult, str, bool]] = []
        failures: List[FetchResult] = []
        if results:
            # F5 first: page-level farm/syndication detection must see the FULL
            # bodies (sentence dedup would gut near-identical copies and destroy
            # the page-level Jaccard signal). Stub drop runs alongside it. Then
            # the existing sentence dedup.
            quality_stats: dict = {"farm_dropped": 0,
                                   "stale_dropped": 0,
                                   "stub_dropped": 0, "dedup_dropped": 0}
            if QUALITY_FILTERS_ENABLED:
                results, stub_st = _drop_stub_pages(results)
                quality_stats["stub_dropped"] += stub_st.get("stub_dropped", 0)
                results, f5_st = _dedup_farm_pages(results)
                quality_stats["farm_dropped"] += f5_st.get("farm_dropped", 0)
            results, dedup_st = _dedup_results(results)
            # Count pages the dedup shrank below the 50-char floor (previously
            # only a debug log; I7: make the drop observable in the digest).
            quality_stats["dedup_dropped"] = dedup_st.pages_dropped
            if QUALITY_FILTERS_ENABLED:
                results, f7_st = _recency_filter(results, config.query)
                quality_stats.update(f7_st)
            results, _f3_st = _dedup_cross_page_sentences(results)
            # Digest + report-file output: the file holds the full filtered text
            # (grep-safe), stdout carries only the compact digest. If the file
            # cannot be written, fall back to the old inline raw output.
            pages = _prepare_report_pages(results)
            failures = [r for r in results if not r.success]
        log_usage({
            "query": config.query, "mode": "search",
            "urls_fetched": len(ok),
            "content_chars": fetched_chars,
            "ok": bool(pages), "error": None,
            "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
            **quality_fields,
        })
        if pages:
            report_path = _report_dir() / f"{_make_run_id(config.query)}.txt"
            digest, report_content = _assemble_report(config.query, pages, failures, report_path, quality_stats)
            if _write_report_file(report_path, report_content):
                print(digest)
                # Redundant path on stderr: harnesses capture both streams, and
                # the path must survive any stdout trimming.
                print(f"FULL REPORT: {report_path}", file=sys.stderr)
            else:
                print(format_batch_raw(results))
        elif ok:
            # Every fetched page was dropped downstream (quality filters
            # F5/F7, stub/sentence dedup) or filtered to empty text. Exit
            # non-zero — matching --url mode — instead of the old silent exit-0.
            print(
                f"No results: all {len(ok)} fetched pages were dropped by quality filters "
                "(farm/recency/stub/dedup) or filtered to empty text",
                file=sys.stderr,
            )
            sys.exit(1)
        elif results:
            # Every fetched page failed (fetch errors, HTTP 403, CAPTCHA...).
            print(f"No results: all {len(results)} fetched pages failed", file=sys.stderr)
            sys.exit(1)
        else:
            print("No results found", file=sys.stderr)
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        # Output pipe closed (e.g. piped to head) — not a real error
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except Exception as e:
        log_usage({
            "query": queries[0] if queries else "", "mode": "search",
            "urls_fetched": 0, "content_chars": 0,
            "ok": False, "error": str(e)[:200],
            "ms": int((time.monotonic() - _wall_t0) * 1000), "timeout": False,
        })
        logger.exception(f"Research failed: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
