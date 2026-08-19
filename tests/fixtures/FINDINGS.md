# Phase 0 live Herdr contract findings

## Surprises and required spec amendments

These findings contradict or materially qualify `docs/rust-rewrite/01-herdr-contract.md` and should be resolved before Phase 2 or Phase 4 implementation.

1. **`HERDR_PLUGIN_EVENT_JSON` is not the flat `EventData` payload.** It is an envelope of the form `{"event":"pane_agent_status_changed","data":{"type":"pane_agent_status_changed",...}}`. The `EventData` inside `.data` is flat for all four captured events. Phase 2 event parsing must unwrap `.data` before applying the documented top-level/recursive field lookup. See all `hook-*.json` fixtures.
2. **Plugin pane entrypoints receive the full plugin environment, including the socket.** The capture contains `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_PLUGIN_ROOT`, `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONTEXT_JSON`, `HERDR_PLUGIN_ID`, and `HERDR_PLUGIN_ENTRYPOINT_ID`, in addition to workspace/tab/pane IDs. This directly contradicts the claim that panes receive only the three topology IDs. See `pane-entrypoint-env.json`.
3. **Hook invocations also receive more context than documented.** They include `HERDR_PLUGIN_CONTEXT_JSON`, `HERDR_PLUGIN_ID`, `HERDR_ENV`, and (while still resolvable) pane/tab IDs. The `pane.closed` hook lacked `HERDR_TAB_ID` after the pane had been removed, so hook code must not require it.
4. **`state_labels` keys are runtime-validated, despite the schema declaring an arbitrary string map.** Keys `contract-probe` and `muted` were rejected with `invalid_state_label`; `unknown` was accepted. Therefore the planned Phase 4 shape `{"muted":"muted"}` will fail on Herdr 0.8.0. The successful test used `{"unknown":"muted"}` with token `{"muted":"1"}`. See `socket-pane-report-metadata-set-invalid-label.json` and `socket-pane-report-metadata-set.json`.
5. **A distinct pane mute badge could not be confirmed. BLOCKED.** With a fake agent in state `unknown`, the accepted override appeared in `pane.get` as `state_labels: {"unknown":"muted"}`, but a focused live visual inspection did not show a separate `muted` badge in Herdr's built-in workspace/agent list. This looks like a status-label override, not a free-form badge facility. Phase 4 needs a supported badge mechanism or a revised acceptance criterion.
6. **`plugin.pane.open` rejects a split request containing both `workspace_id` and `target_pane_id`.** Both fields are independently optional in the JSON schema, but the combined request returned `invalid_params`. Omitting `workspace_id` and targeting the existing pane succeeded; the manifest's split placement was used. See `socket-plugin-pane-open-conflicting-targets.json` and `socket-plugin-pane-open.json`.
7. **`plugin.list` does not include a config directory.** It includes `plugin_root` and `manifest_path`, but no `config_dir` or `state_dir`. The CLI-only `herdr plugin config-dir` returned the conventional config path during the probe. A socket-only implementation cannot rely on `plugin.list` for the config directory. See `socket-plugin-list.json`.
8. **Initial and released `pane.agent_detected` payloads differ.** Initial detection omitted `released`; release included `released: true` and `final_status`. Parsers must treat both fields as optional. See `hook-pane-agent-detected.json` and `hook-pane-agent-detected-released.json`.
9. **The action CLI resolves the UI-focused pane, not the shell pane from which the CLI command is run.** Running `herdr plugin action invoke` inside a background scratch pane initially returned the unrelated UI-focused pane. Focusing the scratch workspace before invocation produced the correct real context. This reinforces that code must consume `focused_pane_id`, not infer a caller from process topology.
10. **Right-click pane-menu wiring was not exercised. BLOCKED.** The `contexts = ["pane"]` action was invoked through `plugin.action.invoke` after focusing the scratch pane, which verified real context generation and delivery but not menu discoverability or mouse selection.

## Probe identity and cleanup

- Date: **2026-08-18** (`America/New_York`).
- Herdr: **0.8.0**.
- Socket protocol: **19**; API schema version: **1**.
- Throwaway plugin: `nhclink16.contract-probe`, linked from a `mktemp` directory under `/tmp`.
- Scratch workspace: `w7` (`contract-probe-phase0`); root pane `w7:p1`; probe plugin panes `w7:p2` and `w7:p3`. The installed sidebar plugin automatically created `w7:p4` inside the scratch workspace.
- The existing `nhclink16.announcer` manifest/plugin was not modified. No pre-existing pane or workspace was closed.
- Cleanup verified live: `w7` was closed, `plugin.unlink` returned `removed: true`, filtered `plugin.list` returned an empty array, and the exact probe temp/config/state paths were removed. The temp and state paths were deleted after the fixture copies were made; config/state paths were moved to the recoverable desktop trash where supported.

## Verified hook contract

