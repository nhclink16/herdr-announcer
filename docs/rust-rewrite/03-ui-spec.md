# UI specification — dashboard and wizard

Two parts: (A) the current Python UI contract you must preserve where stated, and
(B) the new ratatui design — follow it literally; no aesthetic improvisation.
Authoritative Python sources: `announcer/dashboard.py`, `announcer/wizard.py`,
`announcer/tui.py`, tests in `test_dashboard.py`/`test_wizard.py`/`test_tui.py`.

## Theme (single source of truth: `src/tui/theme.rs`)

- Glyphs: `◆ ◇ │ └ ❯ ◼ ◻ ▸ ● ○ ✓ ✗ · …` — plain BMP symbols, NO emoji, no VS16.
- Colors: ACCENT=Cyan, DIM=DarkGray, OK=Green, WARN=Yellow, ERR=Red; focused row
  label gets BOLD (color unchanged).
- Call `crossterm::style::force_color_output(true)` before any drawing.

## A. Preserved contracts

### snapshot() — byte-stable plain text (agents parse it)

`herdr-announcer dashboard` with non-TTY stdout prints the legacy snapshot and
exits 0. Reproduce `announcer/dashboard.py` `render()`+`_fit()`+`snapshot()`
**byte-for-byte** at fixed geometry: WIDTH 86, FRAME_HEIGHT 28, footer = the
legacy FULL string
`j/k or arrows move · space/enter toggle · s snooze · t test · w wizard · r reload · q quit`,
RECENT_LINES 5 newest-first, `└` bottom rail, ANSI-free, trailing blank message
line stripped, exactly one trailing `\n`. Mute info is deliberately NOT in the
snapshot for v1.0. Layout indices, drop-priority (`_fit`), badge precedence,
log-row format `"{ts:<8}  {status:<8}  {pane_id:<12}  {action}[  (r1;r2)]"`, and
all strings are in `dashboard.py:386-555`. Golden file generated from the Python
implementation is the referee. Pad by CHARS (Python `ljust`), not bytes.

### `dashboard` subcommands (exact stdout, exit codes)

- `snooze <5m|30m|2h|tomorrow|off>` → `snooze off` or
  `snoozed until HH:MM (<spec>)`; invalid spec → USAGE to stderr, exit 2.
- `toggle-toast` → `toast on` / `toast off`.
- `open` → `plugin.pane.open` RPC for plugin `nhclink16.announcer` entrypoint
  `dashboard` (params from Phase 0 fixture); error → `dashboard error: {e}`,
  exit 1.
- USAGE string:
  `usage: dashboard.py [snooze 5m|30m|2h|tomorrow|off | toggle-toast | open]` —
  update `dashboard.py` to the new binary name at cutover
  (`usage: herdr-announcer dashboard [snooze ... | toggle-toast | open]`) and pin
  it in a test.

### Transient message strings (verbatim; 5s expiry)

```
config unreadable - showing the last good values
reloaded
announce: done, blocked            (comma list in done/blocked/idle/working/unknown order)
announce: (none) - nothing will speak
toast on / toast off
snoozed · 4m 59s left / snoozed until 08:00 / snooze off
voice test running… / voice test already running / voice test ok
voice test failed: <last stderr line | exit N | spawn error>   (clipped 60)
could not save config: <err>       (clipped 60)
could not save snooze: <err>       (clipped 60)
opening the setup wizard…
```
NEW: `pane {id} unmuted`, `agent {name} muted` / `agent {name} unmuted`.

### Config-write semantics from the dashboard

Every toggle goes through the locked comment-preserving writer
(`config.toml.lock` flock → toml_edit in-place update → `.bak` copy of previous
file → atomic replace → reload). **Failed writes never mutate the in-memory
config** — the UI keeps showing what is on disk and surfaces the failure only in
the message line. `announce` persists in state order (done, blocked, idle,
working, unknown), never click order. Unknown keys and comments survive.

### STATE_HELP strings (verbatim)

```
done     an agent finished work you weren't watching
blocked  an agent is waiting on your input
idle     an agent settled while you were watching
working  an agent started doing something (chatty)
unknown  unrecognized agent activity (chatty)
toast    mirror each announcement as a Herdr notification
```
Snooze hint: `s cycles 5m / 30m / 2h / tomorrow / off`. Badge strings: `live`
(OK), `snoozed · {remaining}` (WARN), `silent · no states selected` (ERR) —
precedence: no states → silent; snoozed; else live.

