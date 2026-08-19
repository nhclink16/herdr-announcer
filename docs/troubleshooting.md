# Troubleshooting

![status demo](../assets/status.gif)

Start with:

```bash
target/release/herdr-announcer status
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
| `muted-agent` | The event's agent type is in `mute_agents` |
| `muted-pane` | The target pane has an active mute or pane snooze |
| `snoozed` | A global snooze is active |
| `debounced` | Same pane+status inside `debounce_seconds` |
| `cleanup` | A closed or exited pane's mute state was removed |
| `gave-up-waiting` | Playback lock held too long (90s); announcement dropped |
| `error` | Something threw; a traceback follows the line |

When anything fell back along the way, a `reasons=` field says exactly what:
`codex: turn.failed`, `elevenlabs: HTTP 401`, `spd-say: timeout`,
`config: mute_agents ignored`. "Why is it the robot voice?" is answered on the
line that spoke.

Herdr's own view of each hook/action process, including its exit code and
stderr:

```bash
herdr plugin log list --plugin nhclink16.announcer
```

Use this first when `announcer.log` has no matching invocation: it distinguishes
a hook that never ran from one that failed before the announcer could log.

## Herdr socket failures

The Rust plugin talks only to the Unix socket in `HERDR_SOCKET_PATH`; it never
falls back to scraping the Herdr CLI.

- `herdr: no socket path` means the hook, action, or pane was launched without
  `HERDR_SOCKET_PATH`. Reproduce it through Herdr rather than invoking hook mode
  in a bare shell.
- `herdr-read: herdr rpc pane.read: <code> <message>` means Herdr returned a
  structured RPC error while reading the pane transcript.
- `herdr-workspace: herdr rpc workspace.list: <code> <message>` means workspace
  context lookup failed.
- `toast: herdr rpc notification.show: <code> <message>` means only the
  notification failed; speech can still succeed.
- Transport failures retain their OS detail after the operation prefix, such
  as connection refused, timed out, or an unexpectedly closed response.

Each request opens a fresh connection. A raw socket probe must send one
newline-delimited JSON request and read one response before closing.

## The usual suspects

- **Silent, log says `skipped-status`** — the agent settled as `idle`
  because you were watching the pane. Herdr marks work `done` only when it
  finishes *unseen*; that's by design, the announcer covers work behind your
  back.
- **Silent, no log lines at all** — no announceable events reached the hook;
  check `herdr plugin list --json`, then inspect the plugin process log above.
- **Speaks on the wrong machine** — audio is server-side; see
  [multi-host routing](multi-host.md).
- **Robot voice when you expected ElevenLabs or codex prose** — the
  `reasons=` field on that line names the fallback cause.
