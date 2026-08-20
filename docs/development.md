# Development

## Layout

```
announce.py       entrypoint + orchestration; a thin facade over announcer/
dashboard.py      compatibility entrypoint for plugin actions and imports
announcer/
  dashboard.py    live status TUI and dashboard command routing
  config.py       defaults, TOML loading (tomllib, tiny-TOML fallback on 3.9)
  config_io.py    compatibility-safe config editing for noninteractive UIs
  fingerprints.py persisted per-pane announcement content and delivery lock
  herdr.py        Herdr CLI client, event parsing
  log.py          invocation log writing and tail parsing
  paths.py        config/state directory resolution outside Herdr
  summarize.py    template / codex / command summarizers, sanitizer
  speech.py       TTS backends, ElevenLabs, playback + debounce locks
  snooze.py       snooze state, duration parsing, and labels
  deadline.py     the shared two-phase timeout machine
  tui.py          raw-terminal widget kit
  wizard.py       setup wizard
examples/         acp-summary.py (Claude over ACP), route-speak.sh
```

The two-phase deadline (a short first-activity window, then a fresh
completion window) lives in `announcer/deadline.py` and is shared by the
codex summarizer and `examples/acp-summary.py`.

## Tests

```bash
python3 -m unittest discover   # silent — no audio, no network
shellcheck examples/*.sh
```

CI runs both on macOS and Ubuntu across Python 3.9, 3.11, and 3.13 — 3.9
matters because it's what macOS ships as `/usr/bin/python3`, and it
exercises the tiny-TOML fallback that `tomllib` replaces on newer
interpreters.

Tests that involve timing use generous margins on purpose; the properties
under test are about *ordering* (completion may outlive the first-activity
deadline), not speed. If you add one, assume a cold CI runner can pause
your thread for 100ms whenever it likes.

## Security posture

Summaries are LLM prose generated from **untrusted agent transcripts**:

- codex runs `--sandbox read-only --ephemeral --ignore-user-config`
- the bundled ACP example disables tools, inherited settings, and MCP servers
- every summary is sanitized to letters, digits, and basic punctuation
  before it reaches any `speak_command`

Keep all three when touching the pipeline.