### voice/tools info lines

Port `voice_backend_label` (`dashboard.py:215-243`) exactly, including the
double-space column separator and redacted custom command (clip 48):
`custom command  (invalid)` / `custom command  <redacted>` / `ElevenLabs  voice
<id>` / `local say  voice <v>` / `local say  system voice` / `local <found
joined by " / ">` / `local  nothing detected!` / `unsupported platform: <os>`.
Tools strip: `{name} ✓|✗` for `codex claude say spd-say espeak-ng espeak`, ✓ OK
✗ DIM, width-budget truncation as `dashboard.py:502-511`.

## B. New ratatui dashboard (`src/tui/dashboard.rs`)

94×30 popup; alternate screen; `EnableMouseCapture`; panic hook restores the
terminal; tick = `event::poll(1s)`. Terminal below 60×16 → render the single
line `announcer dashboard: terminal too small ({cols}x{rows}, need 60x16)` at
(0,0) and still respond to `q`/Esc/Ctrl-C. TTY-but-too-small at startup keeps
Python behavior: stderr
`dashboard: this terminal is smaller than 60x16; showing a static snapshot`,
snapshot to stdout, exit 0.

### Height degradation (REQUIRED — added after live review found top-clipping)

The layout must be **top-anchored** and always fit the viewport height between
16 and full: the header is NEVER clipped. When the full layout does not fit,
drop in this priority order until it does (mirrors the Python `_fit` intent):

1. log entries beyond the newest 1 (the newest entry survives longest)
2. blank-rail spacer rows in the controls block (bottom-most first)
3. the tools row, then the voice row
4. the config-path row
5. the Muted section beyond its heading + 1 row (then entirely)
6. the log region title (leaving just the newest entry), then the last entry

Never dropped: header+badge, snooze row, `Announce on` heading + 5 state rows,
toast row, both action rows, footer, message line (= 13 rows), which fits the
60×16 floor with room to spare. Below 60×16 the existing too-small single-line
behavior applies. Add TestBackend render tests at 60×16, 62×20, and 94×30
asserting the header is the first row and the footer/message are the last rows.

### Vertical layout (top → bottom). Every body row starts with DIM `│ `.

| region | height | content |
|---|---|---|
| header | 1 | `◆ Announcer  <badge>` (◆ ACCENT, Announcer BOLD) |
| config path | 1 | `│  config {path}` all DIM |
| Recent log | flexible, min 6 | see below |
| controls | 12 fixed | see below |
| Muted | 0 if empty, else 1+n (cap 4 + overflow) | see below |
| actions | 2 | Test voice / Full setup rows |
| footer | 1 | key map, DIM |
| message | 1 | transient message, ACCENT |

**Recent log region:** title `│  Recent` BOLD, right-aligned DIM
`({shown}/{total})` when scrolled. Entries **newest at the BOTTOM**, format
`│    HH:MM:SS  {status:<8}  {pane_id:<12}  {action}[  ({r1;r2})]`, clipped with
`…`. Colors: action `error` → ERR, `snoozed` → WARN, else DIM. Source:
`read_log(limit 500)`. Scroll state `offset_from_bottom: usize` clamped;
vertical `Scrollbar` at region right edge only when entries exceed the region.
Empty → `│    no announcements logged yet` DIM.

**Controls (12 rows, exact order):**
```
 1  │                                   (blank rail)
 2  │ ❯ Snooze   {snooze_label}  {hint DIM}
 3  │                                   (blank rail)
 4  │  Announce on                      (BOLD)
 5  │ ❯◼ done     an agent finished work you weren't watching
 6  │ ❯◻ blocked  …
 7  │ ❯◻ idle     …
 8  │ ❯◻ working  …
 9  │ ❯◻ unknown  …
10  │ ❯◻ toast    mirror each announcement as a Herdr notification
11  │  voice    {voice_backend_label}   (DIM, non-focusable)
12  │  tools    codex ✓  claude ✗ …     (non-focusable)
```
`❯` ACCENT shown only on the focused row (space otherwise); `◼` ACCENT checked,
`◻` DIM unchecked; state name padded to 9 chars; focused label BOLD.

