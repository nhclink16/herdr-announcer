# Development

The 1.0 implementation is one Rust crate at the repository root:

```text
Cargo.toml
src/
  main.rs          CLI dispatch
  config.rs        defaults and validation
  config_write.rs  locked, comment-preserving writes and rollback
  event.rs         Herdr event/context decoding
  ipc.rs           one-request-per-connection Herdr socket client
  hook.rs          announcement pipeline
  summarize.rs     template, command, and Codex summaries
  speech.rs        custom, ElevenLabs, and local speech
  actions.rs       pane-context actions
  mute.rs          persistent per-pane mute state
  snapshot.rs      stable non-TTY dashboard output
  tui/             dashboard, wizard, widgets, and theme
tests/              integration, fixture, render, and golden tests
examples/           standalone ACP summarizer and speech router
```


## Test loop

Run the same gates as CI:

```bash
cargo fmt --check
cargo build --release
cargo test
cargo clippy --release -- -D warnings
scripts/check-version.sh
shellcheck examples/*.sh scripts/*.sh
```

Timing tests use generous margins intentionally. They assert ordering—most
notably that the completion window starts at first model activity—not raw
speed.

The status and snapshot golden files under `tests/golden/` are the
compatibility referees for externally parsed output; regenerating them is a
deliberate, reviewed act.

## Security posture

Summaries are generated from untrusted agent transcripts:

- Codex runs read-only, ephemeral, and without user configuration.
- The standalone ACP example disables tools, inherited settings, and MCP
  servers.
- Every summary is sanitized before it reaches a custom speech command.
- Configured command secrets and API keys are redacted from errors, logs, and
  status output.

Keep all four properties when changing the pipeline.
