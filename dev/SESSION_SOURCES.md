# Session sources

Unwind separates session storage from the consumers that build session lists,
conversations, canvas windows, and reports. The `SessionSource` contract lives in
`src/unwind/sources/base.py`. `ClaudeSource` and `CodexSource` implement it.

## Add a source

1. Implement a `SessionSource` subclass in `src/unwind/sources/`.
2. Give it a stable `id` and display `label`. Use a namespaced session ID such as
   `example-<uuid>`; Claude retains its existing UUIDs for URL compatibility.
3. Implement project discovery and session listing. Return `SessionFile` values
   with the source path, file modification time, size, provider, and session ID.
   Group projects with the shared `slug_for(cwd)` function. Resolve sessions only
   inside the requested project.
4. Implement `owns_session`, `matches_record`, and `normalize`. For a format other
   than JSONL, also implement `matches_path` and `read_records`; consumers do not
   need to change. Keep message IDs stable when the transcript grows.
5. Register an instance in `sources.SOURCES`, before the Claude fallback.

The API exposes registered labels through `GET /api/sources`. Both UI filters
use that list. Unknown providers receive a generic icon; you can optionally add
brand artwork in `ProviderIcon.tsx`.

## Normalized records

Records use the existing conversation schema: `type`, `uuid`, `timestamp`,
`sessionId`, and a `message` containing content blocks. Supported blocks are
`text`, `thinking`, `tool_use`, and `tool_result`. System records carry their
text in `content`. Metadata can include `cwd` and `gitBranch`. The registry adds
`provider` to normalized records.

Emit token-only events with `type: "usage"`, `message.model`, and `message.usage`.
The four counters are disjoint: uncached input, cache reads, cache writes, and
output. Deduplicate repeated wire events inside the adapter. Reasoning tokens
already included in output must not be counted again. Usage events do not
increment the conversation message count.

Raw JSONL helpers remain available for Claude-specific fork and callstack
processing. General consumers use `iter_session_records` or
`read_session_records`. File modification time and size invalidate normalized
records, scans, and summaries. Canvas ETags include every source's session files.

## Optional behavior

`estimate_cost` returns per-category USD estimates or `None`. Unknown costs
remain explicit as `unpriced_tokens` in reports and canvas responses. Token
counts remain available even when prices are not.

`session_status` defaults to `done` (historical/resumable). Claude delegates to
its existing process and callstack status inference. Add source-specific live
tracking in an adapter before reporting a session as live. A recent file write
alone is not evidence that a process is still running.

All sources refresh through the UI's 30-second polling. Claude additionally
uses its existing filesystem watcher. Source-specific agent relationships are
not inferred from similar-looking transcript prefixes; Claude's callstack and
fork handling remains confined to its own storage.

## Codex support

Codex reads `sessions/**/*.jsonl` and `archived_sessions/**/*.jsonl` beneath
`CODEX_HOME` (default `~/.codex`). It groups sessions by the metadata `cwd`, keeps
active copies when an archived copy has the same ID, and reads function/custom
tool calls, results, messages, readable reasoning summaries, and cumulative token
usage. Event mirrors are excluded from conversations and repeated token totals
are ignored. New desktop `token_usage_record` entries and older `token_count`
events share one cumulative counter to prevent double counting.

Codex delegates cost estimates to `sources/openai_pricing.py`. The rate table uses
[OpenAI Standard API pricing](https://developers.openai.com/api/docs/pricing)
and individual [model pages](https://developers.openai.com/api/docs/models),
checked on October 6, 2026. It covers GPT-6, GPT-5.6, GPT-5.5, GPT-5.4, and older
Codex models. Exact model IDs, dated snapshots, and explicit aliases are supported;
unknown models remain unpriced. Add or update a table entry to maintain rates.

Prices are current API-equivalent token estimates, not historical invoices or
ChatGPT subscription charges. Service tiers (including Fast), regional uplifts,
tool fees, and discounts are excluded. Cache-write tokens use the published
write rate on newer models and the normal input rate on older ones. Long-context
pricing uses the input tokens in each usage delta, including cached tokens, and
assumes each positive cumulative delta represents one request. Incomplete logs
that combine multiple requests into a delta can therefore overestimate this tier.

Codex live-process detection, nested agent relationships, and
cloud-only ChatGPT conversation import are not implemented. Child rollouts with
a working directory appear as separate sessions.

`tests/test_session_sources.py` exercises both built-in sources together and
registers a third test adapter to verify that shared consumers need no changes.