**Muted section (render only when `mute_agents` non-empty OR pane mutes exist):**
- `│  Muted` BOLD.
- Agent-type rows (union of `mute_agents` ∪ agent types seen in `pane_list()`
  when a socket is available, sorted):
  `│ ❯◼ {agent:<9}never announce this agent type` — toggling writes
  `mute_agents`.
- Pane rows: `│ ❯✗ {pane_id:<14}{agent:<9}{until}` where until = `until closed`
  or `until HH:MM`; activating unmutes (removes entry + clears badge).
- More than 4 rows total → show 4 + DIM `│    … and {n} more`.

**Action rows:**
```
│ ❯▸ Test voice      speak a sample announcement now
│ ❯▸ Full setup      open the setup wizard (replaces this screen)
```

**Footer degradation** (pick the longest that fits the width):
```
FULL:    j/k or arrows move · space/enter toggle · s snooze · t test · w wizard · r reload · wheel scrolls log · q quit
COMPACT: j/k move · space toggle · s snooze · t test · w wizard · r reload · q quit
MINIMAL: j/k move · space toggle · s snooze · q quit
```

### Focus ring

`Vec<FocusId>` rebuilt each refresh:
`Snooze, State(done..unknown), Toast, AgentMute(name)…, PaneMute(pane_id)…,
TestVoice, FullSetup`. Track focus by FocusId identity; if the focused id
disappears on refresh, fall to the nearest previous index. Info rows
(voice/tools/config/log) are not focusable.

### Keys (`KeyEventKind::Press` only)

| key | effect |
|---|---|
| `j` / Down / Tab | next focus; clears message |
| `k` / Up / BackTab | prev focus; clears message |
| Space / Enter | activate focused row (Snooze→cycle; State/Toast/AgentMute→toggle+save; PaneMute→unmute; TestVoice→spawn; FullSetup→wizard) |
| `s` | cycle global snooze (message = snooze_message) |
| `t` | voice test |
| `w` | wizard |
| `r` | re-measure + refresh, message `reloaded` |
| PgUp / PgDn | log scroll by (region height − 1) |
| `q`, Ctrl-C, Ctrl-D, **Esc** | quit (Esc is safe now — crossterm parses sequences) |

AltGr guard: ignore shortcut handling for key events whose modifiers contain
both CONTROL and ALT (treat as text → no-op here).

### Mouse (exact)

During `view()`, record `Vec<(Rect, Hit)>`,
`Hit ∈ {Focusable(FocusId), FooterKey(char), LogArea}`; each focusable row's
Rect spans the full row width.
- `Down(Left)` on Focusable → focus AND activate (single click toggles/runs).
- `Down(Left)` on a footer hint segment (each ` · `-separated hint its own Rect)
  → run that key's function.
- ScrollUp/ScrollDown anywhere → log scroll ±3.
- Everything else ignored (right-click belongs to herdr).

### Refresh (every tick; never raises)

Reload config (parse failure → keep last-good + message
`config unreadable - showing the last good values`), log, snooze, pane-mutes;
`pane_list()` at most every 5 ticks and only when a socket path exists (no
socket → skip silently); poll the voice-test child; expire the message after 5s.

### Voice test child

Spawn `current_exe()` with `--test`, env `HERDR_PLUGIN_CONFIG_DIR`/
`HERDR_PLUGIN_STATE_DIR` set explicitly, stdin/stdout null, stderr piped; poll
each tick; close pipes on completion (no fd leak). Messages as listed in §A.

### Wizard handoff

`w`/Full setup: leave alternate screen, disable mouse, show cursor, THEN
`std::os::unix::process::CommandExt::exec` `current_exe()` with `setup` and the
two dir env vars. Exec only after full terminal restore.

## C. New ratatui wizard (`src/tui/wizard.rs` + `src/tui/widgets.rs`)

`Viewport::Inline(14)`; finished questions collapse to one line above the
viewport via `Terminal::insert_before`; raw mode held per widget loop. Non-TTY
(stdin OR stdout): line-oriented prompts with strings byte-identical to Python
(`tui.py` non-TTY branches; `test_wizard.py` drives them):
`  1) label` lists + `  choice [N]: `, `<title> (comma list) [done,blocked]: `,
`<title> [Y/n]: `, `Please choose 1, 2, 3.`, `Please enter yes or no.`,
`Choose from: blocked, done, idle, unknown, working.`, getpass-style secret.

### Widget visuals (verbatim from `tui.py`)

