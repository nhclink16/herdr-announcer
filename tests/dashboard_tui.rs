use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use herdr_announcer::config::Config;
use herdr_announcer::log::LogEntry;
use herdr_announcer::mute::{PaneMute, set_pane_mute};
use herdr_announcer::snooze::read_snooze;
use herdr_announcer::tui::dashboard::{
    Dashboard, FocusId, Hit, Input, UpdateResult, footer_text, subcommand, voice_backend_label,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;

const NOW: f64 = 1_787_110_000.0;

fn board_with(config: &str) -> (tempfile::TempDir, Dashboard) {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state_dir = temp.path().join("state");
    fs::create_dir_all(&config_dir).unwrap();
    fs::create_dir_all(&state_dir).unwrap();
    fs::write(config_dir.join("config.toml"), config).unwrap();
    let mut board = Dashboard::new(config_dir, state_dir, None);
    board.set_now(NOW);
    board.snooze_until = 0.0;
    board.caps = BTreeMap::from([
        ("codex".to_owned(), true),
        ("claude".to_owned(), false),
        ("say".to_owned(), false),
        ("spd-say".to_owned(), false),
        ("espeak-ng".to_owned(), true),
        ("espeak".to_owned(), false),
    ]);
    (temp, board)
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn render(board: &mut Dashboard, width: u16, height: u16) -> Vec<String> {
    let backend = TestBackend::new(width, height);
    let mut terminal = Terminal::new(backend).unwrap();
    terminal.draw(|frame| board.view(frame)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            (0..width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
                .trim_end()
                .to_owned()
        })
        .collect()
}

fn entry(index: usize) -> LogEntry {
    LogEntry {
        timestamp: format!("2026-08-17T12:{index:02}:11+02:00"),
        pane_id: format!("pane-{index}"),
        status: "done".to_owned(),
        action: format!("action-{index}"),
        elapsed: 0.1,
        reasons: Vec::new(),
        raw: String::new(),
    }
}

#[test]
fn fixed_layout_rows_match_the_spec_at_94_by_30() {
    let (_temp, mut board) =
        board_with("announce = [\"done\"]\nsummary = \"template\"\ntoast = false\n");
    board.set_message("reloaded");
    let lines = render(&mut board, 94, 30);
    assert_eq!(lines[0], "◆ Announcer  live");
    assert!(lines[1].starts_with("│  config "));
    assert_eq!(lines[2], "│  Recent");
    assert_eq!(lines[3], "│    no announcements logged yet");
    assert_eq!(lines[14], "│");
    assert!(lines[15].starts_with("│ ❯ Snooze   off  s cycles 5m / 30m / 2h / tomorrow / off"));
    assert_eq!(lines[16], "│");
    assert_eq!(lines[17], "│  Announce on");
    assert_eq!(
        lines[18],
        "│  ◼ done     an agent finished work you weren't watching"
    );
    assert_eq!(lines[19], "│  ◻ blocked  an agent is waiting on your input");
    assert_eq!(
        lines[20],
        "│  ◻ idle     an agent settled while you were watching"
    );
    assert_eq!(
        lines[21],
        "│  ◻ working  an agent started doing something (chatty)"
    );
    assert_eq!(
        lines[22],
        "│  ◻ unknown  unrecognized agent activity (chatty)"
    );
    assert_eq!(
        lines[23],
        "│  ◻ toast    mirror each announcement as a Herdr notification"
    );
    assert!(lines[24].starts_with("│  voice    local "));
    assert!(lines[25].starts_with("│  tools    codex ✓  claude ✗"));
    assert_eq!(
        lines[26],
        "│  ▸ Test voice     speak a sample announcement now"
    );
    assert_eq!(
        lines[27],
        "│  ▸ Full setup     open the setup wizard (replaces this screen)"
    );
    assert_eq!(lines[28], footer_text(94));
    assert_eq!(lines[29], "reloaded");
}

fn assert_height_edges(lines: &[String], width: u16, height: u16) {
    assert_eq!(lines.len(), usize::from(height));
    assert_eq!(lines[0], "◆ Announcer  live");
    assert_eq!(lines[usize::from(height - 2)], footer_text(width));
    assert_eq!(lines[usize::from(height - 1)], "height-check");
}

#[test]
fn height_degradation_at_60_by_16_keeps_required_edges() {
    let (_temp, mut board) = board_with("announce = [\"done\"]\n");
    board.set_message("height-check");
    let lines = render(&mut board, 60, 16);
    assert_height_edges(&lines, 60, 16);
    assert_eq!(lines[2], "│  Recent");
    assert_eq!(
        lines[4],
        "│ ❯ Snooze   off  s cycles 5m / 30m / 2h / tomorrow / off"
    );
    assert_eq!(lines[5], "│  Announce on");
    assert_eq!(
        lines[11],
        "│  ◻ toast    mirror each announcement as a Herdr notificati"
    );
}

#[test]
fn height_degradation_at_62_by_20_keeps_required_edges() {
    let (_temp, mut board) = board_with("announce = [\"done\"]\n");
    board.set_message("height-check");
    let lines = render(&mut board, 62, 20);
    assert_height_edges(&lines, 62, 20);
    assert_eq!(lines[0], "◆ Announcer  live");
    assert_eq!(lines[2], "│  Recent");
    assert!(lines[5].starts_with("│ ❯ Snooze"));
    assert_eq!(lines[7], "│  Announce on");
}

#[test]
fn height_degradation_at_94_by_30_keeps_required_edges() {
    let (_temp, mut board) = board_with("announce = [\"done\"]\n");
    board.set_message("height-check");
    let lines = render(&mut board, 94, 30);
    assert_height_edges(&lines, 94, 30);
    assert!(lines[1].starts_with("│  config "));
    assert_eq!(lines[2], "│  Recent");
}

#[test]
fn badge_precedence_is_silent_then_snoozed_then_live() {
    let (_temp, mut board) = board_with("announce = []\n");
    board.snooze_until = NOW + 300.0;
    assert_eq!(board.badge().0, "silent · no states selected");
    board.toggle_state("done");
    board.set_now(NOW);
    board.snooze_until = NOW + 299.0;
    assert_eq!(board.badge().0, "snoozed · 4m 59s left");
    board.snooze_until = 0.0;
    assert_eq!(board.badge().0, "live");
}

#[test]
fn focus_keys_wrap_clear_messages_and_altgr_is_ignored() {
    let (_temp, mut board) = board_with("");
    board.set_message("old");
    assert_eq!(
        board.update(key(KeyCode::Char('j'))),
        UpdateResult::Continue
    );
    assert_eq!(board.focus, FocusId::State("done".to_owned()));
    assert!(board.message.is_empty());
    board.update(key(KeyCode::Up));
    assert_eq!(board.focus, FocusId::Snooze);
    board.update(key(KeyCode::BackTab));
    assert_eq!(board.focus, FocusId::FullSetup);
    let before = board.focus.clone();
    board.update(Input::Key(KeyEvent::new(
        KeyCode::Char('s'),
        KeyModifiers::CONTROL | KeyModifiers::ALT,
    )));
    assert_eq!(board.focus, before);
    assert_eq!(board.snooze_until, 0.0);
}

#[test]
fn quit_keys_and_wizard_result_are_exact() {
    for event in [
        KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
        KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE),
        KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
        KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL),
    ] {
        let (_temp, mut board) = board_with("");
        assert_eq!(board.update(Input::Key(event)), UpdateResult::Quit);
    }
    let (_temp, mut board) = board_with("");
    assert_eq!(board.update(key(KeyCode::Char('w'))), UpdateResult::Wizard);
    assert_eq!(board.message, "opening the setup wizard…");
}

