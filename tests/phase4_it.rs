use herdr_announcer::actions::{ActionOptions, process_action_with_options};
use herdr_announcer::debounce::{load_debounce_state, reserve_debounce};
use herdr_announcer::hook::LogContext;
use herdr_announcer::ipc::Client;
use herdr_announcer::log::read_log;
use herdr_announcer::mute::{list_pane_mutes, set_pane_mute};
use herdr_announcer::snooze::write_snooze;
use herdr_announcer::speech::SpeechOptions;
use rustix::fs::{FlockOperation, flock};
use serde_json::{Value, json};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

const NOW: f64 = 1_787_110_000.0;

struct FakeRpc {
    _temp: tempfile::TempDir,
    socket: PathBuf,
    requests: Arc<Mutex<Vec<Value>>>,
    thread: thread::JoinHandle<()>,
}

impl FakeRpc {
    fn start(responses: Vec<Value>) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let socket = temp.path().join("herdr.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&requests);
        let thread = thread::spawn(move || {
            for mut response in responses {
                let (stream, _) = listener.accept().unwrap();
                let mut reader = BufReader::new(stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                response["id"] = request["id"].clone();
                captured.lock().unwrap().push(request);
                let mut stream = reader.into_inner();
                serde_json::to_writer(&mut stream, &response).unwrap();
                stream.write_all(b"\n").unwrap();
            }
        });
        Self {
            _temp: temp,
            socket,
            requests,
            thread,
        }
    }

    fn finish(self) -> Vec<Value> {
        self.thread.join().unwrap();
        Arc::try_unwrap(self.requests)
            .unwrap()
            .into_inner()
            .unwrap()
    }
}

fn config(config_dir: &Path, state_dir: &Path, extra: &str) -> PathBuf {
    fs::create_dir_all(config_dir).unwrap();
    fs::create_dir_all(state_dir).unwrap();
    let spoken = state_dir.join("spoken.txt");
    fs::write(
        config_dir.join("config.toml"),
        format!(
            "summary = \"template\"\ndebounce_seconds = 30\ntoast = false\nspeak_command = [\"/bin/sh\", \"-c\", \"cat >> {}\"]\n{extra}",
            spoken.display()
        ),
    )
    .unwrap();
    spoken
}

fn event(pane: &str, status: &str, agent: &str) -> String {
    json!({
        "event":"pane_agent_status_changed",
        "data":{
            "type":"pane_agent_status_changed",
            "pane_id":pane,
            "workspace_id":"w-test",
            "agent_status":status,
            "agent":agent,
            "display_agent":agent.to_uppercase()
        }
    })
    .to_string()
}

fn run_hook(config: &Path, state: &Path, raw: &str) {
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .env("HERDR_PLUGIN_CONFIG_DIR", config)
        .env("HERDR_PLUGIN_STATE_DIR", state)
        .env("HERDR_PLUGIN_EVENT_JSON", raw)
        .env_remove("HERDR_SOCKET_PATH")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn status_filter_then_agent_mute_then_pane_mute_order_is_logged() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    config(&config_dir, &state, "mute_agents = [\"CoDeX\"]\n");
    set_pane_mute(&state, "p-muted", 0.0, "codex", NOW).unwrap();

    run_hook(&config_dir, &state, &event("p-muted", "working", "codex"));
    run_hook(&config_dir, &state, &event("p-muted", "done", "codex"));
    fs::write(
        config_dir.join("config.toml"),
        format!(
            "summary = \"template\"\ndebounce_seconds = 30\nspeak_command = [\"/bin/sh\", \"-c\", \"cat >> {}\"]\nmute_agents = []\n",
            state.join("spoken.txt").display()
        ),
    )
    .unwrap();
    run_hook(&config_dir, &state, &event("p-muted", "done", "claude"));

    let entries = read_log(&state.join("announcer.log"), 8);
    let actions: Vec<_> = entries.iter().map(|entry| entry.action.as_str()).collect();
    assert_eq!(actions, ["skipped-status", "muted-agent", "muted-pane"]);
    assert!(!state.join("spoken.txt").exists());
}

fn fixture_event(name: &str) -> String {
    let fixture: Value = serde_json::from_str(match name {
        "initial" => include_str!("fixtures/hook-pane-agent-detected.json"),
        "release" => include_str!("fixtures/hook-pane-agent-detected-released.json"),
        _ => unreachable!(),
    })
    .unwrap();
    serde_json::to_string(&fixture["event_json"]).unwrap()
}

#[test]
fn detected_initial_announces_and_debounces_but_release_is_silent() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    let spoken = config(&config_dir, &state, "announce_on_detect = true\n");
    let workspace = json!({
        "id":"x",
        "result":{"type":"workspace_list","workspaces":[{
            "workspace_id":"w7","label":"contract-probe-phase0"
        }]}
    });
    let fake = FakeRpc::start(vec![workspace]);
    let first = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .env("HERDR_PLUGIN_CONFIG_DIR", &config_dir)
        .env("HERDR_PLUGIN_STATE_DIR", &state)
        .env("HERDR_PLUGIN_EVENT_JSON", fixture_event("initial"))
        .env("HERDR_SOCKET_PATH", &fake.socket)
        .output()
        .unwrap();
    assert!(first.status.success());
    assert_eq!(fake.finish()[0]["method"], "workspace.list");
    assert_eq!(
        fs::read_to_string(&spoken).unwrap(),
        "contract-probe agent detected in contract-probe-phase0."
    );

