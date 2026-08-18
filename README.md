# herdr-announcer

[![test](https://github.com/nhclink16/herdr-announcer/actions/workflows/test.yml/badge.svg)](https://github.com/nhclink16/herdr-announcer/actions/workflows/test.yml)

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
- **Failures explain themselves** — every fallback records *why*
  (`reasons=codex: timeout-first-activity`) in the log, and `status` shows
  the last error, so "why is it the robot voice?" is a glance, not an
  investigation
- **Fast provider failover** — a short startup/activity deadline can move from
  a custom provider such as Sonnet to sandboxed Codex, then fixed phrasing
- **Optional toast** — mirror every announcement as a Herdr notification
  (reaches you over SSH when sound can't)

## Requirements

- Herdr ≥ 0.7.0, macOS or Linux, Python 3.9+
- Linux local TTS needs `espeak-ng`, `espeak`, or `spd-say`; macOS needs
  nothing. One `spd-say` caveat: speech-dispatcher is often installed but
  not running, and then `spd-say` exits happily having said nothing — the
  announcer gives it five seconds and moves on to espeak, but if you *only*
  have spd-say and hear silence, that daemon is the first suspect.
- Optional: [Codex CLI](https://github.com/openai/codex) or any CLI LLM for
  summaries; ElevenLabs API key for a natural voice (playback via `afplay`,
  `mpv`, or `ffplay` — or, with none of those, raw PCM straight to `paplay`,
  `pw-play`, or `aplay`, so a stock desktop Linux needs nothing extra)

## Install

```bash
herdr plugin install nhclink16/herdr-announcer
```

## Quick start

1. Install (above). The event hook is live immediately with defaults:
   announce `done` + `blocked`, Codex summaries if `codex` is on your PATH,
   local text-to-speech. No codex? Nothing breaks — you get instant template
   phrasing instead of an LLM sentence.
2. Run the setup wizard to tailor it — either in a Herdr pane:

   ```bash
   herdr plugin pane open --plugin nhclink16.announcer --entrypoint setup
   ```

   or in any plain terminal, from the plugin directory:

   ```bash
   python3 announce.py setup
   ```

   Ctrl-C exits without writing; the wizard previews the config and asks
   before saving.

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
| `summary_fallback` | `"template"` | Failure fallback: `template`, or `codex` after `command` |
| `summary_first_activity_timeout_seconds` | `5` | Time from provider launch to its first model reasoning/output event |
| `codex_model` | `"gpt-5.6-luna"` | Model for codex mode |
| `codex_effort` | `"low"` | Reasoning effort for codex mode |
| `codex_timeout_seconds` | `45` | Codex completion window after first activity |
| `summary_command` | *(unset)* | argv for `summary = "command"`; transcript on stdin, `{agent}`/`{workspace}`/`{status}` substituted |
| `summary_command_timeout_seconds` | `60` | Bundled ACP completion window after first model activity |
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

### Following you between devices

That `speak_command` is fixed to one machine. If you attach from several —
a desktop, a laptop, sometimes sitting at the host itself —
[`examples/route-speak.sh`](examples/route-speak.sh) detects where you actually
are and speaks on every attached device, falling back to the host when nobody
is remote. It runs on macOS and Linux hosts alike — it picks the local voice
automatically (`say`, `espeak-ng`, `spd-say`, `espeak`), detects which `nc`
flavor it has for the liveness probe, and reads connections from `ss` or
`netstat`, whichever exists. List your machines and point `speak_command` at
it:

```toml
speak_command = ["/path/to/route-speak.sh"]
```

```sh
HOSTS="
desktop|10.0.0.5|windows
laptop|10.0.0.6|macos
"
```

Backends are `macos`, `windows`, `linux`, or `cmd:<anything reading stdin>`
(`cmd:ntfy publish mytopic` works fine).

**Detected is not reachable.** A sleeping or powered-off machine leaves its
`ESTABLISHED` TCP entry behind for a long time, so presence detection alone will
happily route speech to a host that is gone — and then the announcement is lost
while ssh waits out its timeout. The script probes the SSH port with a short
`nc -z` before believing a host is present, and if *no* host actually accepted
the speech it falls back to the local machine rather than dropping it. Silent
loss is the worst failure mode here: everything exits 0 and you simply stop
hearing announcements.

**`who` alone is not enough.** It only sees interactive SSH logins, which have
a TTY and a utmp entry. `herdr --remote <host>` attaches over `ssh -T` — no
TTY, no utmp entry — so `who` reports nothing and a detector built on it
silently misses every remote attach. The script also checks for an ESTABLISHED
connection arriving at its own SSH port, which is the signal that catches it.

That direction check matters. Your Herdr host likely holds *outbound* SSH
sessions to the same machines — including the ones this script opens to speak —
and matching those would make every peer look permanently present.

One more trap worth knowing if you write your own: quoting. The announcer
sanitizes every summary to letters, digits, and basic punctuation before it
reaches `speak_command` — summaries come from LLMs reading untrusted agent
output, and a summary that can smuggle `$(...)` into a remote shell command is
a security hole, not a quoting bug. Even so, prefer handing the text over
stdin (macOS `say` and `espeak-ng` both read it) instead of splicing `{text}`
into a command string; stdin has no quoting rules to get wrong.

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

![status demo](assets/status.gif)

Every invocation appends one line to `announcer.log` in the plugin state
directory (`announced+<backend>`, `skipped-status`, `debounced`,
`gave-up-waiting`, or `error` with a traceback) — and when anything fell back
along the way, a `reasons=` field says exactly what: `codex: turn.failed`,
`elevenlabs: HTTP 401`, `spd-say: timeout`. `python3 announce.py status`
shows the tail of the log, the last recorded error, every voice and player
found on the machine, and any config keys it didn't recognize (typos show up
here). Herdr's own view: `herdr plugin log list --plugin nhclink16.announcer`.
Silent? Check `status` first — "no announceable events" and "spoke on the
wrong machine" are the usual suspects, and the reasons now name the second.

## Development

```bash
python3 -m unittest discover   # 64 tests, silent, no network
shellcheck examples/*.sh
```

CI runs both on macOS and Ubuntu across Python 3.9/3.11/3.13.

## License

MIT — see [LICENSE](LICENSE).