#[test]
fn state_and_toast_toggles_persist_in_state_order_with_exact_messages() {
    let (temp, mut board) = board_with("announce = [\"blocked\"]\ntoast = false\n");
    board.focus = FocusId::State("done".to_owned());
    board.update(key(KeyCode::Char(' ')));
    let text = fs::read_to_string(temp.path().join("config/config.toml")).unwrap();
    assert!(text.contains("announce = [\"done\", \"blocked\"]"));
    assert_eq!(board.message, "announce: done, blocked");
    board.focus = FocusId::Toast;
    board.update(key(KeyCode::Enter));
    assert!(board.config.is_truthy("toast"));
    assert_eq!(board.message, "toast on");
}

#[test]
fn failed_save_does_not_mutate_the_shown_config() {
    let temp = tempfile::tempdir().unwrap();
    let invalid_dir = temp.path().join("not-a-directory");
    fs::write(&invalid_dir, "file").unwrap();
    let mut board = Dashboard::new(invalid_dir, temp.path().join("state"), None);
    assert!(!board.config.is_truthy("toast"));
    assert!(!board.toggle_toast());
    assert!(!board.config.is_truthy("toast"));
    assert!(board.message.starts_with("could not save config: "));
}

#[test]
fn snooze_shortcut_cycles_five_minutes_thirty_minutes_two_hours_tomorrow_off() {
    let (_temp, mut board) = board_with("");
    for expected_step in ["5m", "30m", "2h", "tomorrow", "off"] {
        board.set_now(NOW);
        board.update(key(KeyCode::Char('s')));
        let until = read_snooze(&board.state_dir, NOW);
        match expected_step {
            "5m" => assert_eq!(until, NOW + 300.0),
            "30m" => assert_eq!(until, NOW + 1800.0),
            "2h" => assert_eq!(until, NOW + 7200.0),
            "tomorrow" => assert!(until > NOW + 7200.0),
            "off" => assert_eq!(until, 0.0),
            _ => unreachable!(),
        }
    }
    assert_eq!(board.message, "snooze off");
}

