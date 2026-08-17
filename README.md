# Web Search Agent

Deep web search for [OpenCode](https://opencode.ai). Fetches 30 search results and up to 20 pages per query — far beyond the typical 10-20 result limit of built-in tools — and delivers a compact digest plus a full report file you can grep.

## Why use it

Most LLM search tools return 10-20 results, limiting research depth on complex questions. This tool digs deeper — more sources, smarter extraction, quality filtering, better answers. Works with any LLM through OpenCode.

## Quick Start

```bash
git clone https://github.com/itohnobue/research-agent-opencode
cp -R research-agent-opencode/.opencode /path/to/your/project/
cp research-agent-opencode/AGENTS.md /path/to/your/project/
```

If you already have an `AGENTS.md`, append this one instead of overwriting. This teaches OpenCode to route all web searches through this tool. Test it: *"Search for the most performant Rust web frameworks in 2025"*

Auto-installs Python dependencies via `uv` on first run. No API keys required (an optional Brave Search API key deepens coverage).

## Usage

```bash
.opencode/tools/web_search.sh "React server components best practices" --tech
.opencode/tools/web_search.sh "CRISPR delivery methods" --sci --med
.opencode/tools/web_search.sh "Kalman filter implementations" --sci
.opencode/tools/web_search.sh "dummy" --url https://example.com   # direct page fetch (pages only — never file downloads; use curl -L -o for files)
```

| Flag | Sources | Best for |
|------|---------|----------|
| *(none)* | DuckDuckGo + Brave (if key) + DDG News | General web |
| `--tech` | + Hacker News, Stack Overflow, Dev.to, GitHub | Software, DevOps |
| `--sci` | + arXiv, OpenAlex | CS, physics, math, engineering |
| `--med` | + PubMed, Europe PMC, OpenAlex | Medicine, clinical trials |
| `--url` | direct fetch of specific URL(s), skips search | Known-page retrieval only (never file downloads), up to 50k chars per page |

**Fixed tuned defaults** — the tool has no count or format flags: search always fetches 30 results, fetches up to 20 pages, and outputs plain text only. The only flags are the source flags `--sci`/`--med`/`--tech` and `--url` direct fetch — never add count/result-limiting or output-format flags (they do not exist).

**DIGEST + FULL REPORT FILE** — search mode prints a compact digest (stats line, FULL REPORT path, per-page previews) and writes the full filtered text to `tmp/webresearch/<run-id>.txt`. The report file IS the product — read or grep the file at the given path for the content you need (grep by URL or term); the digest is small and must not be trimmed. Do NOT head/tail/grep -m the tool's stdout — nothing to gain, the full content is in the file, not in stdout. For a specific page's fresh content, fetch it directly with `--url` (pages only — never file downloads; `--url` corrupts binaries — use `curl -L -o` for files).

## Key features

- **30 results / up to 20 pages per query** via DuckDuckGo + optional Brave API fallback
- **Anti-bot bypass** — Scrapling with TLS fingerprinting, auto-fallback to httpx
- **Smart extraction** — Trafilatura content-area detection (article body, not sidebars)
- **Quality filters** — content-farm/syndication detection (F5), embedding-based rerank against homonym drift (F6), recency filtering on time-sensitive queries (F7), stub-page drop, cross-page deduplication
- **Non-English support** — auto-detects Japanese, Chinese, Korean, Russian, Arabic, Thai and sets appropriate DDG region
- **Wayback Machine fallback** for paywalled pages
- **PDF extraction** via pdftotext (poppler)
- **Wall-clock watchdog** — hard timeout kills hung fetches on all platforms

Full feature list and blocked/API-routed domain documentation in `AGENTS.md`.

## Requirements

- Python 3.11+ (auto-installed by `uv` if needed)
- `pdftotext` (optional — from [poppler](https://poppler.freedesktop.org/), for PDF content extraction)

## License

MIT
