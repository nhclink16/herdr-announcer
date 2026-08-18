# VHS tape for the status/diagnostics demo: vhs assets/status.tape (run from repo root)
# Shows the failure-visibility story: every fallback records WHY, and status
# surfaces the reasons and the last error. The seeded log lines below are the
# exact format announce.py writes.
Output assets/status.gif
Set FontSize 16
Set Width 860
Set Height 620
Set Padding 12
Set TypingSpeed 55ms

Hide
Type `export HERDR_PLUGIN_CONFIG_DIR=$(mktemp -d) HERDR_PLUGIN_STATE_DIR=$(mktemp -d)`
Enter
Type `printf 'summary = "template"\nspeak_command = ["sh", "-c", "cat >/dev/null"]\n' > $HERDR_PLUGIN_CONFIG_DIR/config.toml`
Enter
Type `printf '2026-08-17T21:04:11-04:00 pane_id=w1:p2 status=done action=announced+summary-codex+speak-elevenlabs elapsed=2.140\n' > $HERDR_PLUGIN_STATE_DIR/announcer.log`
Enter
Type `printf '2026-08-17T21:12:03-04:00 pane_id=w1:p2 status=blocked action=announced+summary-template+speak-espeak-ng elapsed=1.020 reasons=codex: timeout-first-activity;elevenlabs: no-player\n' >> $HERDR_PLUGIN_STATE_DIR/announcer.log`
Enter
Type `printf '{"reasons":["codex: timeout-first-activity","elevenlabs: no-player"],"timestamp":"2026-08-17T21:12:03-04:00"}\n' > $HERDR_PLUGIN_STATE_DIR/last-error.json`
Enter
Type `clear`
Enter
Show

Type `python3 announce.py --test`
Enter
Sleep 1.5s

Type `python3 announce.py status`
Enter
Sleep 5s
