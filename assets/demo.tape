# VHS tape for the setup wizard demo: vhs assets/demo.tape (run from repo root)
Output assets/demo.gif
Set FontSize 16
Set Width 860
Set Height 600
Set Padding 12
Set TypingSpeed 55ms

Hide
Type "export HERDR_PLUGIN_CONFIG_DIR=$(mktemp -d) HERDR_PLUGIN_STATE_DIR=$(mktemp -d); clear"
Enter
Show

Type "herdr plugin pane open --plugin nhclink16.announcer --entrypoint setup"
Sleep 800ms
Hide
Ctrl+U
Type "python3 announce.py setup"
Show
Enter
Sleep 1.2s

# Announce states: keep default done,blocked
Enter
Sleep 900ms

# Summary mode: 1 = codex
Type "1"
Sleep 400ms
Enter
Sleep 700ms

# Codex model: keep gpt-5.6-luna
Enter
Sleep 500ms
# Codex effort: keep low
Enter
Sleep 900ms

# Style: 1 = announcement
Type "1"
Sleep 400ms
Enter
Sleep 900ms

# Voice backend: 1 = local TTS
Type "1"
Sleep 400ms
Enter
Sleep 600ms
# macOS voice name: keep system default
Enter
Sleep 700ms

# Toast: yes
Type "y"
Sleep 300ms
Enter
Sleep 600ms

# Debounce: keep 30
Enter
Sleep 1s

# Write it: yes
Type "y"
Sleep 300ms
Enter
Sleep 700ms

# Voice test: no (silent demo)
Type "n"
Sleep 400ms
Enter
Sleep 3s
