# Rust 1.0 cutover checklist

Run this only after reviewing the complete Phase 0–7 working tree. The current
linked plugin still executes Python 0.9.1; the first step changes that live
behavior.

## 1. Activate the Rust manifest

- [ ] Stop or finish any announcement/wizard/dashboard test already in flight.
- [ ] **LIVE CHANGE:** replace the linked manifest:

  ```bash
  mv herdr-plugin.toml.v2 herdr-plugin.toml
  ```

  Herdr may observe this immediately. Restoring the old manifest is the rollback
  until the deletion step below.

## 2. Remove migration-only Python code

- [ ] **DESTRUCTIVE:** delete `announce.py`, `dashboard.py`, `announcer/`, every
  root `test_*.py`, and all `__pycache__/` directories. Confirm the diff before
  proceeding; recover deleted files from version control if rollback is needed.
- [ ] Keep `examples/acp-summary.py` and `examples/route-speak.sh`; the ACP
  example is now self-contained.
- [ ] **DESTRUCTIVE:** remove the Python job from `.github/workflows/ci.yml`,
  delete `scripts/parity.sh`, remove the parity section from
  `docs/development.md`, and remove notes saying the status/snapshot goldens are
  generated from the now-deleted Python implementation. Keep the Rust golden
  files and their tests.
- [ ] Remove the cutover-pending line from the CHANGELOG `[Unreleased]` section.

## 3. Verify the cutover tree

- [ ] Run every Rust and packaging gate:

  ```bash
  cargo fmt --check
  cargo build --release
  cargo test
  cargo clippy --release -- -D warnings
  scripts/check-version.sh
  shellcheck examples/*.sh scripts/*.sh
  ```

- [ ] Confirm `scripts/check-version.sh` reports 1.0.0 using the live
  `herdr-plugin.toml` (not a `.v2` file).
- [ ] Confirm the linked manifest is clean and reports no warnings:

  ```bash
  herdr plugin list --plugin nhclink16.announcer --json \
    | jq -e '.result.plugins[0]
      | .version == "1.0.0"
        and .min_herdr_version == "0.8.0"
        and ((.warnings // []) | length == 0)'
  ```

## 4. Smoke the live plugin

- [ ] Trigger a real background agent status change and hear one announcement.
- [ ] Invoke `nhclink16.announcer.snooze-5m`, verify silence, then invoke
  `nhclink16.announcer.snooze-off`.
- [ ] Open the dashboard popup; verify keyboard controls, mouse activation,
  wheel scrolling, and Esc teardown.
- [ ] Run the setup wizard and decline its final write (or make a reviewed
  change); verify collapsed answer rows.
- [ ] On a scratch pane, invoke `mute-pane`, verify the muted behavior, then
  unmute it.
- [ ] Invoke `announce-now` on the scratch pane and hear the manual
  announcement.
- [ ] Enable `toast` temporarily and verify the Herdr notification path.
- [ ] Inspect `herdr plugin log list --plugin nhclink16.announcer` for clean
  exits and `announcer.log` for the expected action strings.

## 5. Refresh recordings

- [ ] Re-record `assets/dashboard.tape`, `assets/demo.tape`, and
  `assets/status.tape` with the Rust commands listed in `assets/TODO.md`.
- [ ] Review the resulting GIFs, then delete `assets/TODO.md`.

## 6. Commit and release

- [ ] Review `git diff`, confirm no migration fixtures or secrets are present,
  and commit the cutover.
- [ ] **PUBLISHES HISTORY:** tag and push the release flow documented in
  `docs/rust-rewrite/01-herdr-contract.md`:

  ```bash
  git tag v1.0.0
  git push origin master
  git push origin v1.0.0
  gh release create v1.0.0 --generate-notes
  ```

- [ ] **EXTERNAL INSTALL CHANGE:** verify the public installation path:

  ```bash
  herdr plugin install nhclink16/herdr-announcer --yes
  ```

- [ ] Confirm the GitHub release is marked latest and the README release badge
  resolves to v1.0.0.
