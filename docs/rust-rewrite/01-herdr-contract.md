# herdr 0.8.0 platform contract (verified 2026-08-18)

> **Phase 0 amendments applied.** `tests/fixtures/FINDINGS.md` is the live-capture
> record; where it contradicts anything below, FINDINGS.md wins. Key corrections
> already folded in: event JSON is an ENVELOPE (unwrap `.data`); panes get the
> full plugin env incl. the socket; `state_labels` keys are runtime-validated
> (free-form badge keys like `muted` are REJECTED); `pane.agent_detected` also
> fires on release (`released: true`); `plugin.pane.open` rejects
> `workspace_id` + `target_pane_id` together; `plugin.list` has no config dir.

Verification legend: [V] verified live/from the binary on this machine ·
[V-doc] from the herdr-sidebar engineering logbook (verified by that team against
0.7.1/0.7.4) · [U] unverified — confirm in Phase 0.

Reference implementation for the socket client:
`~/.config/herdr/plugins/github/herdr-sidebar-7ff2582a7c8a/plugins/herdr-sidebar/src/ipc.rs`
Full RPC schema dump any time: `herdr api schema --json` (251 KB).

## Versions [V]

- `herdr 0.8.0`, socket protocol 19, API `schema_version: 1`.
- Socket on this machine: `$HERDR_SOCKET_PATH` → `~/.config/herdr/herdr.sock`.

## Socket protocol [V]

- Newline-delimited JSON. Request `{"id":"<str>","method":"<str>","params":{...}}\n`
  → one response line. Success `{"id":...,"result":{"type":"<snake_result_type>",...}}`,
  error `{"id":...,"error":{"code":"<str>","message":"<str>"}}`.
- **One request per connection.** A second request on the same connection gets
  ECONNRESET. Reconnect per call. Exception: `events.subscribe` upgrades the
  connection to a persistent stream (we do not use it — spawn-per-event hooks).
- Client pattern to copy from sidebar ipc.rs: 5s read/write timeouts, response cap
  4 MiB via `BufReader::new(stream.take(MAX))`, id `"<plugin>:<method>"`.
- Response JSON is byte-identical to what the `herdr` CLI prints.

## RPC methods we use (params verified from the schema) [V]

```jsonc
// notification.show → {"type":"notification_show","shown":bool,"reason":...}
{"title": str /*req*/, "body": str|null,
 "sound": "none"|"done"|"request",
 "position": "top-left"|"top-right"|"bottom-left"|"bottom-right"|null}

// pane.list → {"type":"pane_list","panes":[PaneInfo]}   params: {"workspace_id": str|null}
// pane.current, pane.get {"pane_id"} → PaneInfo

// pane.read
{"pane_id":str, "source":"visible"|"recent"|"recent_unwrapped"|"detection",
 "format":"text"|"ansi"(default text), "lines":u32|null, "strip_ansi":bool(true)}

// workspace.list → workspaces with workspace_id/id and label/title/name fields

// pane.report_metadata (req: pane_id, source)
{"pane_id": str, "source": str,
 "tokens": {"<^[A-Za-z0-9_-]{1,32}$>": str|null},  // MERGE semantics; null clears;
                                                    // {} is a NO-OP; strings only;
                                                    // max 16/call, 32/pane
 "title": str|null, "clear_title": bool,
 "state_labels": {str:str},   // WARNING [V Phase 0]: keys are runtime-validated
                              // (invalid_state_label) — free-form keys like
                              // "muted" are REJECTED; only status-name keys
                              // (e.g. "unknown") pass, which would masquerade
                              // the agent status. Do NOT use for a mute badge.
 "clear_state_labels": bool,
 "ttl_ms": 1..=86400000 | null}
// Verified clear shape [V Phase 0]: tokens {"muted": null} + state_labels {} +
// clear_state_labels true → a following pane.get omits both maps.

// pane.current requires {"caller_pane_id": str} to resolve the caller [V Phase 0].
// pane.read result nests the payload under result.read [V Phase 0].
// PaneInfo omits optional fields instead of serializing null [V Phase 0].

// plugin.pane.open [V Phase 0]: params plugin_id, entrypoint, target_pane_id,
// direction, cwd, env, focus → result.plugin_pane.pane = PaneInfo.
// Passing workspace_id AND target_pane_id together → invalid_params.

// plugin.pane.open — exists [V in method list]; params shape is a Phase 0 capture.
// plugin.list — includes config-dir/plugin-root info; fallback dir discovery.
```

