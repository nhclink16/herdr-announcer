# VHS tape for the setup wizard demo: vhs assets/demo.tape (run from repo root)
Output assets/demo.gif
Set FontSize 16
Set Width 860
Set Height 620
Set Padding 12
Set TypingSpeed 55ms

Hide
Type "export HERDR_PLUGIN_CONFIG_DIR=$(mktemp -d) HERDR_PLUGIN_STATE_DIR=$(mktemp -d); clear"
Enter
Show

Type "python3 announce.py setup"
Enter
Sleep 1.5s

# states multiselect: browse, toggle idle on and off, keep defaults
Down
Sleep 500ms
Down
Sleep 400ms
Space
Sleep 700ms
Space
Sleep 500ms
Enter
Sleep 900ms

# summarizer: Codex
Enter
Sleep 700ms
# model: keep
Enter
Sleep 600ms
# effort: browse then low
Down
Sleep 500ms
Up
Sleep 400ms
Enter
Sleep 800ms

# style: announcer voice
Enter
Sleep 800ms

# voice: this machine
Enter
Sleep 600ms
# voice name: system default
Enter
Sleep 700ms

# toast: No (default)
Enter
Sleep 700ms

# debounce: keep 30
Enter
Sleep 1s

# write it: Yes
Enter
Sleep 800ms

# voice test: No (silent demo)
Down
Sleep 400ms
Enter
Sleep 3s
