# Changelog

## [1.0.0] - 2026-08-19

### Added

- Pane-context actions to mute or snooze one pane and to announce it
  immediately: `mute-pane`, `snooze-pane`, and `announce-now`.
- A `workspace.closed` cleanup hook: closing a workspace emits no `pane.closed`
  events on herdr 0.8.0, so pane mutes are pruned by workspace prefix instead.
- Case-insensitive per-agent-type mutes through `mute_agents`.
- Optional announcements when an agent is detected through
  `announce_on_detect`.
- New hooks for agent detection and pane close/exit cleanup.
- A mouse-enabled dashboard with a scrollable recent-event log.

### Changed

- Rewrote the plugin in Rust while preserving the Python behavior and output
  contracts.
- Replaced Herdr CLI scraping with socket-only integration.
- The setup wizard now preserves comments, unknown keys, and tables when it
  updates `config.toml`.
- Raised `min_herdr_version` to 0.8.0.

### Breaking

- Removed the Python implementation and its Python runtime requirement.
