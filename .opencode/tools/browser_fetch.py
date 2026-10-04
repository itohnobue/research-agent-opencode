#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
# -*- coding: utf-8 -*-
"""
Real-browser page fetch (`--url-chrome`) for the web-research tool.

Third tier of the fetch ladder: search -> `--url` (static, with a Chrome retry for
failures it can recognise) -> `--url-chrome` (real Google Chrome, for pages behind JS
anti-bot gates).

Why it exists: measured 2026-10-01 — `--url https://www.ozon.ru/` returns HTTP 403 with
no content, while the same page fetched by a real Google Chrome comes back complete.
The decisive tell was the `HeadlessChrome` UA token, so this tool never sends it, and it
keeps every other observable consistent (platform-matched UA, matching locale/timezone,
real window metrics, a persistent aged profile).

Everything the tool owns is repo-local: the browser payload lives in <repo>/tmp/browser/
(provisioned by browser_fetch.sh / .bat — macOS .dmg, Linux .deb, the Windows offline
installer; a system browser is never used, on any platform) and the profile in
<repo>/tmp/browser/profile/ (same scheme as tmp/uv/). Reports land in
<repo>/tmp/webresearch/ next to the search tool's reports. On Linux the wrapper also
installs Chrome's shared libraries when running as root — the one thing that cannot live
in tmp/.

Settling is adaptive: `--wait` is the minimum after load and the tier keeps waiting while the
rendered text is still changing (up to `--max-wait`, default 30 s), so a JS shell that fills
in late is captured without guessing a sleep; `--scroll` adds a scroll pass for lazy lists.

Outcome: the verdict is mechanical, never wording-based — `blocked` when the server answered a
refusal status (401/403/406/429/451/503) with a SHORT body, `error` when the page rendered
no text at all or the status was another >=400, otherwise `ok`. A report is written for every
outcome except a navigation timeout or a failed launch (stderr carries the reason; exit 2 for a
timeout, 3 when the tier could not run — the wrapper then serves the static fetch). Only `ok`
is answerable. A wall served with HTTP 200 is mechanically indistinguishable from content, so
the caller judges it from the text: that is what `--url-chrome` is documented for.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TMP_DIR = REPO_ROOT / "tmp"
REPORT_DIR = TMP_DIR / "webresearch"
BROWSER_DIR = TMP_DIR / "browser"
PROFILE_DIR = BROWSER_DIR / "profile"
SHOT_DIR = BROWSER_DIR / "screenshots"

REPORT_MAX_AGE_DAYS = 7
REPORT_MAX_FILES = 30

# The persistent profile is single-instance: overlapping invocations make Chrome abort with
# "Failed to create a ProcessSingleton ... File exists" on SingletonLock. A launch that
# fails before it fetched anything is retried (see _run_browser) — the other invocation
# releases the profile within seconds.
_PROFILE_RETRY_ATTEMPTS = 4
_PROFILE_RETRY_DELAY = 5   # seconds between attempts

# Below this many characters of rendered text a page is treated as "possibly still
# rendering" and waited out to --max-wait (see _settle). 200 leaves room for genuinely
# small pages (a login screen, a stub) while catching JS shells.
THIN_TEXT_CHARS = 200

# A refusal page states the refusal and nothing else: every wall measured on 2026-10-01 was
# 190-740 characters of text. Below this many characters a >=400 status is read as a block;
# at or above it the body is content — real sites have been measured answering 4xx *with*
# thousands of characters of real listings, so length wins over the status.
GATE_MAX_CHARS = 2000

# Set in main() when Playwright is imported (lazy: the module must import without it).
# Lets _fetch_one label a Playwright timeout with the documented "timeout" outcome.
_PW_TIMEOUT_EXC: tuple = ()

UA_TEMPLATE = {
    "Darwin": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
    "Linux": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
    "Windows": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
}

# Statuses a WAF answers with when it refuses a client. These are protocol-level constants,
# not site wording: the classifier carries no phrases at all, so it cannot rot when a site
# rewords its block page. Any other >=400 status is reported as a plain error.
REFUSAL_STATUSES = frozenset({401, 403, 406, 429, 451, 503})


def _env_locale() -> str:
    raw = os.environ.get("BROWSER_LOCALE") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    m = re.match(r"([a-zA-Z]{2,3})[_-]([A-Za-z]{2})", raw)
    return f"{m.group(1).lower()}-{m.group(2).upper()}" if m else "en-US"


def _env_timezone() -> str | None:
    return os.environ.get("BROWSER_TZ") or os.environ.get("TZ") or None


# Repo-local Google Chrome (provisioned into tmp/browser/chrome by browser_fetch.sh /
# .bat). A system install is NEVER used: every host runs the same payload from tmp/.
PROVISIONED_CHROME: dict = {
    "Darwin": ["Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Linux": ["opt/google/chrome/chrome"],
    # Windows unpacks the offline installer: Chrome-bin/**/chrome.exe (globbed below).
    "Windows": ["Chrome-bin/chrome.exe"],
}


def _provisioned_chrome() -> Path | None:
    """Real Chrome provisioned into tmp/browser/chrome (self-contained; never a system
    install). The Windows payload nests chrome.exe under a versioned directory, so the
    known paths are tried first and the tree is globbed after."""
    root = BROWSER_DIR / "chrome"
    for rel in PROVISIONED_CHROME.get(_platform.system(), []):
        p = root / rel
        if p.exists():
            return p
    if _platform.system() == "Windows":
        try:
            found = sorted(root.glob("**/chrome.exe"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            found = []
        if found:
            return found[0]
    return None


def _engine() -> dict:
    """The only browser we run: the repo-local real Google Chrome (tmp/browser/chrome).

    No system install and no other engine: the wrapper provisions this payload on every
    platform, and a host without it falls back to the ordinary static fetch instead. Real
    Chrome is the only build whose client hints carry the 'Google Chrome' brand —
    Chromium and Chrome-for-Testing are measurably flagged (rebrowser-bot-detector,
    2026-10-01).
    """
    candidate = _provisioned_chrome()
    if candidate is None:
        raise RuntimeError(
            "no provisioned Google Chrome in tmp/browser/chrome (a system browser is "
            "never used; the wrapper installs the payload on first use)")
    try:
        out = subprocess.run([str(candidate), "--version"], capture_output=True,
                             text=True, timeout=20)
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out.stdout + out.stderr)
        if m:
            return {"path": str(candidate), "name": "Chrome", "origin": "tmp",
                    "major": m.group(1)}
        detail = (out.stderr or out.stdout or "").strip().splitlines()
        reason = detail[0][:160] if detail else f"exit status {out.returncode}"
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"[:160]
    raise RuntimeError(
        f"Google Chrome at {candidate} is present but failed to start ({reason}) — on "
        "Linux its system libraries are likely missing; run browser_fetch.sh --ensure "
        "to reinstall them")


def _realistic_ua(major: str) -> str:
    system = _platform.system()
    tmpl = UA_TEMPLATE.get(system, UA_TEMPLATE["Linux"])
    return tmpl.format(major=major)


def _classify(text: str, status: int | None = None) -> str:
    """ok | blocked | error — decided mechanically, from the status and the text.

    No site-specific wording is involved, so nothing here rots when a site rewords its
    block page:

    * a refusal status with a SHORT body is a block page — the server answered with its
      notice instead of the page (every wall measured on 2026-10-01 was 190-740 chars).
      The length guard matters in the other direction too: real sites have been measured
      answering 4xx *with* thousands of characters of genuine content, so the status alone
      must never decide a long page — that is content.
    * a page that rendered no text at all is an error: nothing came back.
    * everything else is ok, including a wall served with HTTP 200. No mechanical rule can
      tell those from content, so the caller judges them from the text itself — which is
      exactly what the `--url-chrome` force flag is documented for.
    """
    if not text.strip():
        return "error"
    if status is not None and status >= 400:
        if len(text) >= GATE_MAX_CHARS:
            return "ok"
        return "blocked" if status in REFUSAL_STATUSES else "error"
    return "ok"


def _slug(url: str, limit: int = 60) -> str:
    s = re.sub(r"^https?://", "", url).split("?")[0]
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return (s or "page")[:limit]


def _rotate() -> None:
    try:
        files = sorted(REPORT_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    cutoff = time.time() - REPORT_MAX_AGE_DAYS * 86400
    for i, p in enumerate(files):
        try:
            if i >= REPORT_MAX_FILES or p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _write_report(path: Path, header: dict, body: str) -> None:
    lines = [f"=== {header['url']} ==="]
    lines += [f"{k}: {v}" for k, v in header.items() if k != "url"]
    lines += ["", body.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _settle(page, minimum_s: int, cap_s: int) -> int:
    """Wait `minimum_s`, then keep waiting while the page is still short of content.

    A fixed sleep has to guess how long a JS-heavy page needs; this settles instead, by
    sampling the rendered text once a second. Two rules end the wait:

    * the text has been unchanged for two samples *and* is not thin (a page that is still
      streaming content keeps growing, so it is followed), or
    * `cap_s` is reached — a live feed or an infinite scroller can never hold the fetch
      open indefinitely, and a page that stays nearly empty (a shell still waiting on a
      timer or a request) is waited out rather than silently reported as final.

    `cap_s` 0 disables the extension entirely (the old fixed-sleep behaviour). Returns the
    seconds actually spent, which the report header records.
    """
    t0 = time.time()
    time.sleep(max(0, minimum_s))
    try:
        prev = len(page.inner_text("body"))
    except Exception:  # noqa: BLE001
        return int(time.time() - t0)
    stable = 0
    while cap_s > 0 and time.time() - t0 < cap_s and (prev < THIN_TEXT_CHARS or stable < 2):
        time.sleep(1.0)
        try:
            now = len(page.inner_text("body"))
        except Exception:  # noqa: BLE001
            break
        stable = stable + 1 if now == prev else 0
        prev = now
    return int(time.time() - t0)


def _fresh_tab(ctx, page, args):
    """Close the current tab and open a clean one (per-URL isolation, see _fetch_all)."""
    try:
        page.close()
    except Exception:  # noqa: BLE001
        pass
    fresh = ctx.new_page()
    fresh.set_default_timeout(args.timeout * 1000)
    return fresh


def _fetch_one(page, url: str, wait_s: int, max_wait_s: int, scroll: bool, screenshot: bool,
               timeout_ms: int, run_id: str, idx: int, ua: str, browser_label: str) -> dict:
    rec = {"url": url, "outcome": "error", "error": None, "ua": ua, "browser": browser_label}
    t0 = time.time()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        rec["status"] = resp.status if resp else None
        rec["settle"] = _settle(page, wait_s, max_wait_s)
        if scroll:
            for _ in range(6):
                page.mouse.wheel(0, 1400)
                time.sleep(1.0)
            # Lazy lists load as they scroll: let the last batch land too.
            rec["settle"] += _settle(page, 1, max(0, max_wait_s - rec["settle"]))
        text = page.inner_text("body")
        rec.update(
            final_url=page.url,
            title=page.title(),
            outcome=_classify(text, rec.get("status")),
            chars=len(text),
        )
        body = text
        if screenshot:
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            shot = SHOT_DIR / f"{run_id}-{idx}-{_slug(url, 40)}.png"
            try:
                page.screenshot(path=str(shot))
                rec["screenshot"] = str(shot)
            except Exception:  # noqa: BLE001
                pass
        header = {
            "url": url,
            "fetched": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "outcome": rec["outcome"],
            "status": rec.get("status"),
            "title": rec.get("title"),
            "final_url": rec.get("final_url"),
            "browser": rec.get("browser"),
            "ua": rec.get("ua"),
            "chars": rec["chars"],
            "settle": rec.get("settle"),
            "seconds": round(time.time() - t0, 1),
        }
        if rec.get("screenshot"):
            header["screenshot"] = rec["screenshot"]
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"{run_id}-chrome-{idx}-{_slug(url)}.txt"
        _write_report(path, header, body)
        rec["report"] = str(path)
    except Exception as exc:  # noqa: BLE001
        # Playwright's own TimeoutError is the one failure the documented vocabulary
        # distinguishes: a load that ran out of time, not a generic error.
        if _PW_TIMEOUT_EXC and isinstance(exc, _PW_TIMEOUT_EXC):
            rec["outcome"] = "timeout"
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="browser_fetch.py",
        description="Real-browser page fetch (--url-chrome tier). Renders with real "
                    "Google Chrome; use it when search and --url cannot reach a page. "
                    "OUTCOME: ok | blocked | error, decided from the HTTP status and the "
                    "body length (a refusal status with a short body is blocked; no text, "
                    "or another >=400, is error); a timeout or unlaunchable tier writes no "
                    "report (exit 2, or 3 when the tier could not run).")
    ap.add_argument("urls", nargs="+", metavar="URL", help="One or more page URLs")
    # Accepted for symmetry with web_search.sh, which forwards the flag verbatim.
    ap.add_argument("--url-chrome", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--wait", type=int, default=8, metavar="N",
                    help="Minimum seconds to settle after load (default 8)")
    ap.add_argument("--max-wait", type=int, default=30, metavar="N",
                    help="Cap for the adaptive settle: the tier keeps waiting while the "
                         "page is still adding content, up to this many seconds "
                         "(default 30; 0 disables the extension)")
    ap.add_argument("--scroll", action="store_true", help="Scroll to trigger lazy content")
    ap.add_argument("--screenshot", action="store_true", help="Save a PNG next to the report")
    ap.add_argument("--headful", action="store_true", help="Visible window (local debugging)")
    ap.add_argument("--profile-reset", action="store_true", help="Wipe the browser profile first")
    ap.add_argument("--timeout", type=int, default=90, metavar="N",
                    help="Per-page budget in seconds (default 90)")
    args = ap.parse_args()

    if args.profile_reset and PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)

    # Chrome lives in tmp/browser/chrome; nothing else about this tool is system-wide.
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_GC", "1")

    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        global _PW_TIMEOUT_EXC
        _PW_TIMEOUT_EXC = (PWTimeout,)

        run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        results: list[dict] = []
        _run_browser(args, sync_playwright, run_id, results)
    except Exception as exc:  # noqa: BLE001
        # Browser tier unavailable: no payload, no libraries, no display, download
        # blocked, Playwright not installable... Exit 3 tells the wrapper to fall back
        # to the ordinary static --url fetch instead of failing the request.
        print(f"note: real-browser tier unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 3

    _rotate()

    for rec in results:
        path = rec.get("report")
        if not path:
            print(f"Failed: {rec['url']}: {rec.get('error')}", file=sys.stderr)
            continue
        print(f"Full web page saved at: {path}")
        print(f"OUTCOME: {rec['outcome']} | title: {rec.get('title')} | "
              f"{rec.get('chars', 0)} chars | {rec.get('final_url')}")
        print(f"FULL REPORT: {path}")

    ok = any(r["outcome"] == "ok" for r in results)
    return 0 if ok else 2


def _run_browser(args, sync_playwright, run_id: str, results: list) -> None:
    """The browser path proper (real Chrome only) — fills `results`; main() reports them.

    The persistent profile is single-instance: Chrome refuses a second process on the same
    --user-data-dir ("Failed to create a ProcessSingleton ... File exists" on SingletonLock)
    and aborts. That happens whenever two invocations overlap — a parallel run, or one that
    is still shutting down — so a launch that fails before any page was fetched is retried a
    few times, which is long enough for the other invocation to release the profile. Only
    exhausting those attempts, or a failure once a page has already landed, raises: main()
    turns that into exit 3 (the wrapper then serves the request from the ordinary static
    fetch).
    """
    for attempt in range(1, _PROFILE_RETRY_ATTEMPTS + 1):
        try:
            _fetch_all(args, sync_playwright, run_id, results)
            return
        except Exception as exc:  # noqa: BLE001
            if results:
                # A browser-level failure after pages had already landed (a dead context
                # while opening the next tab). Keep the pages already fetched and mark the
                # remainder failed: re-raising would discard real content and, via exit 3,
                # send a multi-URL request into a static fallback that takes only one URL.
                first_line = str(exc).splitlines()[0][:140] if str(exc) else type(exc).__name__
                print(f"note: browser run stopped after {len(results)} page(s) "
                      f"({first_line}) — keeping what was fetched", file=sys.stderr)
                done = {r.get("url") for r in results}
                for u in args.urls:
                    if u not in done:
                        results.append({"url": u, "outcome": "error",
                                        "error": f"{type(exc).__name__}: {exc}"})
                return
            if attempt == _PROFILE_RETRY_ATTEMPTS:
                raise
            first_line = str(exc).splitlines()[0][:140] if str(exc) else type(exc).__name__
            print(f"note: browser launch failed ({first_line}) — the profile may be held by "
                  f"another invocation; retrying in {_PROFILE_RETRY_DELAY}s "
                  f"(attempt {attempt + 1}/{_PROFILE_RETRY_ATTEMPTS})", file=sys.stderr)
            time.sleep(_PROFILE_RETRY_DELAY)


def _fetch_all(args, sync_playwright, run_id: str, results: list) -> None:
    """Open ONE persistent context for this invocation and fetch every URL through it."""
    with sync_playwright() as pw:
        engine = _engine()
        ua = _realistic_ua(engine["major"])
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            # Real Google Chrome, provisioned repo-local in tmp/browser/chrome.
            # No Chromium/CfT and no headless shell: both are measurably detected.
            executable_path=engine["path"],
            headless=not args.headful,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1600,900",
                # Containers default to a tiny /dev/shm; without this Chrome can crash on
                # heavy pages (the retired headless shell passed it too).
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            user_agent=ua,
            locale=_env_locale(),
            timezone_id=_env_timezone(),
            viewport={"width": 1600, "height": 900},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(args.timeout * 1000)
            browser_label = f"{engine['name']} {engine['major']} ({engine['origin']})"
            for idx, url in enumerate(args.urls, 1):
                if idx > 1:
                    # One tab per URL. A page that redirects late, keeps loading, or fails
                    # (net::ERR_TIMED_OUT) leaves pending navigations behind, and those
                    # abort the NEXT url's goto ("Navigation ... is interrupted by another
                    # navigation to ..."). A fresh tab has its own navigation state, and
                    # closing the old one cancels whatever it still had in flight.
                    page = _fresh_tab(ctx, page, args)
                rec = _fetch_one(page, url, args.wait, args.max_wait, args.scroll,
                                 args.screenshot, args.timeout * 1000, run_id, idx, ua,
                                 browser_label)
                if rec.get("error") and "interrupted by another navigation" in str(rec["error"]):
                    # Safety net: swap the tab and retry this URL once.
                    page = _fresh_tab(ctx, page, args)
                    rec = _fetch_one(page, url, args.wait, args.max_wait, args.scroll,
                                     args.screenshot, args.timeout * 1000, run_id, idx, ua,
                                     browser_label)
                results.append(rec)
                print(f"[{idx}] {rec['outcome']}: {url}"
                      + (f" — {rec['error']}" if rec.get("error") else
                         f" — {rec.get('title', '')[:60]!r} ({rec.get('chars', 0)} chars)"),
                      file=sys.stderr)
        finally:
            ctx.close()

if __name__ == "__main__":
    sys.exit(main())
