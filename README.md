# Web Search Agent

Deep web search for [OpenCode](https://opencode.ai). Fetches and processes 50+ results per query — far beyond the typical 10-20 result limit of built-in tools.

## Why use it

Most LLM search tools return 10-20 results, limiting research depth on complex questions. This tool digs deeper — more sources, smarter extraction, better answers. Works with any LLM through OpenCode.

## Quick Start

```bash
git clone https://github.com/itohnobue/research-agent-opencode
cp -R research-agent-opencode/.opencode /path/to/your/project/
```

Then add `AGENTS.md` to your project root. Test it: *"Search for the most performant Rust web frameworks in 2025"*

Auto-installs Python dependencies via `uv` on first run. No API keys required.

## Usage

```bash
.opencode/tools/web_search.sh "React server components best practices" --tech
.opencode/tools/web_search.sh "CRISPR delivery methods" --sci --med
.opencode/tools/web_search.sh "Kalman filter implementations" --sci
```

| Flag | Sources | Best for |
|------|---------|----------|
| *(none)* | DuckDuckGo + Brave + Reddit + DDG News | General web |
| `--tech` | + Hacker News, Stack Overflow, Dev.to, GitHub | Software, DevOps |
| `--sci` | + arXiv, OpenAlex | CS, physics, math, engineering |
| `--med` | + PubMed, Europe PMC, OpenAlex | Medicine, clinical trials |

## Key features

- **50+ results per query** via DuckDuckGo + Brave search fallback
- **Anti-bot bypass** — Scrapling with TLS fingerprinting, auto-fallback to httpx
- **Smart extraction** — Trafilatura content-area detection (article body, not sidebars)
- **Token compression** — sentence-level BM25 + centrality scoring, cross-page deduplication
- **Non-English support** — auto-detects Japanese, Chinese, Korean, Russian, Arabic, Thai and sets appropriate DDG region
- **Wayback Machine fallback** for paywalled pages
- **PDF extraction** via pdftotext (poppler)

Full feature list and blocked/API-routed domain documentation in `AGENTS.md`.

## Requirements

- Python 3.11+ (auto-installed by `uv` if needed)
- `pdftotext` (optional — from [poppler](https://poppler.freedesktop.org/), for PDF content extraction)

## License

MIT
