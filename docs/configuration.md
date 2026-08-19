# Configuration

Config lives at `<config-dir>/config.toml` where
`herdr plugin config-dir nhclink16.announcer` prints the directory. The
[setup wizard](../README.md#quick-start) writes it for you; every key is
optional, and the file is safe to hand-edit. Wizard re-runs preserve comments,
unknown keys, and tables while updating the settings you explicitly choose.

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
| `mute_agents` | `[]` | Case-insensitive agent types to silence, such as `["codex"]` |
| `announce_on_detect` | `false` | Announce a newly detected agent once; release events never announce |

See [config.example.toml](../config.example.toml) for a fully annotated
example, including a Claude-over-ACP summarizer using the bundled
[examples/acp-summary.py](../examples/acp-summary.py).

## Global and pane-specific silence

Dashboard snooze actions (`5m`, `30m`, `2h`, `tomorrow`, and `off`) suppress
all automatic announcements. Pane-context actions are narrower:

- **Mute announcer for this pane** toggles an until-close mute. Herdr receives
  a `muted` metadata token, and the entry is removed automatically when the
  pane closes or exits.
- **Snooze announcer for this pane** cycles 5 minutes → 30 minutes → 2 hours →
  off. An expired timed entry is removed when mute state is next read.
- **Announce this pane now** reads and announces the focused pane immediately.
  It intentionally bypasses state filtering, agent and pane mutes, global
  snooze, and debounce, while still using the normal summary, speech, and
  playback-lock pipeline.

`mute_agents` applies before pane mutes and global snooze. It first compares
the event's machine agent type, then its display agent, without regard to case.
`announce_on_detect` uses the same mute, snooze, and debounce gates; a Herdr
agent-release event is always ignored.

## Voices

The first configured backend wins:

1. **`speak_command`** — any argv that takes text. `{text}` is substituted,
   or, with no `{text}` argument, the text arrives on stdin (prefer stdin —
   no quoting rules to get wrong). Announcements are sanitized to plain
   words before they reach this command. This is the escape hatch for
   [multi-host routing](multi-host.md), ntfy pushes, or a local neural TTS
   like [piper](https://github.com/rhasspy/piper).
2. **ElevenLabs** — set an API key. Plays via `afplay`, `mpv`, or `ffplay`,
   or falls back to raw PCM through `paplay`/`pw-play`/`aplay` so desktop
   Linux needs no extra decoder. The player is checked *before* the paid
   API call.
3. **OS text-to-speech** — macOS `say` (set `voice` to pick one), or on
   Linux `spd-say`, `espeak-ng`, `espeak` — first that works.

> [!NOTE]
> speech-dispatcher is often installed but not running, and then `spd-say`
> exits happily having said nothing. The announcer gives it five seconds and
> moves on to espeak; if you *only* have spd-say and hear silence, that
> daemon is the first suspect.
