# Web Search Agent

A web search agent for Claude Code (or any LLM) that processes 50+ results per search — far beyond the typical 10-20 limit.

## Quick Start

1. **Copy files**: Put `.claude/` folder into your Claude Code working directory
2. **Add instructions**: Copy `CLAUDE.md` contents into your project's instruction file
3. **Test it**: Ask Claude to search the web, e.g., *"Search for most beautiful Hokusai paintings and explain why they're great"*

The wrapper scripts auto-install **uv**, which handles Python and dependencies.

## Why You Need This

Most LLM tools (including Claude Code) only use 10-20 search results, limiting research depth.

This agent uses DuckDuckGo to fetch and process 50+ pages per query — similar to Qwen's Search function but works with any LLM.

**Best for**: Solving tricky bugs, tech research, any task where more information means better answers.

## Features

- **Deep Search**: 50+ results vs typical 10-20
- **Autonomous**: Single command searches, filters, fetches, and reports
- **Smart Filtering**: Skips blocked domains, login walls, CAPTCHA pages
- **Clean Extraction**: Uses w3m for proper table/list rendering (regex fallback)
- **Fast**: HTTP/2 connection pooling, parallel fetch (30-40% faster)
- **Observable**: Per-phase timing, failure breakdown, slow URL identification
- **Zero Setup**: Auto-installs dependencies via uv

## Requirements

- **uv**: Auto-installed by wrapper scripts
- **Python 3.11+**: Auto-installed by uv if needed
- **w3m** (optional): Better HTML rendering (tables, lists). Falls back to regex if not installed

## Diagnostics

Default output (stderr) shows timing at each phase:

```
Researching: "Python asyncio tutorial"
  [search] 10 URLs in 1.0s
    fetch: 10/10 (9 ok, 2s)
  Done: 9/10 ok (35,567 chars) in 2.6s
  Skipped: 1 HTTP 403
```

With `-v` (verbose), you see every URL individually:

```
Researching: "Python asyncio tutorial"
  [search] 10 URLs in 1.0s
    --   0.2s  python.plainenglish.io (HTTP 403)
    OK   0.4s  blog.apify.com
    OK   0.5s  docs.python.org
    OK   1.6s  www.lambdatest.com
  Done: 9/10 ok (35,567 chars) in 2.6s
  Skipped: 1 HTTP 403
```

Slow URLs (>5s) are always listed in the summary, even without `-v`.

## Blocked Domains

Automatically filtered (require login or block scraping):
reddit.com, twitter.com, x.com, facebook.com, youtube.com, tiktok.com, instagram.com, linkedin.com, medium.com

## License

MIT
