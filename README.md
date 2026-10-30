# Codex Observatory

A local, privacy-first dashboard for understanding Codex Desktop activity. Run it in the background with Docker Desktop or directly with Python. No AI APIs, telemetry, uploads, external assets, or Python runtime packages. Compressed history uses the `zstd` command, included in the Docker image.

## Run in the background on macOS

Requires Docker Desktop running and Python 3.11+ for the convenience commands.

```sh
git clone https://github.com/eparirishit/codex-observatory.git
cd codex-observatory
python3 manage.py up
```

Open the complete local URL printed by the command. You can close Terminal afterward. The container restarts when Docker Desktop restarts, unless you explicitly stop it. Docker Desktop must be running; enable its start-at-login setting yourself if you want that behavior after signing in to macOS.

You can also double-click **Start Observatory.command** from Finder to start the background container.

```sh
python3 manage.py status    # Container state and health
python3 manage.py url       # Retrieve the current private access link
python3 manage.py logs      # Recent operational messages, no raw log contents
python3 manage.py down      # Stop/remove the container, preserving source logs
```

The link contains a random access token. It is removed from the address bar after opening and kept in tab memory. After a browser reload or container restart, retrieve the current link again. Dashboard Refresh and Live updates work without a page reload. No token is saved in browser storage or printed in Docker startup logs. Its private runtime file lives on an ephemeral in-container filesystem.

The default source is `~/.codex/sessions`. Customize the source or port:

```sh
CODEX_SESSION_DIR=/path/to/codex/sessions OBSERVATORY_PORT=8766 python3 manage.py up
```

Use the same source/port environment for subsequent management commands. Direct Compose users should supply their local UID/GID:

```sh
LOCAL_UID=$(id -u) LOCAL_GID=$(id -g) docker compose up -d --build --wait
docker compose exec -T observatory cat /tmp/observatory-link
```

`manage.py` supplies these IDs automatically. Do not loosen source-log permissions to make the container work.

## Run without Docker

Python 3.11 or later. Install the `zstd` command to read compressed history (`brew install zstd` on macOS); no Python packages are needed:

```sh
python3 observatory.py
```

Open the complete printed URL. Stop this foreground process with Control-C. Use `--port 8766` or `--codex-home /path/to/codex-home` if needed. Native mode also inspects `archived_sessions` and selected safe fields in `config.toml`.

## Four views

- **Overview:** token composition, activity by turn start date, historical account allowance, models/activity types, and evidence-backed suggestions.
- **Sessions:** filter/sort chats, select a session, and inspect turns, context growth, tool timeline, failures, repeats, compactions, and account snapshot history.
- **Tools & MCP:** execution counts, top MCP tools, failures, repeated-call candidates, durations, and shell-output byte counts. Outer calls and inner executions remain separate.
- **Data & privacy:** coverage, configuration availability, measured versus inferred values, limitations, and accounting rules.

Live updates refresh every 15 seconds while the tab is visible. Unchanged files reuse in-memory metrics; changed files are reparsed read-only. When the dashboard is closed, the container waits for requests. Codex logs remain the source of history; no scheduled job or AI call is involved.

## Understand the numbers

**Recorded tokens are activity counters.** Exact subscription quota spent by a session, model, or tool is unavailable.

The preferred source is `token_usage_record.usage`, deduplicated by response ID. Per-turn totals sum responses using recorded turn IDs. Multiple segments of one session are combined. Thread/turn summary counters and `token_count` snapshots are never added to response records again.

Older logs use cumulative deltas. Duplicate snapshots add no tokens. Initial snapshots containing inherited history are excluded as unattributed baselines. Counter decreases establish new baselines and exclude ambiguous reset samples. These updates can combine responses, so fallback response counts and turn attribution are approximate. Missing values show as `—`, never zero.

**Cached input is part of input; reasoning is part of output.** The composition chart uses three disjoint groups: cached input, remaining input, and generated output. It is omitted when required counters are missing or inconsistent. Daily bars assign an entire turn to its start date in the browser time zone, including turns crossing midnight. The latest 30 recorded dates are charted; all dates remain in the exact-value table.

Response input is a context proxy, not precise live occupancy. It cannot isolate tokens attributable to prompts, plugins, files, or tool definitions. Compactions use committed `compacted` checkpoints, excluding mirrored items.

Repeat **candidates** require the same tool and arguments after a recorded failure in the same turn. Successful polling and known overlapping executions are excluded; candidates do not prove a retry. Explicit status, shell exit code, and MCP `isError` establish failures. Collaboration tool executions preserve their recorded status and duration; aborted turns are ended. A wrapper returning only establishes that output arrived. Overlapping log segments retain terminal results and join calls with outputs recorded in another segment. Durations can overlap and do not add to wall time.

Approval subagents are identified by recorded model `codex-auto-review`. Their tokens appear separately; subscription treatment is unknown. Main-session totals do not automatically include child sessions.

