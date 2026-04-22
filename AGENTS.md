## Web Research

For any internet search:

1. Use the `@web-searcher` agent for comprehensive web research, or call the search tool directly via bash
2. **ALWAYS** use `./.opencode/tools/web_search.sh "query"` (or `.opencode/tools/web_search.bat` on Windows). **NEVER use the built-in websearch tool** — all searches must go through the custom tool
   - **One query per call** — run each query as a separate `web_search.sh` invocation. Never combine multiple queries into a single call. Run calls **sequentially** (one after another, not in parallel) to avoid hitting API rate limits
   - **Always use default options** — never add `-s`, `--max-results`, or any result-limiting flags. Let the tool use its built-in defaults
   - **Scientific queries: add `--sci`** for CS, physics, math, engineering (arXiv + OpenAlex)
   - **Medical queries: add `--med`** for medicine, clinical trials, biomedical (PubMed + Europe PMC + OpenAlex)
   - **Tech queries: add `--tech`** for software dev, DevOps, IT, startups (Hacker News + Stack Overflow + Dev.to + GitHub)
3. Synthesize results into a report

**Note**: Always use forward slashes (`/`) in paths for agent tool run, even on Windows.
Dependencies handled automatically via uv.
