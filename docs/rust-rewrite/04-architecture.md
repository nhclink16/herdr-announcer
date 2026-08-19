# Architecture, phases, tests, CI

## Crate layout

Single crate at repo root (Python coexists until Phase 7):

```
Cargo.toml
herdr-plugin.toml            # stays Python-driven until Phase 7 cutover
src/
  main.rs        # argv dispatch only; exit codes 0/1/2/130         (~120 lines)
  lib.rs         # module declarations + shared Reasons type
  paths.rs       # dir resolution, PLUGIN_ID
  lockfile.rs    # flock helpers (exclusive, nonblocking-poll)
  atomicfile.rs  # tmp+rename atomic writes (json + text)
  ipc.rs         # herdr socket client + typed method wrappers
  event.rs       # event/context JSON parsing
  config.rs      # DEFAULTS, ordered keys, load, validation
  config_write.rs# toml_edit comment-preserving writes, .bak, lock, rollback
  redact.rs      # full port of announcer/redact.py
  log.rs         # announcer.log write/trim/parse
  snooze.rs      # snooze.json, parse_duration, labels, steps
  debounce.rs    # last.json reserve/rollback under flock
  mute.rs        # pane-mutes.json + mute_agents checks (NEW)
  deadline.rs    # TwoPhaseDeadline + subprocess line pumps
  summarize.rs   # prompts, template, codex NDJSON, command, sanitizer, chain
  speech.rs      # speak(), players, elevenlabs, local TTS, lock, capabilities
  hook.rs        # process_invocation pipeline + new-hook routing
  actions.rs     # pane-context action handlers (NEW)
  cli.rs         # status output, last-error, usage
  snapshot.rs    # byte-stable plain-text dashboard snapshot
  tui/
    mod.rs       # terminal setup/teardown, force_color_output, panic hook
    theme.rs     # glyphs + colors (single source)
    dashboard.rs # ratatui dashboard (state, update, view)
    wizard.rs    # inline-viewport wizard + non-TTY path
    widgets.rs   # clack widgets + transcript collapse
tests/
  fixtures/      # captured live JSON (Phase 0) + FINDINGS.md
  golden/        # status.txt, snapshot.txt — generated FROM the Python impl
  status_golden.rs  snapshot_golden.rs  pipeline_it.rs
.github/workflows/ci.yml
scripts/check-version.sh   scripts/parity.sh
```

## CLI dispatch (main.rs — hand-parsed, no clap)

```
herdr-announcer                      # hook mode (HERDR_PLUGIN_EVENT_JSON | stdin;
                                     #  TTY stdin + no event env → usage, exit 2)
herdr-announcer --test
herdr-announcer status
herdr-announcer setup
herdr-announcer dashboard
herdr-announcer dashboard snooze <5m|30m|2h|tomorrow|off>
herdr-announcer dashboard toggle-toast
herdr-announcer dashboard open
herdr-announcer action <mute-pane|snooze-pane|announce-now>
herdr-announcer cleanup
```

## Cargo.toml

```toml
[package]
name = "herdr-announcer"
version = "1.0.0"
edition = "2024"
license = "MIT"
description = "Speaks a short summary when an agent finishes or needs input"

[lib]
name = "herdr_announcer"
path = "src/lib.rs"

[[bin]]
name = "herdr-announcer"
path = "src/main.rs"

[dependencies]
ratatui = "0.30"
crossterm = "0.29"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
toml_edit = "0.23"     # comment-preserving config read/write
jiff = "0.2"           # local-offset timestamps + DST-correct next-8am
regex = "1"            # redaction parity (port regex-for-regex)
ureq = { version = "3", default-features = false, features = ["rustls"] }
rustix = { version = "1", features = ["fs"] }   # flock
tempfile = "3"
```
No clap, no thiserror, no async. Error model = accumulated `Vec<String>` reasons
plus a tiny `SpeakError { PlaybackLockTimeout, Other(String) }`.

## ipc.rs contract

- `socket_path()` = `HERDR_SOCKET_PATH` only; missing → reason
  `herdr: no socket path` and degrade (never CLI-scrape).
- `call(method, params)` — one `UnixStream::connect` per request, 5s read/write
  timeouts, 4 MiB cap, id `"nhclink16.announcer:<method>"`; `error` member →
  `io::Error` `"herdr rpc <method>: <code> <message>"`.
- Typed wrappers: `pane_list`, `pane_current`, `pane_get`, `pane_read` (source
  `recent_unwrapped`, lines 100, permissive text extraction, last 4000 chars),
  `workspace_label` (via `workspace.list`), `notification_show`,
  `report_metadata`, `plugin_pane_open`.
- Reason prefixes preserved: `herdr-workspace:`, `herdr-read:`, `toast:`.