- Event hook commands ran successfully from the plugin root using the relative command `python3 dump.py hook`.
- `HERDR_PLUGIN_EVENT` uses dotted names such as `pane.agent_status_changed`; the envelope's `event` and nested `data.type` use underscore spelling.
- After unwrapping `.data`, all four captured payloads place `pane_id` and `workspace_id` at the top level:
  - `pane_agent_status_changed`: `pane_id`, `workspace_id`, `agent_status`, optional `agent`, and `state_labels` when present.
  - `pane_agent_detected` initial: `pane_id`, `workspace_id`, and `agent`.
  - `pane_agent_detected` release: the preceding fields plus `final_status` and `released: true`.
  - `pane_closed` and `pane_exited`: `pane_id` and `workspace_id`.
- `pane.closed` and `pane.exited` are distinct lifecycle signals in this test. Closing a probe plugin pane produced `pane.closed`; sending `exit` to the scratch shell produced `pane.exited` and removed the pane without a matching `pane.closed` capture.
- Reporting the fake agent as `working` produced `working`. Reporting it as `idle` while its background tab was unseen produced `done`, confirming Herdr's seen/unseen idle-versus-done behavior.
- The hook environment consistently included socket/bin/plugin root/config/state paths. Event context correlated to the scratch pane and used `invocation_source: "api"`.

## Verified pane-context action contract

- `contexts = ["pane"]` linked without warnings.
- The real scratch invocation context contains `focused_pane_id: "w7:p1"` and no `pane_id` field.
- It also contains workspace/tab IDs and labels, workspace and focused-pane cwd values, `focused_pane_status`, `invocation_source: "cli"`, and `correlation_id`.
- `HERDR_PLUGIN_CONTEXT_JSON` parsed to the same context echoed by `plugin.action.invoke`.
- Action env included the socket, bin, plugin root/config/state paths, plugin/action IDs, and workspace/tab/pane IDs.

## Verified plugin-pane entrypoint contract

- `HERDR_SOCKET_PATH` **is present** in plugin pane entrypoints.
- `HERDR_PLUGIN_CONTEXT_JSON` is present and identified the original target as `focused_pane_id: "w7:p1"`, with `invocation_source: "api"` and correlation ID `plugin-pane`.
- `HERDR_PLUGIN_ENTRYPOINT_ID` was `capture-env`; `HERDR_PANE_ID` identified the newly created pane.
- The entrypoint also received config/state/root/bin paths and all three topology IDs.

## Verified socket contract

- Requests and responses are newline-delimited JSON. Each normal transcript opened a fresh Unix socket connection and contains both the exact line and parsed object.
- A second request on one connection never received a second response. Three repetitions produced two `ConnectionResetError` results with errno 104 (`ECONNRESET`) and one `BrokenPipeError` with errno 32 (`EPIPE`) at send time. See `socket-second-request-econnreset.json`.
- `pane.read` accepted `source: "recent_unwrapped"`, `format: "text"`, `lines`, and `strip_ansi`; its result type is `pane_read` and the read payload is nested under `result.read`.
- `workspace.list`, filtered `pane.list`, `pane.current` with `caller_pane_id`, and `pane.get` returned `workspace_list`, `pane_list`, `pane_current`, and `pane_info` respectively. Optional `PaneInfo` fields are omitted rather than serialized as `null`.
- `notification.show` accepted separate title/body values, `sound: "none"`, and `position: "top-right"`; the visible toast was observed and the response was `shown: true`, `reason: "shown"`.
- Successful `pane.report_metadata` returned `{"type":"ok"}`. A following `pane.get` showed the merged token and state-label maps.
- Metadata clear shape verified: `tokens: {"muted": null}`, `state_labels: {}`, and `clear_state_labels: true`. A following `pane.get` omitted both maps, confirming they were cleared.
- Successful `plugin.pane.open` parameters for the split test were `plugin_id`, `entrypoint`, `target_pane_id`, `direction`, `cwd`, `env`, and `focus`; the response nested the created `PaneInfo` under `result.plugin_pane.pane`.
- Filtered `plugin.list` returned exactly the probe plugin and exposed its action/event/pane definitions, manifest path, plugin root, source, platforms, version, and enabled state.

## Fixture index

- Hook/action/pane env captures: `hook-*.json`, `action-pane-context.json`, `pane-entrypoint-env.json`.
- Required RPC transcripts: `socket-pane-read.json`, `socket-workspace-list.json`, `socket-pane-list.json`, `socket-pane-current.json`, `socket-pane-get.json`, `socket-notification-show.json`, `socket-pane-report-metadata-set.json`, `socket-pane-report-metadata-clear.json`, `socket-plugin-pane-open.json`, and `socket-plugin-list.json`.
- Supporting verification: both post-metadata `pane.get` transcripts, the invalid-label transcript, the conflicting-targets plugin-pane transcript, and the second-request reset transcript.
