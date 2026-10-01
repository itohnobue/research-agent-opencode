#!/bin/bash
# Wrapper for browser_fetch.py — the real-browser fetch tier (--url-chrome).
# macOS / Linux / MSYS (Git Bash) on Windows.
#
# One browser only: real Google Chrome, provisioned repo-local into tmp/browser/chrome
# (macOS .dmg, Linux .deb, Windows offline installer + 7zr) — a system browser is never
# used, so every host behaves identically. Chromium and Chrome-for-Testing are measurably
# detected (rebrowser-bot-detector, 2026-10-01: only real Chrome carries the
# "Google Chrome" client-hint brand) — they are not used at all.
#
# Graceful degradation: if Chrome is unavailable (unsupported platform, download
# blocked, missing libraries), the request is served by the ORDINARY static --url path
# instead of failing. Nothing is installed system-wide except Chrome's own shared
# libraries on Linux, which install_chrome() adds when it runs as root.
#
# Modes:
#   <urls...>   fetch these pages with Chrome
#   --ensure    provision the tier only, fetch nothing — exit 0 if Chrome is ready,
#               3 if it is not. Used by web_research.py's --url preflight, which runs
#               it BEFORE its wall-clock block (a first provisioning takes minutes).
#
# Env:
#   BROWSER_FETCH_NO_FALLBACK=1   never run the static fallback; exit 3 instead. Set by
#               web_research.py's escalation, which already holds the static result.
#
#   tmp/uv/               uv itself (bootstrapped on first run, retried once)
#   tmp/browser/chrome/   real Google Chrome (provisioned on first run)
#   tmp/browser/profile/  persistent browser profile (cookies, history)
#   tmp/webresearch/      report files (shared with the search tool)

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/../../" && pwd )"

# ---------------------------------------------------------------- uv (repo-local)
UV_DIR="$REPO_ROOT/tmp/uv"
UV_BIN="$UV_DIR/uv"
install_uv() {
    echo "Installing uv to $UV_DIR ..." >&2
    mkdir -p "$UV_DIR"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh
}
if ! "$UV_BIN" --version >/dev/null 2>&1; then
    install_uv
    if ! "$UV_BIN" --version >/dev/null 2>&1; then
        echo "uv verification failed; retrying once ..." >&2
        install_uv
    fi
fi

# --------------------------------------------------------- Chrome (repo-local)
BROWSER_DIR="$REPO_ROOT/tmp/browser"
CHROME_DIR="$BROWSER_DIR/chrome"
# Per-run staging directory: never shared, so a takeover run cannot collide with the
# staging of a run still in flight (the swap into CHROME_DIR is what must be atomic).
STAGE_DIR="$BROWSER_DIR/.chrome-stage.$$"

chrome_exe() {
    # Repo-local only — tmp/browser/chrome, provisioned by install_chrome() below.
    # A system Google Chrome is never used: the tier must behave the same on every host.
    # MINGW/MSYS: the Windows payload lives at Chrome-bin/chrome.exe (provisioned by
    # browser_fetch.bat — this shell detects it but does not provision it).
    case "$(uname -s)" in
        Darwin) echo "$CHROME_DIR/Google Chrome.app/Contents/MacOS/Google Chrome" ;;
        Linux)  echo "$CHROME_DIR/opt/google/chrome/chrome" ;;
        MINGW*|MSYS*|CYGWIN*) echo "$CHROME_DIR/Chrome-bin/chrome.exe" ;;
        *)      echo "" ;;
    esac
}
chrome_ok() { local e; e="$(chrome_exe)"; [ -n "$e" ] && [ -x "$e" ]; }

install_chrome() {
    # Provision into a sibling staging dir and swap it in only on success: an
    # interrupted download/extract must never leave a payload that passes the
    # readiness check (a partial .app can still carry the exec bit on macOS).
    local stage="$STAGE_DIR"
    rm -rf "$stage"
    case "$(uname -s)" in
        Darwin)
            echo "Provisioning Google Chrome into $CHROME_DIR ..." >&2
            local dmg="$BROWSER_DIR/chrome.dmg" mnt="$BROWSER_DIR/.dmg-mnt"
            curl -fL --retry 3 -o "$dmg" \
                "https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg" || { rm -rf "$stage"; return 1; }
            rm -rf "$mnt"; mkdir -p "$mnt"
            hdiutil attach -nobrowse -quiet "$dmg" -mountpoint "$mnt" || { rm -rf "$stage"; return 1; }
            mkdir -p "$stage"
            cp -R "$mnt/Google Chrome.app" "$stage/" || { hdiutil detach -quiet "$mnt"; rm -rf "$stage"; return 1; }
            hdiutil detach -quiet "$mnt" || true
            rm -f "$dmg"
            xattr -dr com.apple.quarantine "$stage/Google Chrome.app" 2>/dev/null || true
            # Swap only once the whole bundle is staged.
            rm -rf "$CHROME_DIR"
            mkdir -p "$CHROME_DIR"
            mv "$stage/Google Chrome.app" "$CHROME_DIR/" || { rm -rf "$stage"; return 1; }
            rm -rf "$stage"
            ;;
        Linux)
            echo "Provisioning Google Chrome into $CHROME_DIR ..." >&2
            local deb="$BROWSER_DIR/chrome.deb" arch debarch
            arch="$(uname -m)"
            debarch="amd64"; [ "$arch" = "aarch64" ] && debarch="arm64"
            curl -fL --retry 3 -o "$deb" \
                "https://dl.google.com/linux/direct/google-chrome-stable_current_${debarch}.deb" || { rm -rf "$stage"; return 1; }
            mkdir -p "$stage"
            dpkg-deb -x "$deb" "$stage" || { rm -f "$deb"; rm -rf "$stage"; return 1; }
            rm -f "$deb"
            # Chrome needs system libraries; add them when we may (containers run as root).
            if [ "$(id -u 2>/dev/null)" = "0" ] && command -v apt-get >/dev/null 2>&1; then
                "$UV_BIN" run --no-project --with playwright playwright install-deps chromium >&2 || true
                apt-get install -y --no-install-recommends libgtk-3-0 libxss1 >&2 || true
            fi
            # Swap only once the extraction succeeded (a single rename — atomic on one fs).
            rm -rf "$CHROME_DIR"
            mv "$stage" "$CHROME_DIR" || { rm -rf "$stage"; return 1; }
            ;;
        *)
            echo "note: no automated Chrome provisioning in this shell." >&2
            echo "      On Windows run browser_fetch.bat — it unpacks Google's offline" >&2
            echo "      installer into tmp/browser/chrome (7zr); nothing system-wide." >&2
            return 1
            ;;
    esac
}

