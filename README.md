# Codex Observatory

A local, privacy-first dashboard for understanding Codex Desktop activity. Run it in the background with Docker Desktop or directly with Python. No AI APIs, telemetry, uploads, external assets, or runtime packages.

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

Python 3.11 or later, with no package installation:

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

Repeated identical commands/arguments within a turn are retry **candidates**, including intentional polling. Explicit status, shell exit code, and MCP `isError` establish failures. A wrapper returning only establishes that output arrived. Durations can overlap and do not add to wall time.

Approval subagents are identified by recorded model `codex-auto-review`. Their tokens appear separately; subscription treatment is unknown. Main-session totals do not automatically include child sessions.

Account percentages are historical account-level observations that may include other local/cloud activity. Passed reset times and stale observations are marked. [Official usage guidance](https://learn.chatgpt.com/docs/pricing) explains how context, models, task complexity, tools, and caching affect allowance.

## Privacy boundaries

- Docker mounts only the session directory, read-only. **Credentials, configuration, other Codex files, and archived sessions are not mounted.** Configuration therefore appears unavailable in Docker. Native mode reads selected configuration fields without changing them.
- Raw JSON is reduced to allowlisted metrics in memory. Prompts, commands, arguments, outputs, reasoning text, paths, account IDs, credentials, and secret configuration values are excluded. IDs are hashed; this is pseudonymization, not guaranteed anonymity.
- The container runs with your UID/GID, a read-only root filesystem, dropped capabilities, and bounded resources. Its sole published port binds to `127.0.0.1`. The application makes no outbound requests; Docker's default network is not an outbound firewall. Do not change the port binding to a public interface or expose it through a tunnel. See [Docker port-publishing guidance](https://docs.docker.com/engine/network/port-publishing/).
- Metrics require a random per-process token. Host/Origin checks, cross-site request rejection, restrictive CSP, no CORS, no-store headers, and disabled access logging protect against other websites. Local administrators and apps already able to read your files are outside this boundary.
- Image builds may download the official Python base image. The running app has no outbound calls. Raw logs are never copied into the image or build context.
- Optional exports contain timestamps, model/tool names, anonymous IDs, and counters; review before sharing. Raw logs, local measured reports, screenshots, credentials, and exports are excluded from Git and Docker's build context.

## Coverage and limits

The parser supports observed Codex schemas, not a guaranteed stable API. Compressed `.jsonl.zst` archives are counted but skipped. Source symlinks are skipped. Malformed, partial, oversized (>16 MiB), or unknown records generate coverage warnings; a later refresh can recover an unfinished append.

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