```
◆ <Title>                      ◆ ACCENT, title BOLD
│  <hint>                      DIM
│  ● Selected label            ● ACCENT, label BOLD     (select current)
│  ○ other label               DIM                      (select other)
│ ❯◼ Selected label            multiselect current
│  ◻ other label               DIM
│  [default] > typed           text; secret shows • per char
│  <footer hints>              DIM
└                              DIM
```
Footer hints: select `↑↓ move · enter select · q quit · esc/Ctrl-C quit`;
multiselect `↑↓ move · space toggle · enter confirm · q quit · esc/Ctrl-C quit`
(+ hint line `space toggles, enter confirms`); text/secret
`enter confirms · esc/Ctrl-C quit`.
Collapse line: `◇ {title} · {answer}` — ◇/title/` · ` DIM, answer ACCENT.
Answer summaries: selected label; `(updated)` for changed text with
display_default; masked default for secrets; `(blank)` for empty.

Keys: ↑/k, ↓/j/Tab move; digits 1..n jump (select); space toggles
(multiselect — Enter refused while empty); Enter confirms; `q`/Esc/Ctrl-C abort
choice widgets (`q` is literal text in text/secret); Ctrl-D accepts default in
text/secret; backspace edits. Every widget returns `(value, changed_from_default)`.

### Step flow — port exactly from `wizard.py:389-648` (`_setup_wizard`)

Intro lines, then:
1. multiselect `When should it speak?` → `announce` (5 options with exact
   labels; default = current config).
2. select `Who writes the summary sentence?` → `summary` — options assembled
   from detected `codex` binary, existing `summary_command`, detected `claude`,
   always `None - instant fixed phrasing, no LLM` last. Choosing command with no
   existing one writes the documented claude argv.
   2a. codex only: text `Codex model`; select `Codex reasoning effort`
   (low/medium/high with exact descriptions).
3. (skip when template) select `How should it sound?` → `style`
   (announcement/summary/custom); custom → prompt-template text loop with exact
   fallback message.
4. select `Where should the voice come out?` — `keep` option only when
   config.toml existed; `local` (+ macOS voice question), `elevenlabs` (secret
   key masked, voice id, model; empty key message), `custom` (shlex-style parse
   loop with `Invalid command: {e}`).
5. confirm `Also show each announcement as a Herdr notification? (reaches you
   over SSH)` → `toast`.
6. int-validated text loop `Ignore repeats within how many seconds?` →
   `debounce_seconds` (`Please enter a non-negative integer.`).
7. preview `About to write {path}:` + `_config_lines` (only chosen-or-non-default
   keys, DEFAULTS order, API key masked; `(empty file - everything matches the
   defaults)`; `.bak` notice when file existed) → confirm `Write it?` default
   yes; declined → `Nothing written.` exit 0.
8. write via rollback-snapshot writer; confirm `Test the voice now?` default yes
   → `speak()` in-process → `Spoke via: {backend}` / `Voice test failed: {e}`.
9. `Done. Re-run this wizard anytime; the file is safe to hand-edit too.`

Abort (Esc/Ctrl-C/q in choices) anywhere → rollback semantics
(02-core-behavior + `wizard.py:651-751`): restore before-image only if on-disk
state still matches what the wizard wrote; messages
`\nsetup aborted, nothing written` /
`\nsetup aborted; config changed concurrently and was kept`; exit 130.

Note: with toml_edit the writes now genuinely preserve comments — REMOVE the
stale printed line `Note: wizard writes do not preserve comments from
hand-edited files.` and update docs/configuration.md accordingly.

## Known Python pain points (fixed by design — do not reintroduce)

1. Esc couldn't quit (raw reader collapsed all sequences to "esc") — crossterm
   fixes this; Esc quits the dashboard.
2. Frame-walk fragility forced fixed-height frames — ratatui owns the screen;
   keep only the *intent*: never lose the newest log row or the state list;
   degrade the footer before wrapping it.
3. Clipping was ANSI-blind — style after layout, or use ratatui spans.
4. Mouse/scroll events were actively discarded — now first-class.
5. Voice-test child pipes must be closed (fd leak) and stdout nulled (deadlock).
6. Capabilities probed once at construction — refresh them on `r`.
7. The tools/voice rows were info-only remedied only by the full wizard — keep
   them info-only in v1.0 (no scope creep).