## Manifest v2 (activates in Phase 7; a dev copy evolves from Phase 2)

```toml
id = "nhclink16.announcer"
name = "Announcer"
version = "1.0.0"
min_herdr_version = "0.8.0"
description = "Speaks a short summary when an agent finishes or needs input - local TTS, ElevenLabs, or any custom command"
platforms = ["macos", "linux"]

[[build]]
command = ["cargo", "build", "--release"]

[[events]]
on = "pane.agent_status_changed"
command = ["./target/release/herdr-announcer"]

[[events]]
on = "pane.agent_detected"
command = ["./target/release/herdr-announcer"]

[[events]]
on = "pane.closed"
command = ["./target/release/herdr-announcer", "cleanup"]

[[events]]
on = "pane.exited"
command = ["./target/release/herdr-announcer", "cleanup"]

[[actions]]
id = "mute-pane"
title = "Mute announcer for this pane"
description = "Toggle announcements for this pane; the mute dies with the pane"
contexts = ["pane"]
command = ["./target/release/herdr-announcer", "action", "mute-pane"]

[[actions]]
id = "snooze-pane"
title = "Snooze announcer for this pane"
description = "Cycle this pane's snooze: 5m, 30m, 2h, off"
contexts = ["pane"]
command = ["./target/release/herdr-announcer", "action", "snooze-pane"]

[[actions]]
id = "announce-now"
title = "Announce this pane now"
description = "Summarize and speak this pane's current status immediately"
contexts = ["pane"]
command = ["./target/release/herdr-announcer", "action", "announce-now"]

[[actions]]
id = "test"
title = "Test announcer voice"
description = "Speak a sample announcement with the configured voice"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "--test"]

[[actions]]
id = "status"
title = "Announcer status"
description = "Show configuration, detected tools, and recent log lines"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "status"]

[[actions]]
id = "open-dashboard"
title = "Open announcer dashboard"
description = "Live status and one-key controls in a popup"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "open"]

[[actions]]
id = "snooze-5m"
title = "Snooze announcer for 5 minutes"
description = "Silence all announcements for 5 minutes"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "snooze", "5m"]

[[actions]]
id = "snooze-30m"
title = "Snooze announcer for 30 minutes"
description = "Silence all announcements for 30 minutes"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "snooze", "30m"]

[[actions]]
id = "snooze-2h"
title = "Snooze announcer for 2 hours"
description = "Silence all announcements for 2 hours"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "snooze", "2h"]

[[actions]]
id = "snooze-tomorrow"
title = "Snooze announcer until tomorrow morning"
description = "Silence all announcements until 8:00 tomorrow"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "snooze", "tomorrow"]

[[actions]]
id = "snooze-off"
title = "Resume announcer now"
description = "End any active snooze immediately"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "snooze", "off"]

[[actions]]
id = "toggle-toast"
title = "Toggle announcer toast notifications"
description = "Mirror announcements as Herdr notifications on or off"
contexts = ["workspace"]
command = ["./target/release/herdr-announcer", "dashboard", "toggle-toast"]

[[panes]]
id = "setup"
title = "Announcer setup"
description = "Interactive setup wizard"
placement = "split"
command = ["./target/release/herdr-announcer", "setup"]

[[panes]]
id = "dashboard"
title = "Announcer dashboard"
description = "Live status and one-key controls"
placement = "popup"
width = 94
height = 30
command = ["./target/release/herdr-announcer", "dashboard"]
```

## Phases — tasks and acceptance criteria

### Phase 0 — Live contract verification (no product code)

Capture into `tests/fixtures/` (pretty-printed, with a FINDINGS.md noting herdr
version + date + each verified fact/surprise):
1. Hook env + `HERDR_PLUGIN_EVENT_JSON` for `pane.agent_status_changed`,
   `pane.agent_detected`, `pane.closed`, `pane.exited` (temporary dump hooks in
   a linked dev copy; confirm flat vs nested and where pane_id lives).
2. A temporary `contexts=["pane"]` action dumping `HERDR_PLUGIN_CONTEXT_JSON` +
   env → one real `PluginInvocationContext`.
3. Pane-entrypoint env dump → is `HERDR_SOCKET_PATH` present in panes?
4. Socket transcripts: `pane.read`, `workspace.list`, `pane.list`,
   `pane.current`, `pane.get`, `notification.show` (title/body split,
   `sound:"none"`), `pane.report_metadata` with `state_labels` (+ how a badge
   renders and how to clear it), `plugin.pane.open` params, `plugin.list`.
   Confirm ECONNRESET on a second request per connection.
Acceptance: fixtures + FINDINGS.md committed to the working tree; any spec
amendments listed at the top of FINDINGS.md.