release_lock() {
    # Only the creating run may release the lock: a takeover may have replaced the
    # directory, and the interrupted holder's trap must not remove the new owner's lock.
    if [ -f "$LOCK/pid" ] && [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ]; then
        rm -f "$LOCK/pid"
        rmdir "$LOCK" 2>/dev/null || true
    fi
}
acquire_lock() {
    mkdir "$LOCK" 2>/dev/null || return 1
    printf '%s' "$$" > "$LOCK/pid" 2>/dev/null || true
    return 0
}

if ! chrome_ok; then
    mkdir -p "$BROWSER_DIR"
    LOCK="$BROWSER_DIR/.chrome.lock"
    # shellcheck disable=SC2064
    trap 'release_lock; rm -rf "$STAGE_DIR"' EXIT
    if acquire_lock; then
        install_chrome || true
        chrome_ok || { echo "Chrome verification failed; retrying once ..." >&2; install_chrome || true; }
        release_lock
    else
        echo "another Chrome provisioning run is in progress; waiting for it ..." >&2
        i=0
        while [ $i -lt 120 ] && ! chrome_ok; do sleep 5; i=$((i + 1)); done
        if ! chrome_ok; then
            # The holder failed or died: reclaim the stale lock and take over once.
            echo "the provisioning run did not finish; taking over the stale lock ..." >&2
            rm -rf "$LOCK" 2>/dev/null || true
            if acquire_lock; then
                install_chrome || true
                release_lock
            fi
        fi
    fi
fi

# --------------------------------------------------- graceful degradation (static --url)
URLS=()
ENSURE_ONLY=0
for a in "$@"; do
    case "$a" in
        http://*|https://*) URLS+=("$a") ;;
        --ensure) ENSURE_ONLY=1 ;;
    esac
done

# Provisioning-only mode: install the tier if it is missing, fetch nothing.
if [ "$ENSURE_ONLY" = "1" ]; then
    if chrome_ok; then exit 0; fi
    echo "note: Google Chrome is unavailable on this platform — browser tier not installed" >&2
    exit 3
fi

# BROWSER_FETCH_NO_FALLBACK=1: the caller already holds the static result, so never
# re-run the static fetch — report the tier failure as exit 3 and let it stand.
NO_FALLBACK="${BROWSER_FETCH_NO_FALLBACK:-0}"

run_static_fallback() {
    echo "note: serving this request with the ordinary static --url fetch" >&2
    # --no-render: this fallback IS the static path — it must never escalate back into
    # the browser tier (a nested attempt would recurse while Chrome is missing).
    # web_research.py --url takes exactly one URL, so fetch them one at a time.
    local rc=0 u
    for u in "${URLS[@]}"; do
        env -u PYTHONPATH PYTHONIOENCODING=utf-8 "$UV_BIN" run --no-project \
            "$SCRIPT_DIR/web_research.py" --url "$u" --no-render || rc=$?
    done
    return $rc
}

if ! chrome_ok; then
    if [ "$NO_FALLBACK" = "1" ]; then
        echo "error: Google Chrome is not available (provisioning failed or unsupported platform)" >&2
        exit 3
    fi
    if [ ${#URLS[@]} -gt 0 ]; then
        echo "note: Google Chrome is not available (provisioning failed or unsupported platform)" >&2
        run_static_fallback
        exit $?
    fi
    echo "error: Google Chrome is not available and no URL was given to fall back on" >&2
    exit 3
fi

# Run with inline dependencies (PEP 723 metadata in browser_fetch.py)
env -u PYTHONPATH PYTHONIOENCODING=utf-8 "$UV_BIN" run --no-project "$SCRIPT_DIR/browser_fetch.py" "$@"
rc=$?
# Exit 3 = the browser tier could not run at all (payload/libraries/download). A
# blocked page (a refusal status; exit 2) is an honest result and is NOT re-fetched.
if [ $rc -eq 3 ] && [ ${#URLS[@]} -gt 0 ] && [ "$NO_FALLBACK" != "1" ]; then
    run_static_fallback
    exit $?
fi
exit $rc
