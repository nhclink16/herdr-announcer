# herdr-announcer

Your agents, out loud. A [Herdr](https://herdr.dev) plugin that speaks a
one-sentence summary when a coding agent finishes its work or gets stuck
waiting for you.

![setup wizard demo](assets/demo.gif)

🔊 [What it sounds like](assets/sample-announcement.m4a) — *"Builder finished
mid-cycle proration in billing-api, all fourteen invoice tests passed,
downgrade logic left untouched."*

## Features

- **Announces `done` and `blocked`** — hear that an agent finished, or that
  it's sitting on an approval prompt, without watching the pane
- **Real summaries, not "task complete"** — one spoken sentence generated
  from the tail of the agent's terminal output
- **Three summarizers** — `codex exec` (sandboxed, read-only), any CLI LLM
  via `summary_command` (Claude Code example included), or instant
  template phrasing with no LLM at all
- **Three voices** — OS text-to-speech (free, built-in), ElevenLabs (bring
  an API key), or any custom command: SSH to the machine you're sitting at,
  an [ntfy](https://ntfy.sh) push, anything that takes text
- **Interactive setup wizard** — detects what's on your machine and writes
  the config for you
- **Never talks over itself** — simultaneous finishes queue behind a file
  lock and speak one at a time
- **Never goes silent from a broken summarizer** — LLM failures fall back
  to template phrasing; the announcement always goes out
- **Optional toast** — mirror every announcement as a Herdr notification
  (reaches you over SSH when sound can't)

## Requirements

- Herdr ≥ 0.7.0, macOS or Linux, Python 3.9+
- Linux local TTS needs `spd-say` or `espeak`; macOS needs nothing
- Optional: [Codex CLI](https://github.com/openai/codex) or any CLI LLM for
  summaries; ElevenLabs API key for a natural voice (playback via `afplay`,
  `mpv`, or `ffplay`)

## Install

```bash
herdr plugin install nhclink16/herdr-announcer
```

## Quick start

1. Install (above). The event hook is live immediately with defaults:
   announce `done` + `blocked`, Codex summaries if `codex` is on your PATH,
   local text-to-speech.
2. Run the setup wizard in a popup to tailor it:

   ```bash
   herdr plugin pane open --plugin nhclink16.announcer --entrypoint setup
   ```

3. Test the voice:

   ```bash
   herdr plugin action invoke nhclink16.announcer.test
   ```

4. Check what it's doing:

   ```bash
   herdr plugin action invoke nhclink16.announcer.status
   ```

5. Optional — bind the wizard and status to keys in
   `~/.config/herdr/config.toml`:

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

## Configuration

Config lives at `<config-dir>/config.toml` where
`herdr plugin config-dir nhclink16.announcer` prints the directory. The
wizard writes it for you; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `announce` | `["done", "blocked"]` | Agent states that trigger an announcement |
| `debounce_seconds` | `30` | Suppress repeats of the same pane+status |
| `summary` | `"codex"` | `codex`, `command`, or `template` |
| `codex_model` | `"gpt-5.6-luna"` | Model for codex mode |
| `codex_effort` | `"low"` | Reasoning effort for codex mode |
| `codex_timeout_seconds` | `45` | Codex call timeout before template fallback |
| `summary_command` | *(unset)* | argv for `summary = "command"`; transcript on stdin, `{agent}`/`{workspace}`/`{status}` substituted |
| `style` | `"announcement"` | Prompt style: `announcement`, `summary`, or `custom` |
| `custom_prompt` | *(unset)* | Your prompt for `style = "custom"` |
| `speak_command` | *(unset)* | argv that receives the text (`{text}` or stdin); overrides other voices |
| `elevenlabs_api_key` | *(unset)* | Enables ElevenLabs voice |
| `elevenlabs_voice_id` | `"21m00Tcm4TlvDq8ikWAM"` | ElevenLabs voice |
| `elevenlabs_model` | `"eleven_turbo_v2_5"` | ElevenLabs TTS model |
| `voice` | *(system default)* | macOS `say` voice name |
| `toast` | `false` | Also send each announcement as a Herdr toast |

See [config.example.toml](config.example.toml) for a fully annotated example,
including a Claude-over-ACP summarizer using the bundled
[examples/acp-summary.py](examples/acp-summary.py).

## Attached over SSH?

Sound plays on the machine running the Herdr server — a plain SSH session
cannot carry audio to your local speakers. Easiest first:

1. `toast = true` — with `[ui.toast] delivery = "terminal"` in your Herdr
   config, the summary arrives as a native notification on your local
   machine, through SSH.
2. `speak_command` — route the text somewhere audible: an ntfy push, or
   text-to-speech on the machine you're at, if the server can SSH back:

   ```toml
   speak_command = ["ssh", "my-desktop", "powershell -NoProfile -Command \"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')\""]
   ```

## How it works

Herdr emits `pane.agent_status_changed`; the hook filters to your configured
states, debounces, reads the agent's recent output through the Herdr CLI,
generates one sentence, sanitizes it, and speaks it under a playback lock.
Untrusted transcripts never reach an agentic tool with write access — codex
summaries run `--sandbox read-only --ephemeral`.

## Limitations

- **Watched work doesn't announce.** Herdr marks an agent `done` only when
  it finishes *unseen*; a pane you're actively viewing settles as `idle`.
  That's by design — the announcer covers work behind your back.
- **Audio is server-side.** See the SSH section for the two escape hatches.
- macOS and Linux only (the Herdr Windows beta lacks plugin-pane support for
  this flow); Windows *speakers* work fine via the `speak_command` SSH route.
- No per-agent filtering yet — every detected agent announces.

## Troubleshooting

Every invocation appends one line to `announcer.log` in the plugin state
directory (`announced+<backend>`, `skipped-status`, `debounced`, or `error`
with a traceback). Herdr's own view: `herdr plugin log list --plugin
nhclink16.announcer`. Silent? Check the log first — "no announceable events"
and "spoke on the wrong machine" are the usual suspects.

## License

MIT — see [LICENSE](LICENSE).