`PaneInfo` (also nested inside `pane_created`/`pane_updated`/`pane_moved` events):
```jsonc
{"pane_id","terminal_id","workspace_id","tab_id","focused","agent_status","revision",
 "label":str|null,"title":str|null,"cwd","foreground_cwd",
 "agent":str|null,"display_agent":str|null,"agent_session":{...}|null,
 "state_labels":{str:str},"tokens":{str:str},
 "terminal_title","terminal_title_stripped","scroll":{...}|null}
```
`AgentStatus` enum: `idle | working | blocked | done | unknown`.

## Manifest schema (`herdr-plugin.toml`) [V — from the binary's serde schema]

- Top-level: `id`, `name`, `version`, `description`, `min_herdr_version`,
  `platforms`, arrays `build`, `startup`, `panes`, `actions`, `events`,
  `link_handlers`.
- `PluginPlatform` = `linux|macos|windows`. Item-level `platforms` allowed
  everywhere; `platforms = []` is rejected (omit instead).
- `[[actions]]`: **id, title, command** + `description`,
  `contexts`: subset of `global|workspace|tab|pane|selection` (optional, real on
  0.8.0), `platforms`.
- `[[panes]]`: **id, title, command** + `placement` =
  `overlay|popup|split|tab|zoomed` (default overlay), `width`/`height`
  (integer cells or "NN%", **popup only**), `description`, `platforms`.
- `[[events]]`: **on, command**. Unknown event names are non-fatal warnings
  (visible in `herdr plugin list --json` → `.warnings`) — that is the safe
  self-test after linking.
- `[[build]]`: **command**.
- Hook/action commands run with **cwd = plugin root**.

## Event hooks

Allowed `on =` names (26): all `workspace.*`
(created/updated/metadata_updated/renamed/moved/reordered/focused/closed),
`worktree.created/opened/removed`, `tab.created/closed/renamed/moved/focused`,
`pane.created/closed/updated/focused/moved/output_changed/exited/agent_detected/
agent_status_changed`, `layout.updated`. [V]

`HERDR_PLUGIN_EVENT_JSON` is the **EventEnvelope** — [V Phase 0, corrects the
earlier claim]: `{"event": "<underscore_name>", "data": {"type":
"<underscore_name>", ...}}`. **Unwrap `.data` first**, then the flat/nested
rules below apply to the payload. (`HERDR_PLUGIN_EVENT` env var uses the dotted
spelling.) Payload shapes after unwrapping:

```jsonc
pane_agent_status_changed: {type, pane_id, workspace_id, agent_status,
                            agent?, display_agent?, title?, state_labels{}}
pane_agent_detected:       {type, pane_id, workspace_id, agent?}            // initial detection
pane_agent_detected (release): {type, pane_id, workspace_id, agent?, final_status, released:true}
                           // ALSO fires when an agent leaves — treat released/
                           // final_status as optional; announce-on-detect must
                           // skip released:true events [V Phase 0]
pane_closed:               {type, pane_id, workspace_id}
pane_exited:               {type, pane_id, workspace_id}
pane_created / pane_updated: {type, pane: PaneInfo}   // NESTED — no top-level pane_id!
tab_created:               {type, tab: TabInfo}       // nested
workspace_created:         {type, workspace: WorkspaceInfo}  // nested
```
**Trap:** `pane_created`, `pane_updated`, `pane_moved`, `tab_created`,
`workspace_created` nest their object; everything else is flat. Keep the Python
`parse_event` behavior: prefer top-level string `pane_id`/`agent_status`, fall back
to a recursive first-string-for-key search (`announcer/herdr.py:28`).

### Env injected into hook/action commands [V]

```
HERDR_SOCKET_PATH   HERDR_BIN_PATH   HERDR_PLUGIN_ROOT
HERDR_PLUGIN_CONFIG_DIR   HERDR_PLUGIN_STATE_DIR
HERDR_PLUGIN_EVENT (event name)   HERDR_PLUGIN_EVENT_JSON (payload)
HERDR_PLUGIN_ACTION_ID (actions)  HERDR_PLUGIN_CONTEXT_JSON (actions/plugin panes)
HERDR_WORKSPACE_ID
```

