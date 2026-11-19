# Web Search Agent

Deep web search for [OpenCode](https://opencode.ai) V2. Requests up to 50 search results across several engines and fetches up to 20 pages per query by default — more depth than the typical 10-20 result limit of built-in tools — and delivers a one-line report pointer into an indexed report file you can read or grep.

## Why use it

Most LLM search tools return 10-20 results, limiting research depth on complex questions. This tool digs deeper — more sources, smarter extraction, quality filtering, better answers. Works with any LLM through OpenCode.

## Quick Start

```bash
git clone https://github.com/itohnobue/research-agent-opencode
cp -R research-agent-opencode/.opencode /path/to/your/project/
cp research-agent-opencode/AGENTS.md /path/to/your/project/
cp research-agent-opencode/opencode.json /path/to/your/project/   # skip if you already have one
```

If you already have an `AGENTS.md` or `opencode.json`, keep your own (append this AGENTS.md's instructions) instead of overwriting. This teaches OpenCode to route all web searches through this tool. Test it: *"Search for the most performant Rust web frameworks"*

## Key features

- **Up to 50 requested results / 20 DDG pages fetched per query** — plus up to 20 DeepSeek pages from the API response (50 when DuckDuckGo is unavailable), which are not fetched unless their content is missing
- **Anti-bot bypass** — Scrapling with TLS fingerprinting; httpx fallback for DNS-failing hosts
- **Smart extraction** — Trafilatura content-area detection (article body, not sidebars)
- **Quality filters** — content-farm/syndication detection (F5), recency filtering on time-sensitive queries (F7), stub-page drop, cross-page deduplication
- **Non-English support** — auto-detects Japanese, Chinese, Korean and sets the appropriate DDG region
- **Wayback Machine fallback** for paywalled pages
- **PDF links, on demand** — search mode lists PDF results in a `PDF LINKS` block instead of downloading them (their transfer dominates a run); `--url <link>` downloads and extracts one via pdftotext, with a built-in pure-Python fallback (no poppler required)
- **Structured sources** — RSS/Atom feeds return dated entry lists with entry URLs; Hacker News item pages return the comment thread; peer-reviewed links resolve to open-access full text (Europe PMC, OpenAlex, arXiv)
- **Page image URLs in the report** — a fetched page with images carries one `[images] …` line under its `=== <url> ===` header (`og:image`, then `twitter:image`, then the `<img>` attributes), so a picture can be taken straight from a result
- **Wall-clock watchdog** — hard timeout kills hung fetches on all platforms

Full feature list and blocked/API-routed domain documentation in `.opencode/agents/web-searcher.md`.

### Supported sources

| Source | What it provides | Best for |
|--------|------------------|----------|
| DuckDuckGo (multi-engine backend) | General web index, 50 requested results / 20 fetched pages per query | General questions and any topic without a specialist source |
| Hacker News | Tech community discussion, Show HN posts, launch threads | Software releases, developer opinion, startup news |
| Stack Overflow | Programming Q&A, accepted answers, error diagnostics | Language and library specifics, bug fixes |
| Dev.to | Developer blog posts and tutorials | Practical walkthroughs, practitioner experience |
| GitHub | Repositories, issues, release notes, source code | Implementation detail, project status, version facts |
| arXiv | Preprints in CS, physics, math, engineering | Research-level technical questions before peer review |
| OpenAlex | Scholarly works, authors, open-access links | Literature discovery, citation context |
| PubMed | Biomedical and clinical literature | Medicine, clinical trials, health evidence |
| Europe PMC | Biomedical abstracts and open-access full text | Clinical evidence with retrievable full text |
| Semantic Scholar | Citation records, citers and references | Citation fallback when another index lacks the paper |
| Wayback Machine | Archived copies of pages that are gone or paywalled | Dead links, changed pages, paywalled articles |
| DeepSeek (optional) | Separate in-house search index | Extra coverage when the default backend is thin |

### Optional: DeepSeek Search

Off by default. Turn it on in your shell:

```bash
export WEB_RESEARCH_DEEPSEEK=1
```

The API key is read from `provider.deepseek.options.apiKey` in `~/.config/opencode/opencode.json` — the config OpenCode already uses; `DEEPSEEK_API_KEY` overrides it.

## Requirements

- [OpenCode CLI](https://opencode.ai) — **V2 2.0+**
- Python 3.11+ (managed by `uv`; downloaded into uv's cache if needed)
- `pdftotext` (optional — from [poppler](https://poppler.freedesktop.org/); used when present for layout-aware PDF text. Without it, a bundled pure-Python extractor handles text PDFs — scanned pages and encrypted files still yield nothing)

## License

MIT