### Phase 1 — Foundation

Files: Cargo.toml, main.rs (usage/exit codes), lib.rs, paths.rs, lockfile.rs,
atomicfile.rs, redact.rs, log.rs, snooze.rs, debounce.rs, config.rs, cli.rs
(status), snapshot.rs, ci.yml, scripts/check-version.sh.
Key signatures:
```rust
pub fn resolve_dirs() -> (PathBuf, PathBuf);
pub fn with_flock<T>(lock: &Path, f: impl FnOnce() -> T) -> io::Result<T>;
pub fn write_json_atomic(path: &Path, v: &serde_json::Value) -> io::Result<()>; // compact, sorted, \n
pub fn mask_secret(v: &str) -> String;
pub fn redact_command(argv: &[String]) -> Vec<String>;
pub fn redact_command_text(text: &str, argv: &[String]) -> String;
pub fn log_invocation(state: &Path, pane: &str, status: &str, action: &str,
                      elapsed: f64, trace: &str, reasons: &[String]) -> io::Result<()>;
pub fn read_log(path: &Path, limit: usize) -> Vec<LogEntry>;
pub fn reserve_debounce(state: &Path, pane: &str, status: &str, secs: i64)
    -> io::Result<(bool, Option<f64>)>;
pub fn rollback_debounce(state: &Path, pane: &str, status: &str,
                         reservation: Option<f64>) -> io::Result<()>;
pub fn set_snooze(state: &Path, spec: &str, now: f64) -> Option<f64>;
pub fn load_config(dir: &Path, reasons: &mut Vec<String>,
                   unknown: Option<&mut Vec<String>>) -> Result<Config, String>;
```
Also: `to_python_json()` helper matching Python `json.dumps` defaults (space
after `,` and `:`; `null`/`true`/`false`) for status/values rendering.
Tests: port test_redact, test_paths (minus CLI-probe cases), test_config (minus
tiny-TOML), log-parsing + snooze-state + debounce test groups. Generate
`tests/golden/status.txt` by running `python3 announce.py status` against a
fixture config/state dir.
Acceptance: build/test/clippy green; CI green ubuntu+macos;
`diff <(python3 announce.py status) <(target/release/herdr-announcer status)`
differs ONLY by the two appended new-key lines (with
HERDR_PLUGIN_CONFIG_DIR/STATE_DIR pointing at fixtures).

### Phase 2 — IPC + minimal pipeline

Files: ipc.rs, event.rs, hook.rs (template-only), speech.rs (local TTS + custom
speak_command + playback lock), main.rs hook mode.
Tests: parse_event ports against Phase 0 fixtures; ipc timeout test (accepting-
but-silent UnixListener must error < 2s); pipeline integration against a fake
unix-socket herdr serving canned fixtures (action strings, log lines, debounce
reserve/rollback, filter/snooze ordering).
Acceptance (live, dev-linked via `herdr plugin link` with a dev manifest): a real
status change speaks a template announcement; `herdr plugin log list --plugin
nhclink16.announcer` shows `announced+summary-template+speak-…`; unset socket
path → reason `herdr: no socket path` with degraded context; `--test` speaks.

### Phase 3 — Full summarize + speech parity

Files: deadline.rs, summarize.rs, speech.rs (ElevenLabs + players), cli.rs
last-error, hook.rs completion.
Tests: deadline ports (6), summarize ports (7), speech ports (~18 incl. the
end-to-end secret-never-leaks assertion), codex collector against a stub `codex`
script on PATH emitting fixture NDJSON, timing tests asserting ordering not
speed.
Acceptance: live codex summary end-to-end; codex removed from PATH → fallback
chain per config; ElevenLabs against a local HTTPS stub asserting header
redaction (+ one manual real-key run); `status` shows masked last error.

### Phase 4 — Mutes, new hooks, pane actions