**Panes get the FULL plugin env** [V Phase 0, corrects the sidebar-era claim]:
`HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_PLUGIN_ROOT`,
`HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONTEXT_JSON`,
`HERDR_PLUGIN_ID`, `HERDR_PLUGIN_ENTRYPOINT_ID`, plus the three topology IDs.
Keep the conventional-path fallback in `paths.rs` anyway
(`~/.config/herdr/plugins/config/nhclink16.announcer`,
`~/.local/state/herdr/plugins/nhclink16.announcer`; macOS state honors
`$XDG_STATE_HOME`) — `plugin.list` does NOT expose a config dir, so env +
convention is the whole resolution story. Hooks additionally get
`HERDR_PLUGIN_CONTEXT_JSON`/`HERDR_PLUGIN_ID`, but a `pane.closed` hook may lack
`HERDR_TAB_ID` (the pane is already gone) — never require it.

### Hook execution semantics [V-doc]

- Focus events fire in bursts and hook invocations run **concurrently** — all
  shared state must be flock-guarded (ours already is).
- herdr rate-limits plugin spawns (`plugin_command_limit_reached` error code).
- Debug surface: `herdr plugin log list --plugin nhclink16.announcer` captures
  status/exit_code/stdout/stderr/event/action per invocation.
- Never hook `pane.*` from a command that itself creates panes (feedback loop).

## Pane-context actions [V shape / U menu wiring]

`HERDR_PLUGIN_CONTEXT_JSON` = `PluginInvocationContext`, **all fields optional**:
```jsonc
{"workspace_id","workspace_label","workspace_cwd","tab_id","tab_label",
 "focused_pane_id",       // ← the pane the action was invoked on
 "focused_pane_agent",    // e.g. "claude"
 "focused_pane_status",   // AgentStatus
 "selected_text","clicked_url","link_handler_id",
 "invocation_source","correlation_id","worktree":{...}}
```
There is **no `pane_id` field** — use `focused_pane_id`, falling back to the
`pane.current` RPC, then error with a toast. `plugin.action.invoke` echoes the
resolved context back in `.result.context` — use that as the Phase 0 probe.

## Cross-platform pitfalls [V-doc]

- crossterm honors `NO_COLOR`; Claude Code shells set it → call
  `crossterm::style::force_color_output(true)` in every TUI entrypoint.
- Modifier+Enter is indistinguishable from Enter (no keyboard-enhancement
  protocol in herdr panes) — unmodified keys must suffice.
- AltGr arrives as CONTROL|ALT on `Char` events — treat CONTROL+ALT chars as text,
  only CONTROL-without-ALT as shortcut, or German/French/Nordic layouts lose
  `@ { [ ] } \`.
- Avoid VS16 emoji (inconsistent widths). Our glyph set is plain BMP symbols.
- Right-click is intercepted by herdr's own context menu — do not design around
  right-click in the dashboard.

## Testing hooks headless [V-doc recipes]

- Dev loop: `herdr plugin link .` → `herdr plugin list --json` (check `.warnings`)
  → `herdr plugin action invoke nhclink16.announcer.<action>` →
  `herdr plugin log list --plugin nhclink16.announcer`.
- Drive a TUI: `pane split --current --direction right --no-focus` → `pane run <id>
  "<abs path>"` → `pane send-keys <id> Down Enter` → `pane read <id> --source visible`.
  Mouse is drivable by sending SGR sequences (`ESC[<0;X;YM` press, `...m` release,
  `ESC[<35;X;YM` motion, 1-based coords) via `pane.send_input`.
- Run a test TUI with `HERDR_PANE_ID=''` so it does not register as the real pane.

## Release flow [V-doc]

Bump version in Cargo.toml + herdr-plugin.toml + Cargo.lock (+ CHANGELOG heading);
commit, tag `vX.Y.Z`, push branch and tag; `gh release create` (tag alone does not
update the Latest badge). Install syntax users see:
`herdr plugin install nhclink16/herdr-announcer --yes` (`--yes` AFTER the target).