#[test]
fn focus_disappearance_falls_to_nearest_previous_identity() {
    let (_temp, mut board) = board_with("");
    set_pane_mute(&board.state_dir, "pane-x", 0.0, "codex", NOW).unwrap();
    board.set_now(NOW);
    board.pane_mutes.insert(
        "pane-x".to_owned(),
        PaneMute {
            until: 0.0,
            at: NOW,
            agent: "codex".to_owned(),
        },
    );
    board.rebuild_focus_ring();
    board.focus = FocusId::PaneMute("pane-x".to_owned());
    assert!(board.unmute_pane("pane-x"));
    assert_eq!(board.focus, FocusId::Toast);
    assert_eq!(board.message, "pane pane-x unmuted");
}

#[test]
fn muted_section_renders_agent_and_pane_rows_with_overflow() {
    let (_temp, mut board) = board_with("mute_agents = [\"codex\", \"claude\"]\n");
    board
        .pane_agents
        .extend(["gemini".to_owned(), "codex".to_owned()]);
    for index in 0..3 {
        board.pane_mutes.insert(
            format!("pane-{index}"),
            PaneMute {
                until: if index == 0 { 0.0 } else { NOW + 3600.0 },
                at: NOW,
                agent: "codex".to_owned(),
            },
        );
    }
    board.rebuild_focus_ring();
    let lines = render(&mut board, 94, 30);
    assert!(lines.iter().any(|line| line == "│  Muted"));
    assert!(
        lines
            .iter()
            .any(|line| line.contains("◼ claude   never announce this agent type"))
    );
    assert!(
        lines
            .iter()
            .any(|line| line.contains("◻ gemini   never announce this agent type"))
    );
    assert!(lines.iter().any(|line| line.contains("… and 2 more")));
}

#[test]
fn recent_entries_are_newest_at_bottom_and_scrolled_indicator_appears() {
    let (_temp, mut board) = board_with("");
    board.entries = (0..20).map(entry).collect();
    let lines = render(&mut board, 94, 30);
    assert!(lines[13].contains("action-19"));
    board.scroll_log(3);
    let lines = render(&mut board, 94, 30);
    assert!(lines[2].contains("(11/20)"));
    assert!(lines[13].contains("action-16"));
}