    run_hook(&config_dir, &state, &fixture_event("initial"));
    run_hook(&config_dir, &state, &fixture_event("release"));
    let entries = read_log(&state.join("announcer.log"), 8);
    assert_eq!(entries.len(), 2, "release must create no log line");
    assert_eq!(entries[0].status, "detected");
    assert_eq!(
        entries[0].action,
        "announced+summary-template+speak-command"
    );
    assert_eq!(entries[1].status, "detected");
    assert_eq!(entries[1].action, "debounced");
    assert_eq!(
        load_debounce_state(&state.join("last.json"))["w7:p1"].status,
        "detected"
    );
}

#[test]
fn missing_context_falls_back_to_pane_current_with_caller_id() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    config(&config_dir, &state, "");
    let current = json!({"id":"x","result":{"type":"pane_current","pane":{
        "pane_id":"w7:p1","workspace_id":"w7","agent_status":"unknown"
    }}});
    let ok = json!({"id":"x","result":{"type":"ok"}});
    let shown = json!({"id":"x","result":{"type":"notification_show","shown":true}});
    let fake = FakeRpc::start(vec![current, ok, shown]);
    let options = ActionOptions {
        client: Some(Client::new(&fake.socket)),
        caller_pane_id: Some("caller:p9".to_owned()),
        speech: SpeechOptions::default(),
        now: NOW,
    };
    let mut context = LogContext::hook();
    let action = process_action_with_options(
        &config_dir,
        &state,
        "mute-pane",
        Some("{}"),
        &mut context,
        &mut Vec::new(),
        &options,
    )
    .unwrap();
    assert_eq!(action, "mute-pane");
    assert_eq!(context.pane_id, "w7:p1");
    let requests = fake.finish();
    assert_eq!(
        requests
            .iter()
            .map(|value| value["method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["pane.current", "pane.report_metadata", "notification.show"]
    );
    assert_eq!(requests[0]["params"], json!({"caller_pane_id":"caller:p9"}));
    assert_eq!(requests[1]["params"]["tokens"], json!({"muted":"1"}));
    assert_eq!(
        requests[2]["params"]["title"],
        "Announcer: pane muted until it closes"
    );
}

#[test]
fn pane_snooze_action_cycles_state_metadata_and_toasts() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    config(&config_dir, &state, "");
    let mut responses = Vec::new();
    for _ in 0..4 {
        responses.push(json!({"id":"x","result":{"type":"ok"}}));
        responses.push(json!({
            "id":"x","result":{"type":"notification_show","shown":true}
        }));
    }
    let fake = FakeRpc::start(responses);
    let options = ActionOptions {
        client: Some(Client::new(&fake.socket)),
        caller_pane_id: None,
        speech: SpeechOptions::default(),
        now: NOW,
    };
    for expected in [Some(300.0), Some(1800.0), Some(7200.0), None] {
        assert_eq!(
            process_action_with_options(
                &config_dir,
                &state,
                "snooze-pane",
                Some(&action_context()),
                &mut LogContext::hook(),
                &mut Vec::new(),
                &options,
            )
            .unwrap(),
            "snooze-pane"
        );
        let entry = list_pane_mutes(&state, NOW).get("w-action:p1").cloned();
        assert_eq!(entry.map(|entry| entry.until - NOW), expected);
    }
    let requests = fake.finish();
    let titles: Vec<_> = requests
        .iter()
        .filter(|request| request["method"] == "notification.show")
        .map(|request| request["params"]["title"].as_str().unwrap())
        .collect();
    assert_eq!(
        titles,
        [
            "Announcer: pane snoozed · 5m 00s left",
            "Announcer: pane snoozed · 30m 00s left",
            "Announcer: pane snoozed · 2h 00m left",
            "Announcer: pane snooze off",
        ]
    );
    assert_eq!(requests[6]["params"]["tokens"], json!({"muted":null}));
}

