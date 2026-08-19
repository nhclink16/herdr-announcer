# Rust rewrite — overview and working agreement

This directory is the specification for rewriting herdr-announcer in Rust. It was
produced from three deep code-exploration reports (the Python implementation, the
current UI, and the live herdr 0.8.0 platform contract) plus an architecture design
pass. **Read the file for the phase you are implementing before writing code.**

Files:
- `00-overview.md` — this file: goals, locked decisions, phase list, working rules.
- `01-herdr-contract.md` — verified herdr 0.8.0 platform contract (socket, events,
  manifest, pane-context actions, pitfalls).
- `02-core-behavior.md` — exact behavioral spec of the Python core pipeline that the
  Rust port must preserve.
- `03-ui-spec.md` — exact spec of the dashboard/wizard: what exists today and the
  new ratatui design (layouts, keys, mouse, strings — follow it literally).
- `04-architecture.md` — crate layout, module map, dependencies, manifest v2,
  phase-by-phase tasks with acceptance criteria, test-porting map, CI, risks.

## Goal

Replace the Python implementation (v0.9.1, ~4.6k lines, 364 tests) with a single
Rust binary at feature parity PLUS: pane-context actions (mute-pane, snooze-pane,
announce-now), persistent per-agent-type mutes, a mouse-capable scrollable
dashboard, new event hooks (`pane.agent_detected`, `pane.closed`, `pane.exited`),
and marketplace-grade polish. Ships as v1.0.0.

## Locked decisions — do not relitigate

- Full Rust: ratatui 0.30 + crossterm 0.29, single crate at repo root,
  `[[build]] = ["cargo", "build", "--release"]`.
- Socket-only herdr integration over `HERDR_SOCKET_PATH`. No `herdr` CLI scraping.
  Pin the contract; failures append reason strings and degrade, never panic.
- Spawn-per-event hooks (no daemon). All cross-invocation state is flock-serialized
  files, which makes concurrent hook bursts safe.
- Plugin id stays `nhclink16.announcer`. `config.toml` schema unchanged (18 existing
  keys, identical defaults) + two new keys `mute_agents = []`,
  `announce_on_detect = false`. Hand-edited user configs must keep working.
- Python coexists in the repo and keeps shipping until Phase 7 deletes it in one
  cutover release. The Python source is the authoritative spec wherever this
  documentation is ambiguous — read the cited file.
- macOS + Linux only. Isolate POSIX-isms (UnixStream, flock, exec) in
  `ipc.rs` / `lockfile.rs` / one dashboard function.
- Default behavior must be byte-identical to Python v0.9.1: same log lines, same
  `status` output (plus two appended key lines), same snapshot output, same state
  files, same exit codes (2 usage / 1 error / 130 wizard abort).

## Phases

0. Live contract verification — capture fixtures from running herdr, no product code.
1. Foundation — scaffold, paths/locks/atomic writes/redact/log/snooze/debounce/
   config/status/snapshot, CI, golden files generated from the Python impl.
2. IPC + minimal pipeline — socket client, event parsing, hook mode with template
   summaries + local/custom speech.
3. Full summarize + speech parity — codex NDJSON, command backend, ElevenLabs,
   last-error, redaction end-to-end.
4. Mutes, new hooks, pane-context actions.
5. Dashboard (ratatui, mouse, scrollable log).
6. Wizard (ratatui inline viewport, clack transcript).
7. Cutover — manifest v2, delete Python, docs/CHANGELOG, v1.0.0.

Each phase ends with: `cargo build --release && cargo test && cargo clippy --release
-- -D warnings` green, plus the phase's live acceptance checks in `04-architecture.md`.

## Working rules for the implementing agent

- One phase at a time. Do not start the next phase's files.
- Port reason strings, log actions, prompts, and user-visible strings **verbatim** —
  the log vocabulary and CLI output are parsed by agents and are API.
- Where the spec names a Python file/line, open and read it before implementing.
- Never log, print, or persist an unredacted secret. `redact.rs` is a security
  boundary; port it regex-for-regex and keep the end-to-end assertion (a secret in
  `speak_command` argv must appear in none of stderr, `announcer.log`,
  `last-error.json`, or `status` output, while its `****last4` mask appears in all).
- All state writes: tempfile in the same directory + atomic rename. All locks:
  flock on files opened append+create, unlocked on drop.
- Every TUI entrypoint calls `crossterm::style::force_color_output(true)` before
  drawing (NO_COLOR is set in agent shells and would silently strip all color).
- Do not commit; leave changes in the working tree for review.