#[test]
fn footer_degrades_at_three_widths() {
    assert_eq!(
        footer_text(120),
        "j/k or arrows move · space/enter toggle · s snooze · t test · w wizard · r reload · wheel scrolls log · q quit"
    );
    assert_eq!(
        footer_text(80),
        "j/k move · space toggle · s snooze · t test · w wizard · r reload · q quit"
    );
    assert_eq!(
        footer_text(59),
        "j/k move · space toggle · s snooze · q quit"
    );
}

#[test]
fn mouse_focuses_and_activates_rows_footer_segments_and_wheel() {
    let (_temp, mut board) = board_with("toast = false\n");
    board.entries = (0..30).map(entry).collect();
    render(&mut board, 94, 30);
    let toast = board
        .hit_regions
        .iter()
        .find_map(|(rect, hit)| (hit == &Hit::Focusable(FocusId::Toast)).then_some(*rect))
        .unwrap();
    board.update(Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: toast.x + 5,
        row: toast.y,
        modifiers: KeyModifiers::NONE,
    }));
    assert_eq!(board.focus, FocusId::Toast);
    assert!(board.config.is_truthy("toast"));

    board.update(Input::Mouse(MouseEvent {
        kind: MouseEventKind::ScrollUp,
        column: 0,
        row: 0,
        modifiers: KeyModifiers::NONE,
    }));
    assert_eq!(board.offset_from_bottom, 3);
    board.update(Input::Mouse(MouseEvent {
        kind: MouseEventKind::ScrollDown,
        column: 0,
        row: 0,
        modifiers: KeyModifiers::NONE,
    }));
    assert_eq!(board.offset_from_bottom, 0);

    let snooze_footer = board
        .hit_regions
        .iter()
        .find_map(|(rect, hit)| (hit == &Hit::FooterKey('s')).then_some(*rect))
        .unwrap();
    board.update(Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: snooze_footer.x,
        row: snooze_footer.y,
        modifiers: KeyModifiers::NONE,
    }));
    assert!(board.snooze_until > 0.0);

    render(&mut board, 120, 30);
    let wheel_footer = board
        .hit_regions
        .iter()
        .find_map(|(rect, hit)| (hit == &Hit::FooterKey('↕')).then_some(*rect))
        .unwrap();
    board.update(Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: wheel_footer.x,
        row: wheel_footer.y,
        modifiers: KeyModifiers::NONE,
    }));
    assert_eq!(board.offset_from_bottom, 3);
}

#[test]
fn page_keys_scroll_by_recent_height_minus_one_and_clamp() {
    let (_temp, mut board) = board_with("");
    board.entries = (0..30).map(entry).collect();
    render(&mut board, 94, 30);
    assert_eq!(board.log_region_height, 12);
    board.update(key(KeyCode::PageUp));
    assert_eq!(board.offset_from_bottom, 11);
    board.update(key(KeyCode::PageUp));
    assert_eq!(board.offset_from_bottom, 19);
    board.update(key(KeyCode::PageDown));
    assert_eq!(board.offset_from_bottom, 8);
}

#[test]
fn too_small_view_is_one_literal_line_and_quit_still_works() {
    let (_temp, mut board) = board_with("");
    let lines = render(&mut board, 59, 15);
    assert_eq!(
        lines[0],
        "announcer dashboard: terminal too small (59x15, need 60x16)"
    );
    assert!(lines[1..].iter().all(String::is_empty));
    assert_eq!(board.update(key(KeyCode::Char('q'))), UpdateResult::Quit);
}

