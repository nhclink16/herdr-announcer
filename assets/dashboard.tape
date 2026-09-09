# VHS tape for the dashboard demo: vhs assets/dashboard.tape (run from repo root)
# Seeds a realistic config and log, then drives the real TUI: navigate,
# cycle snooze, quit. The seeded log lines are the exact format announce.py
# writes; the no-op speak_command keeps the recording silent.
Output assets/dashboard.gif
Set FontSize 16
Set Width 860
Set Height 620
Set Padding 12
Set TypingSpeed 55ms

Hide
Type `export HERDR_PLUGIN_CONFIG_DIR=$(mktemp -d) HERDR_PLUGIN_STATE_DIR=$(mktemp -d)`
Enter
Type `printf 'announce = ["done", "blocked"]\ntoast = true\nspeak_command = ["sh", "-c", "cat >/dev/null"]\n' > $HERDR_PLUGIN_CONFIG_DIR/config.toml`
Enter
Type `printf '2026-08-17T21:20:11-04:00 pane_id=w1:p2 status=done action=announced+summary-codex+speak-command elapsed=1.914\n' > $HERDR_PLUGIN_STATE_DIR/announcer.log`
Enter
Type `printf '2026-08-17T21:24:03-04:00 pane_id=w1:p3 status=blocked action=announced+summary-template+speak-espeak-ng elapsed=0.912 reasons=codex: timeout-first-activity\n' >> $HERDR_PLUGIN_STATE_DIR/announcer.log`
Enter
Type `clear`
Enter
Show

Type `target/release/herdr-announcer dashboard`
Enter
Sleep 2s

# browse the toggles
Type `j`
Sleep 600ms
Type `j`
Sleep 600ms
Type `j`
Sleep 800ms

# toggle idle on, then off again
Type ` `
Sleep 900ms
Type ` `
Sleep 700ms

# hop up to the snooze row and cycle: 5m -> 30m -> 2h
Type `k`
Sleep 400ms
Type `k`
Sleep 400ms
Type `k`
Sleep 500ms
Type `s`
Sleep 900ms
Type `s`
Sleep 900ms
Type `s`
Sleep 1.2s

# and back off
Type `s`
Sleep 600ms
Type `s`
Sleep 1s

Type `q`
Sleep 1.5s
