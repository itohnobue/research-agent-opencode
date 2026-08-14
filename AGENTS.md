## Web Research

For any internet search:

1. Use the `@web-searcher` agent for comprehensive web research, or call the search tool directly via bash
2. **ALL internet research must go through `web_search.sh`** — no exceptions. This means: no built-in websearch tool, no WebFetch tool, no `curl` against APIs, no manual GitHub API calls, no `wget` for search. Fetching a specific known URL goes through `web_search.sh --url <url>` (direct fetch mode, up to 50k chars) — the sanctioned way to get a named page when a search would be wasteful. Every time you need information from the internet, use `./.opencode/tools/web_search.sh "query"` (or `.opencode/tools/web_search.bat` on Windows)
   - **One query per call** — run each query as a separate `web_search.sh` invocation. Never combine multiple queries into a single call. Run calls **sequentially** (one after another, not in parallel) to avoid hitting API rate limits
   - **Fixed tuned defaults** — the tool has no count or format flags: search always fetches 30 results, fetches up to 20 pages, and outputs plain text only. The only flags are the source flags `--sci`/`--med`/`--tech` and `--url` direct fetch — never add count/result-limiting or output-format flags (they do not exist). Let the tool use its built-in defaults
   - **DIGEST + FULL REPORT FILE** — search mode prints a compact digest (stats line, FULL REPORT path, per-page previews) and writes the full filtered text to `tmp/webresearch/<run-id>.txt`. The report file IS the product — read or grep the file at the given path for the content you need (grep by URL or term); the digest is small and must not be trimmed. Do NOT head/tail/grep -m the tool's stdout — nothing to gain, the full content is in the file, not in stdout. For a specific page's fresh content, fetch it directly with `--url`.
   - **Direct URL fetch: `--url`** — when you need a specific known page (URL from a search result, docs page, paper), use `web_search.sh "dummy" --url <url>` instead of WebFetch/curl/wget. Up to 50k chars per page, plain text.
   - **Scientific queries: add `--sci`** for CS, physics, math, engineering (arXiv + OpenAlex)
   - **Medical queries: add `--med`** for medicine, clinical trials, biomedical (PubMed + Europe PMC + OpenAlex)
   - **Tech queries: add `--tech`** for software dev, DevOps, IT, startups (Hacker News + Stack Overflow + Dev.to + GitHub)
3. Synthesize results into a report

**Note**: Always use forward slashes (`/`) in paths for agent tool run, even on Windows.
Dependencies handled automatically via uv.
