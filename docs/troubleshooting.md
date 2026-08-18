# Troubleshooting

![status demo](../assets/status.gif)

Start with:

```bash
python3 announce.py status
```

It shows the config in effect, every voice and audio player found on the
machine, config keys it didn't recognize (typos show up here), the last
recorded error, and the tail of the log.

## Reading the log

Every invocation appends one line to `announcer.log` in the plugin state
directory:

| `action=` | Meaning |
| --- | --- |
| `announced+summary-<s>+speak-<v>` | Spoke, via summarizer `<s>` and voice `<v>` |
| `skipped-status` | The event's state isn't in your `announce` list |
| `debounced` | Same pane+status inside `debounce_seconds` |
| `gave-up-waiting` | Playback lock held too long (90s); announcement dropped |
| `error` | Something threw; a traceback follows the line |

When anything fell back along the way, a `reasons=` field says exactly what:
`codex: turn.failed`, `elevenlabs: HTTP 401`, `spd-say: timeout`,
`config: skipped line 12`. "Why is it the robot voice?" is answered on the
line that spoke.

Herdr's own view of the hook: `herdr plugin log list --plugin
nhclink16.announcer`.

## The usual suspects

- **Silent, log says `skipped-status`** — the agent settled as `idle`
  because you were watching the pane. Herdr marks work `done` only when it
  finishes *unseen*; that's by design, the announcer covers work behind your
  back.
- **Silent, no log lines at all** — no announceable events reached the hook;
  check the plugin is enabled (`herdr plugin list`).
- **Speaks on the wrong machine** — audio is server-side; see
  [multi-host routing](multi-host.md).
- **Robot voice when you expected ElevenLabs or codex prose** — the
  `reasons=` field on that line names the fallback cause.