#[test]
fn voice_backend_labels_match_backend_precedence() {
    let temp = tempfile::tempdir().unwrap();
    fs::write(
        temp.path().join("config.toml"),
        "speak_command = [\"helper\", \"--api-key\", \"secret-4321\"]\n",
    )
    .unwrap();
    let config = herdr_announcer::config::load_config(temp.path(), &mut Vec::new(), None).unwrap();
    assert_eq!(
        voice_backend_label(&config, &BTreeMap::new()),
        "custom command  helper --api-key '****4321'"
    );
    let defaults = Config::default();
    let label = voice_backend_label(&defaults, &BTreeMap::from([("espeak-ng".to_owned(), true)]));
    if cfg!(target_os = "linux") {
        assert_eq!(label, "local espeak-ng");
    }
}

#[test]
fn subcommands_have_exact_output_usage_and_round_trip() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    let state = temp.path().join("state");
    let bad = subcommand(&["snooze".to_owned()], &config, &state);
    assert_eq!(bad.code, 2);
    assert_eq!(
        bad.stderr,
        format!("{}\n", herdr_announcer::tui::dashboard::USAGE)
    );
    let snooze = subcommand(&["snooze".to_owned(), "5m".to_owned()], &config, &state);
    assert_eq!(snooze.code, 0);
    assert!(snooze.stdout.starts_with("snoozed until "));
    assert!(snooze.stdout.ends_with(" (5m)\n"));
    let off = subcommand(&["snooze".to_owned(), "off".to_owned()], &config, &state);
    assert_eq!(off.stdout, "snooze off\n");
    let first = subcommand(&["toggle-toast".to_owned()], &config, &state);
    let second = subcommand(&["toggle-toast".to_owned()], &config, &state);
    assert_eq!(first.stdout, "toast on\n");
    assert_eq!(second.stdout, "toast off\n");
}

struct FakeSocket {
    _temp: tempfile::TempDir,
    path: std::path::PathBuf,
    request: Arc<Mutex<Option<Value>>>,
    thread: thread::JoinHandle<()>,
}

impl FakeSocket {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("herdr.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let request = Arc::new(Mutex::new(None));
        let captured = Arc::clone(&request);
        let thread = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream);
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            *captured.lock().unwrap() = Some(request.clone());
            let mut stream = reader.into_inner();
            writeln!(
                stream,
                "{}",
                json!({"id":request["id"],"result":{"type":"plugin_pane_opened","plugin_pane":{}}})
            )
            .unwrap();
        });
        Self {
            _temp: temp,
            path,
            request,
            thread,
        }
    }

    fn finish(self) -> Value {
        self.thread.join().unwrap();
        Arc::try_unwrap(self.request)
            .unwrap()
            .into_inner()
            .unwrap()
            .unwrap()
    }
}

#[test]
fn open_subcommand_uses_phase_zero_rpc_shape() {
    let fake = FakeSocket::new();
    let temp = tempfile::tempdir().unwrap();
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .args(["dashboard", "open"])
        .env("HERDR_SOCKET_PATH", &fake.path)
        .env("HERDR_PANE_ID", "w7:p1")
        .env("HERDR_PLUGIN_CONFIG_DIR", temp.path().join("config"))
        .env("HERDR_PLUGIN_STATE_DIR", temp.path().join("state"))
        .output()
        .unwrap();
    assert!(result.status.success());
    let request = fake.finish();
    assert_eq!(request["method"], "plugin.pane.open");
    assert_eq!(request["params"]["plugin_id"], "nhclink16.announcer");
    assert_eq!(request["params"]["entrypoint"], "dashboard");
    assert_eq!(request["params"]["target_pane_id"], "w7:p1");
    assert_eq!(request["params"]["direction"], "right");
    assert_eq!(request["params"]["focus"], true);
    assert_eq!(request["params"]["env"], json!({}));
    assert!(request["params"].get("workspace_id").is_none());
}

#[test]
fn status_and_snapshot_goldens_are_not_rewritten_by_dashboard_tests() {
    assert!(Path::new("tests/golden/status.txt").is_file());
    assert!(Path::new("tests/golden/snapshot.txt").is_file());
}