Account percentages are historical account-level observations that may include other local/cloud activity. Passed reset times and stale observations are marked. [Official usage guidance](https://learn.chatgpt.com/docs/pricing) explains how context, models, task complexity, tools, and caching affect allowance.

Recommendations appear only for a turn at least five times the median of five or more positive-usage turns with the same activity type, model, and effort, or a sustained cache-hit drop from at least 80% to 50% or less (at least 30 percentage points). Cache comparisons use the latest two three-response windows in one session with unchanged model/effort and at least 10,000 input tokens in each window. Only globally deduplicated response-level token records with response IDs support these recommendations; mixed-model or mixed-effort turns are excluded from outlier comparisons. No signal produces a neutral empty state. The dashboard consolidates measurement caveats in “How to read this”; numbered labels refer to those notes.

## Privacy boundaries

- Docker mounts only the session directory, read-only. **Credentials, configuration, other Codex files, and archived sessions are not mounted.** Configuration therefore appears unavailable in Docker. Native mode reads selected configuration fields without changing them.
- Raw JSON is reduced to allowlisted metrics in memory. Prompts, commands, arguments, outputs, reasoning text, paths, account IDs, credentials, and secret configuration values are excluded. IDs are hashed; this is pseudonymization, not guaranteed anonymity.
- The container runs with your UID/GID, a read-only root filesystem, dropped capabilities, and bounded resources. Its sole published port binds to `127.0.0.1`. The application makes no outbound requests; Docker's default network is not an outbound firewall. Do not change the port binding to a public interface or expose it through a tunnel. See [Docker port-publishing guidance](https://docs.docker.com/engine/network/port-publishing/).
- Metrics require a random per-process token. Host/Origin checks, cross-site request rejection, restrictive CSP, no CORS, no-store headers, and disabled access logging protect against other websites. Local administrators and apps already able to read your files are outside this boundary.
- Image builds may download the official Python base image. The running app has no outbound calls. Raw logs are never copied into the image or build context.
- Optional exports contain timestamps, model/tool names, anonymous IDs, and counters; review before sharing. Raw logs, local measured reports, screenshots, credentials, and exports are excluded from Git and Docker's build context.

## Coverage and limits

The parser supports observed Codex schemas, not a guaranteed stable API. Compressed `.jsonl.zst` archives are streamed read-only through the same parser, including archived sessions in native mode. A missing `zstd` command or a corrupt archive is counted as an unreadable file and a skipped compressed archive; partial metrics from failed decoding are discarded. Source symlinks are skipped. Malformed, partial, oversized (>16 MiB), or unknown records generate coverage warnings; unsupported event and item types also appear in the coverage totals. A later refresh can recover an unfinished append.

Hidden platform work, cloud activity absent from local logs, plugin overhead, tool token charges, precise live context occupancy, and exact subscription accounting are unavailable.

## Development

```sh
python3 -m unittest -v
node test_metrics.cjs
node --check static/app.js
docker build -t codex-observatory:test .
```

Tests use synthetic records and planted secrets, never private log fixtures. Chart checks verify non-overlapping composition and date grouping. CI tests and builds the image without any source-log mounts. Node is only needed for development checks.

`python3 observatory.py --check` prints content-free local metrics; review before including that output in a public issue.

## Cost and workflow analytics

The dashboard computes **API-equivalent cost, not what you pay on a subscription**, in the browser from allowlisted response metrics. The catalog is a dated snapshot of [official API pricing](https://developers.openai.com/api/docs/pricing) and its linked model pages, verified 2026-10-30. It is a current-rate comparison, not a reconstruction of historical invoices. No pricing service is contacted at runtime.

- **Pricing basis:** use the recorded tier, or compare Standard, Fast, Ultrafast, and Flex scenarios without changing Codex. Fast/priority uses the published API rate, never a subscription-credit multiplier. Published long-context adjustments apply to full responses above the model's threshold. Cached input is a subset of input and reasoning is a subset of output, so neither is added twice.
- **Coverage:** unknown models/tiers, missing or inconsistent counters, and cumulative fallback records are unpriced. Positive cache-write counts need a user-supplied rate because the logs do not establish retention. Partial subtotals omit unpriced responses and show coverage. Regional premiums, separate service/tool fees, taxes, and subscription charges are excluded.
- **Overrides:** expand “Rates, coverage & local overrides” and enter an exact `model|tier` mapping with USD rates per million tokens, for example `{"my-model|fast":{"input":3,"cached":0.3,"output":15}}`. Optional `cache_write` supplies an explicit write rate. Rates must be finite and nonnegative. Known models retain their published long-context multipliers; unknown-model overrides are flat rates at every context size. Invalid edits leave the previous rates intact.
- **Reasoning and normalization:** compare reasoning/output ratios by recorded model and effort, tokens per inner execution, tokens per completed turn-minute, and cost per turn. Coverage is explicit; these are activity measures and do not score productivity or answer quality.
- **Rollups and cache:** Day/Week/Month groups use recorded response timestamps in the browser's time zone; weeks start on Monday. All periods remain visible. Cache shares are weighted by input tokens, missing observations remain gaps, and substantial drops are flagged.
- **Observed windows:** group local response activity into the 5-hour or weekly intervals actually recorded in rate-limit snapshots. Local tokens/hour and account percentage-points/hour are separate measurements. The account slope requires two monotonic observations at least five minutes apart, can include other local/cloud work, and is not a token-to-quota conversion. Historical/stale observations stay labeled.
- **Tools:** signed input change between consecutive response records is a context-change proxy, not a tool token charge. Previous assistant output and other context also contribute. Compaction/model changes suppress the proxy; multi-call intervals stay shared. Only single-call intervals enter a tool's aggregate, with interval coverage and shell output bytes beside it.
- **Projects:** working directories become stable hashed IDs before leaving the reader. You can filter projects and assign labels in the browser. Labels, price overrides, and pricing basis are the only browser-stored preferences; access tokens remain in tab memory. These preferences and raw working directories are excluded from metrics exports. Exports now include allowlisted per-response counters, hashed response/project IDs, recorded service tiers, and execution timing; review them before sharing.

The existing `--check` diagnostic remains content-free and the `test_metrics.cjs` chart checks also cover the analytics calculations. No new runtime dependency is required.
