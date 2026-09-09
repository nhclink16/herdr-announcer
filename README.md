<div align="center">

# herdr-announcer

**Your agents, out loud.** A [Herdr](https://herdr.dev) plugin that speaks a
one-sentence summary when a coding agent finishes its work or gets stuck
waiting for you.

[![ci](https://github.com/nhclink16/herdr-announcer/actions/workflows/ci.yml/badge.svg)](https://github.com/nhclink16/herdr-announcer/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/nhclink16/herdr-announcer)](https://github.com/nhclink16/herdr-announcer/releases)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![herdr](https://img.shields.io/badge/herdr-%E2%89%A5%200.8.0-8A2BE2)

[Install](#install) · [Quick start](#quick-start) ·
[Configuration](docs/configuration.md) ·
[Voices & multi-host](docs/multi-host.md) ·
[Troubleshooting](docs/troubleshooting.md) ·
[Development](docs/development.md)

</div>

![announcer dashboard](assets/dashboard.gif)

🔊 [What it sounds like](assets/sample-announcement.m4a) — *"Builder finished
mid-cycle proration in billing-api, all fourteen invoice tests passed,
downgrade logic left untouched."*

## Install

Herdr 0.8.0 or newer and Rust 1.88 or newer with `cargo` are required.
The plugin's `[[build]]` entry runs `cargo build --release` during install:

```bash
herdr plugin install nhclink16/herdr-announcer --yes
```

For a local checkout, build first and link the repository:

```bash
cargo build --release
herdr plugin link .
```

## Quick start

The event hook is live immediately with defaults: announce `done` +
`blocked`, Codex summaries if `codex` is on your PATH, local
text-to-speech.

> [!TIP]
> No codex? Nothing breaks — you get instant template phrasing instead of
> an LLM sentence.

Open the control panel — recent announcements with their reasons, snooze
(5m / 30m / 2h / until tomorrow), mutes, toggles, and a voice test. Use the
keyboard or mouse, scroll the log with the wheel or Page Up/Page Down, and
press Esc to close it. Every control is also available as a palette action:

```bash
herdr plugin pane open --plugin nhclink16.announcer --entrypoint dashboard
```

![setup wizard demo](assets/demo.gif)

Tailor it with the wizard, then test the voice:

```bash
herdr plugin pane open --plugin nhclink16.announcer --entrypoint setup
herdr plugin action invoke nhclink16.announcer.test
```

(Or from the plugin directory in any terminal:
`target/release/herdr-announcer setup`. Ctrl-C exits without writing; the
wizard previews the config and asks before saving.)

## Actions and panes

The manifest exposes 12 palette actions and two pane entrypoints:

| ID | Context | Description |
| --- | --- | --- |
| `mute-pane` | pane | Toggle announcements for this pane; the mute dies with the pane |
| `snooze-pane` | pane | Cycle this pane's snooze: 5m, 30m, 2h, off |
| `announce-now` | pane | Summarize and speak this pane's current status immediately |
| `test` | workspace | Speak a sample announcement with the configured voice |
| `status` | workspace | Show configuration, detected tools, and recent log lines |
| `open-dashboard` | workspace | Open the live dashboard popup |
| `snooze-5m` | workspace | Silence all announcements for 5 minutes |
| `snooze-30m` | workspace | Silence all announcements for 30 minutes |
| `snooze-2h` | workspace | Silence all announcements for 2 hours |
| `snooze-tomorrow` | workspace | Silence all announcements until 8:00 tomorrow |
| `snooze-off` | workspace | End any active global snooze immediately |
| `toggle-toast` | workspace | Toggle Herdr notification mirroring |
| `setup` | pane entrypoint | Open the interactive setup wizard in a split |
| `dashboard` | pane entrypoint | Open the live dashboard in a 94×30 popup |

<details>
<summary>Bind the wizard and status to keys</summary>

In `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+a"
type = "shell"
command = "herdr plugin pane open --plugin nhclink16.announcer --entrypoint setup"
description = "announcer setup"

[[keys.command]]
key = "prefix+shift+a"
type = "plugin_action"
command = "nhclink16.announcer.status"
description = "announcer status"
```

</details>

## Features

- **A dashboard in a popup** — snooze, toggles, the recent-announcement
  log with failure reasons, per-agent and per-pane mute state, mouse controls,
  scrolling, and a voice test, one keybind away; agents can drive every
  control through `herdr plugin action invoke`
- **Mute exactly what you mean** — silence an agent type in config or toggle a
  pane until it closes; pane actions can also snooze one pane or announce it
  immediately
- **Real summaries, not "task complete"** — one spoken sentence generated
  from the tail of the agent's terminal output, by sandboxed `codex exec`,
  any CLI LLM, or instant template phrasing with no LLM at all
- **Three voices** — OS text-to-speech (free, built-in), ElevenLabs, or any
  custom command: SSH to the machine you're sitting at, an
  [ntfy](https://ntfy.sh) push, [piper](https://github.com/rhasspy/piper),
  anything that takes text
- **Never goes silent, never talks over itself** — LLM failures fall back to
  template phrasing under a fast activity deadline; simultaneous finishes
  queue behind a playback lock
- **Failures explain themselves** — every fallback records *why*
  (`reasons=codex: timeout-first-activity`) in the log, and `status` shows
  the last error, so "why is it the robot voice?" is a glance, not an
  investigation
- **Optional toast** — mirror every announcement as a Herdr notification
  (reaches you over SSH when sound can't)

## How it works

```mermaid
flowchart LR
    E["Herdr event"] --> F{"announce<br/>list?"}
    F -- no --> X["skip"]
    F -- yes --> M{"muted or<br/>snoozed?"}
    M -- yes --> X
    M -- no --> D{"debounced?"}
    D -- yes --> X
    D -- no --> T["read transcript<br/>via Herdr socket"]
    T --> S["summarize<br/>codex · command · template"]
    S -- failure + reason --> B["template fallback"]
    S --> Z["sanitize"]
    B --> Z
    Z --> L["playback lock"] --> V["🔊 speak"]
```

Untrusted transcripts never reach an agentic tool with write access — codex
summaries run `--sandbox read-only --ephemeral` — and summaries are
sanitized to plain words before any `speak_command` sees them.

## Requirements & limitations

- Herdr ≥ 0.8.0, macOS or Linux, and a stable Rust toolchain for installation.
  Linux local TTS wants
  `espeak-ng`, `espeak`, or `spd-say` ([one caveat](docs/configuration.md#voices));
  macOS needs nothing.
- **Watched work doesn't announce.** Herdr marks an agent `done` only when
  it finishes *unseen*; a pane you're actively viewing settles as `idle`.
  That's by design — the announcer covers work behind your back.
- Herdr 0.8.0 accepts the per-pane `muted` metadata token but does not render a
  visible pane badge for it; the dashboard and action toasts show mute state.

> [!WARNING]
> Audio plays on the machine running the Herdr server. Attached over SSH?
> [Two escape hatches](docs/multi-host.md), including a router that follows
> you between devices.

## License

MIT — see [LICENSE](LICENSE).