Files: mute.rs, actions.rs, hook.rs gates + detect path, config.rs new keys,
cleanup subcommand, dev manifest gains new [[events]]/[[actions]].
Tests: mute round-trip/prune/expiry; ordering (`skipped-status` beats
`muted-pane`); context parsing incl. all-fields-missing → `pane.current`
fallback (pane.current needs `caller_pane_id`); announce-now bypass semantics;
cleanup removes entry + clears the `muted` token; agent_detected release events
(`released: true`) never announce.
Acceptance (live): mute-pane action toasts + the `muted` token appears in
`pane.get` (no visible badge in v1.0 — see FINDINGS.md #4/#5); a `done` event in
a muted pane logs `muted-pane`; closing the pane logs `cleanup` and clears state;
announce-now speaks any pane on demand; `mute_agents=["codex"]` silences codex
panes only; default config behaves byte-identically to Phase 3.

### Phase 5 — Dashboard

Files: tui/mod.rs, theme.rs, dashboard.rs, snapshot golden test, `dashboard`
subcommands (exact stdout/exit codes per 03-ui-spec).
Tests: state-machine ports (~80: key dispatch, derived values, config
round-trip); TestBackend row assertions against the layout table; mouse
hit-region tests (synthetic MouseEvent coords); `tests/golden/snapshot.txt`
generated from Python `dashboard.py` in a pipe, asserted byte-identical.
Acceptance: manual checklist in the real 94×30 popup — every key works, click
toggles rows, wheel scrolls log with scrollbar, Esc quits, 59×15 shows too-small
line, `w` execs wizard with restored terminal, colors render under NO_COLOR=1.

### Phase 6 — Wizard

Files: tui/widgets.rs, tui/wizard.rs, config_write.rs rollback plumbing.
Order: FIRST a ~30-line spike proving `Viewport::Inline` + `insert_before`
collapse, then the widgets, then the 9 steps.
Tests: wizard non-TTY flow ports (19), widget state-machine tests (14), config
write round-trips (hand-written config with comments + unknown keys → toggle →
comments/unknown keys survive; assert semantics, not bytes — toml_edit updates
in place rather than Python's move-to-top).
Acceptance: full `setup` run in a split pane with clack collapse lines; Ctrl-C
mid-flow prints `setup aborted, nothing written`, exit 130, config untouched;
comments survive a wizard write.

### Phase 7 — Cutover v1.0

- Manifest v2 live; delete `announce.py`, `dashboard.py`, `announcer/`,
  `test_*.py`, `__pycache__`; make `examples/acp-summary.py` self-contained
  (inline its ~90-line import from `announcer.deadline`); keep
  `examples/route-speak.sh`.
- Docs: development.md (cargo layout/test loop), configuration.md (+`mute_agents`,
  `announce_on_detect`, pane actions, comment-preservation note fixed),
  troubleshooting.md (socket failure reasons), README (new actions, mouse
  dashboard, re-recorded tapes), CHANGELOG.md.
- `scripts/check-version.sh`: Cargo.toml version == herdr-plugin.toml version ==
  top CHANGELOG heading; wired into CI. Bump all to 1.0.0.
- Delete the python-parity CI job and `scripts/parity.sh`.
Acceptance: fresh `herdr plugin link` clean; `herdr plugin list --json` shows
zero warnings; full manual smoke (status-change announce, snooze action,
dashboard popup, wizard, mute-pane, announce-now).

## Test strategy

- ~270 of 364 Python tests port 1:1 (logic + ordering + subcommand wording).
- ~25 obsolete: tiny-TOML parser, raw-byte key reader, ANSI stripper,
  import-compat aliases, suite invariants.
- ~70 re-specified: render tests → snapshot golden + TestBackend assertions;
  tui widget tests → widget state machines; acp-summary stays a Python smoke.
- New Rust-only: ipc timeout/oversize/error-member; fixture-driven event
  decoding for all hooked types; mute state; context fallback chain; mouse
  hit-testing; to_python_json; version-check script.
- Shared redaction vectors: export the Python test vectors as JSON consumed by
  both suites until Phase 7.
- `scripts/parity.sh` (until Phase 7): fixture dirs → diff Python vs Rust for
  `status`, snapshot, `dashboard snooze 5m` stdout, one synthetic event's log
  line (template summary, `speak_command=["true"]`), and `last.json`/
  `snooze.json` bytes.

## CI (`.github/workflows/ci.yml`)

```yaml
name: CI

on:
  push:
    branches: [master]
  pull_request:

jobs:
  rust:
    name: build + test + clippy (${{ matrix.os }})
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
      - uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy, rustfmt
      - uses: Swatinem/rust-cache@v2
      - run: cargo fmt --check
      - run: cargo build --release
      - run: cargo test
      - run: cargo clippy --release -- -D warnings
      - run: scripts/check-version.sh

  shell:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: shellcheck examples/*.sh

  # DELETE this job in Phase 7 together with the Python sources.
  python:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
      - run: python3 -m unittest discover -v
```
(Note: this repo's default branch is `master`, not `main`.)

## Risks (watch during review)

1. toml_edit updates in place vs Python's move-keys-to-top — assert semantics.
2. Byte-stable status/snapshot: json.dumps separators, char-based padding —
   goldens are the referee.
3. Redaction parity — shared vectors + end-to-end secret assertion.
4. Codex two-phase deadline — port tests before implementation.
5. Wizard inline-viewport collapse — spike first.
6. Pane env austerity — TUI degrades to reason-logged no-ops without a socket.
7. jiff local-offset/DST math and f64 debounce-token JSON round-trip — targeted
   tests incl. a DST transition date.