fn action_context() -> String {
    json!({
        "focused_pane_id":"w-action:p1",
        "focused_pane_agent":"codex",
        "focused_pane_status":"done",
        "workspace_id":"w-action",
        "workspace_label":"manual-workspace"
    })
    .to_string()
}

fn read_response(text: &str) -> Value {
    json!({"id":"x","result":{"type":"pane_read","read":{"text":text}}})
}

#[test]
fn announce_now_bypasses_every_gate_and_still_takes_playback_lock() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    let spoken = config(&config_dir, &state, "mute_agents = [\"codex\"]\n");
    set_pane_mute(&state, "w-action:p1", 0.0, "codex", NOW).unwrap();
    write_snooze(&state, NOW + 3600.0).unwrap();
    reserve_debounce(&state, "w-action:p1", "done", 3600).unwrap();
    let before = load_debounce_state(&state.join("last.json"));
    let fake = FakeRpc::start(vec![read_response("tests passed")]);
    let options = ActionOptions {
        client: Some(Client::new(&fake.socket)),
        caller_pane_id: None,
        speech: SpeechOptions::default(),
        now: NOW,
    };
    let mut reasons = Vec::new();
    let action = process_action_with_options(
        &config_dir,
        &state,
        "announce-now",
        Some(&action_context()),
        &mut LogContext::hook(),
        &mut reasons,
        &options,
    )
    .unwrap();
    assert_eq!(action, "announced+summary-template+speak-command");
    assert_eq!(reasons, ["manual"]);
    assert_eq!(
        fs::read_to_string(&spoken).unwrap(),
        "codex finished in manual-workspace."
    );
    assert_eq!(fake.finish()[0]["method"], "pane.read");
    assert_eq!(load_debounce_state(&state.join("last.json")), before);
    assert!(list_pane_mutes(&state, NOW).contains_key("w-action:p1"));

    let held = OpenOptions::new()
        .create(true)
        .append(true)
        .open(state.join("speak.lock"))
        .unwrap();
    flock(&held, FlockOperation::LockExclusive).unwrap();
    let fake = FakeRpc::start(vec![read_response("still done")]);
    let mut reasons = Vec::new();
    let locked = process_action_with_options(
        &config_dir,
        &state,
        "announce-now",
        Some(&action_context()),
        &mut LogContext::hook(),
        &mut reasons,
        &ActionOptions {
            client: Some(Client::new(&fake.socket)),
            caller_pane_id: None,
            speech: SpeechOptions {
                playback_timeout: Duration::from_millis(30),
                playback_poll_interval: Duration::from_millis(5),
            },
            now: NOW,
        },
    )
    .unwrap();
    assert_eq!(locked, "gave-up-waiting");
    assert_eq!(reasons, ["manual", "playback-lock: timeout"]);
    flock(&held, FlockOperation::Unlock).unwrap();
    fake.finish();
}

