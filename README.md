# herdr-announcer

A [Herdr](https://herdr.dev) plugin that speaks a short summary out loud when a
coding agent finishes its work or gets stuck waiting for your input.

By default it announces with your OS's built-in text-to-speech (free, robotic).
Drop an ElevenLabs API key into the config for a natural voice, or point
`speak_command` at anything — an ntfy push, TTS on another machine over SSH —
if you're not sitting where the Herdr server runs.

Summaries are one spoken sentence generated from the tail of the agent's
terminal output ("builder finished mid-cycle proration and all fourteen
invoice tests passed") — by `codex exec` by default, or by any CLI you point
`summary_command` at (Claude Code, Gemini CLI, a local model). No LLM at all?
Set `summary = "template"` for instant fixed phrasing. A missing or failing
summarizer never mutes the announcement; it falls back to the template.

Three prompt styles: `announcement` (radio-voice, leads with the agent's
name), `summary` (plain factual report), or `custom` (write your own prompt;
`{agent}`, `{workspace}`, and `{status}` are filled in). See
[config.example.toml](config.example.toml). An [example ACP
client](examples/acp-summary.py) shows how to use Claude over the Agent
Client Protocol as the summarizer.

Simultaneous finishes don't talk over each other: announcements take a file
lock around playback, so they queue and speak one at a time.

## Install

```bash
herdr plugin install nhclink16/herdr-announcer
```

## Configure (optional — works with zero config)

```bash
herdr plugin config-dir nhclink16.announcer   # prints the config directory
cp config.example.toml <that-dir>/config.toml
```

See [config.example.toml](config.example.toml) for every setting: which states
to announce (`done`, `blocked`, ...), debounce window, summary model, and the
voice backends.

## Test it

```bash
herdr plugin action invoke nhclink16.announcer.test
```

## Requirements

- Herdr >= 0.7.0, macOS or Linux, Python 3.9+
- Optional: [Codex CLI](https://github.com/openai/codex) for LLM summaries —
  or any other CLI LLM via `summary_command`
- Optional: ElevenLabs API key for a natural voice
- Linux local TTS uses `spd-say` or `espeak` if present

## Attached over SSH?

Sound plays on the machine running the Herdr server — a plain SSH session
cannot carry audio to your local speakers. Your options, easiest first:

1. `toast = true` — the summary arrives as a Herdr notification instead.
   With `[ui.toast] delivery = "terminal"` in your Herdr config, that
   notification travels through SSH and shows natively on your local machine.
2. `speak_command` — route the text anywhere you can hear it:
   an [ntfy](https://ntfy.sh) push to your phone, or text-to-speech on the
   machine you're sitting at, if the server can SSH back to it:

   ```toml
   speak_command = ["ssh", "my-desktop", "powershell -NoProfile -Command \"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')\""]
   ```

## Troubleshooting

Every invocation logs one line to `announcer.log` in the plugin state
directory; Herdr's own view is `herdr plugin log list --plugin
nhclink16.announcer`. If you hear nothing over SSH: sound plays on the machine
running the Herdr server — use `speak_command` to route it to where you are.