#[test]
fn cleanup_clears_token_and_logs_only_when_an_entry_was_removed() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    config(&config_dir, &state, "");
    set_pane_mute(&state, "w7:p2", 0.0, "codex", NOW).unwrap();
    let ok = json!({"id":"x","result":{"type":"ok"}});
    let fake = FakeRpc::start(vec![ok]);
    let raw: Value = serde_json::from_str(include_str!("fixtures/hook-pane-closed.json")).unwrap();
    let event = serde_json::to_string(&raw["event_json"]).unwrap();
    let binary = env!("CARGO_BIN_EXE_herdr-announcer");
    for _ in 0..2 {
        let output = Command::new(binary)
            .arg("cleanup")
            .env("HERDR_PLUGIN_CONFIG_DIR", &config_dir)
            .env("HERDR_PLUGIN_STATE_DIR", &state)
            .env("HERDR_PLUGIN_EVENT_JSON", &event)
            .env("HERDR_SOCKET_PATH", &fake.socket)
            .output()
            .unwrap();
        assert!(output.status.success());
    }
    let requests = fake.finish();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["method"], "pane.report_metadata");
    assert_eq!(requests[0]["params"]["tokens"], json!({"muted":null}));
    assert!(list_pane_mutes(&state, NOW).is_empty());
    let entries = read_log(&state.join("announcer.log"), 8);
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].pane_id, "w7:p2");
    assert_eq!(entries[0].action, "cleanup");
}

#[test]
fn workspace_close_cleanup_prunes_every_pane_of_that_workspace_only() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state = temp.path().join("state");
    config(&config_dir, &state, "");
    set_pane_mute(&state, "w7:p2", 0.0, "codex", NOW).unwrap();
    set_pane_mute(&state, "w7:p9", NOW + 3600.0, "claude", NOW).unwrap();
    set_pane_mute(&state, "w8:p1", 0.0, "codex", NOW).unwrap();
    let event =
        r#"{"event":"workspace_closed","data":{"type":"workspace_closed","workspace_id":"w7"}}"#;
    let binary = env!("CARGO_BIN_EXE_herdr-announcer");
    for _ in 0..2 {
        let output = Command::new(binary)
            .arg("cleanup")
            .env("HERDR_PLUGIN_CONFIG_DIR", &config_dir)
            .env("HERDR_PLUGIN_STATE_DIR", &state)
            .env("HERDR_PLUGIN_EVENT_JSON", event)
            .env_remove("HERDR_SOCKET_PATH")
            .output()
            .unwrap();
        assert!(output.status.success());
    }
    let remaining = list_pane_mutes(&state, NOW);
    assert_eq!(remaining.keys().collect::<Vec<_>>(), ["w8:p1"]);
    let entries = read_log(&state.join("announcer.log"), 8);
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].pane_id, "w7:p2,w7:p9");
    assert_eq!(entries[0].action, "cleanup");
}

#[test]
fn default_new_keys_preserve_phase_three_log_shape() {
    let temp = tempfile::tempdir().unwrap();
    let baseline_config = temp.path().join("baseline-config");
    let baseline_state = temp.path().join("baseline-state");
    let phase_four_config = temp.path().join("phase-four-config");
    let phase_four_state = temp.path().join("phase-four-state");
    config(&baseline_config, &baseline_state, "");
    config(
        &phase_four_config,
        &phase_four_state,
        "mute_agents = []\nannounce_on_detect = false\n",
    );
    let synthetic = event("p-default", "done", "codex");
    run_hook(&baseline_config, &baseline_state, &synthetic);
    run_hook(&phase_four_config, &phase_four_state, &synthetic);

    let shape = |state: &Path| {
        let entry = read_log(&state.join("announcer.log"), 1).pop().unwrap();
        format!(
            "pane_id={} status={} action={} reasons={}",
            entry.pane_id,
            entry.status,
            entry.action,
            entry.reasons.join(";")
        )
    };
    let baseline = shape(&baseline_state);
    let phase_four = shape(&phase_four_state);
    assert_eq!(phase_four, baseline, "default-key log shape diff");
    assert_eq!(
        phase_four,
        "pane_id=p-default status=done action=announced+summary-template+speak-command reasons=herdr: no socket path"
    );
}
