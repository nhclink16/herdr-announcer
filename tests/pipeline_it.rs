use herdr_announcer::debounce::load_debounce_state;
use herdr_announcer::event::event_payload;
use herdr_announcer::hook::{LogContext, PipelineOptions, process_invocation_with_options};
use herdr_announcer::ipc::Client;
use herdr_announcer::log::{parse_log_line, read_log};
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
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn fixture_response(fixture: &str) -> Value {
    serde_json::from_str::<Value>(fixture).unwrap()["response"].clone()
}

fn status_event() -> String {
    let capture: Value = serde_json::from_str(include_str!(
        "fixtures/hook-pane-agent-status-changed-done.json"
    ))
    .unwrap();
    serde_json::to_string(&capture["event_json"]).unwrap()
}

struct FakeHerdr {
    _temp: tempfile::TempDir,
    socket: PathBuf,
    requests: Arc<Mutex<Vec<Value>>>,
    thread: thread::JoinHandle<()>,
}

impl FakeHerdr {
    fn start(expected_connections: usize, transcript: &str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let socket = temp.path().join("herdr.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let thread_requests = Arc::clone(&requests);
        let transcript = transcript.to_owned();
        let thread = thread::spawn(move || {
            for _ in 0..expected_connections {
                let (stream, _) = listener.accept().unwrap();
                let mut reader = BufReader::new(stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                let mut response = match request["method"].as_str().unwrap() {
                    "workspace.list" => {
                        fixture_response(include_str!("fixtures/socket-workspace-list.json"))
                    }
                    "pane.read" => fixture_response(include_str!("fixtures/socket-pane-read.json")),
                    "notification.show" => {
                        fixture_response(include_str!("fixtures/socket-notification-show.json"))
                    }
                    method => panic!("unexpected fake Herdr method {method}"),
                };
                if request["method"] == "pane.read" {
                    response["result"]["read"]["text"] = json!(transcript);
                }
                response["id"] = request["id"].clone();
                thread_requests.lock().unwrap().push(request);
                let mut stream = reader.into_inner();
                serde_json::to_writer(&mut stream, &response).unwrap();
                stream.write_all(b"\n").unwrap();
                // Dropping this stream after one response enforces one request
                // per connection, matching the captured Herdr contract.
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

fn write_config(config_dir: &Path, state_dir: &Path, command: &str, toast: bool) {
    fs::create_dir_all(config_dir).unwrap();
    fs::create_dir_all(state_dir).unwrap();
    fs::write(
        config_dir.join("config.toml"),
        format!(
            "summary = \"template\"\ndebounce_seconds = 30\ntoast = {toast}\nspeak_command = [\"/bin/sh\", \"-c\", \"{command}\"]\n"
        ),
    )
    .unwrap();
}

#[test]
fn full_hook_uses_one_connection_per_rpc_and_logs_exact_action() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    let state = temp.path().join("state");
    let spoken = state.join("spoken.txt");
    write_config(
        &config,
        &state,
        &format!("cat > {}", spoken.display()),
        true,
    );
    let fake = FakeHerdr::start(3, "terminal output");
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .env("HERDR_PLUGIN_CONFIG_DIR", &config)
        .env("HERDR_PLUGIN_STATE_DIR", &state)
        .env("HERDR_PLUGIN_EVENT_JSON", status_event())
        .env("HERDR_SOCKET_PATH", &fake.socket)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stdout.is_empty());
    assert_eq!(
        fs::read_to_string(spoken).unwrap(),
        "contract-probe finished in contract-probe-phase0."
    );

    let requests = fake.finish();
    let methods: Vec<_> = requests
        .iter()
        .map(|request| request["method"].as_str().unwrap())
        .collect();
    assert_eq!(
        methods,
        ["workspace.list", "pane.read", "notification.show"]
    );
    assert_eq!(requests[1]["params"]["source"], "recent_unwrapped");
    assert_eq!(requests[1]["params"]["lines"], 100);
    assert_eq!(
        requests[2]["params"]["title"],
        "contract-probe finished in contract-probe-phase0."
    );
    assert_eq!(requests[2]["params"]["sound"], "none");

    let entries = read_log(&state.join("announcer.log"), 8);
    assert_eq!(entries.len(), 1);
    let entry = &entries[0];
    assert_eq!(entry.pane_id, "w7:p1");
    assert_eq!(entry.status, "done");
    assert_eq!(entry.action, "announced+summary-template+speak-command");
    assert!(entry.reasons.is_empty());
    assert_eq!(parse_log_line(&entry.raw).unwrap(), *entry);
}

#[test]
fn skipped_status_precedes_snooze_and_snooze_precedes_debounce() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    let state = temp.path().join("state");
    write_config(&config, &state, "exit 0", false);
    write_snooze(
        &state,
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
            + 600.0,
    )
    .unwrap();
    let options = PipelineOptions {
        client: None,
        speech: SpeechOptions::default(),
    };
    let working = json!({
        "pane_id": "p-order",
        "agent_status": "working",
        "workspace_id": "w7"
    })
    .to_string();
    let mut reasons = Vec::new();
    assert_eq!(
        process_invocation_with_options(
            &config,
            &state,
            false,
            Some(&working),
            &mut LogContext::hook(),
            &mut reasons,
            &options,
        )
        .unwrap(),
        "skipped-status"
    );
    assert!(reasons.is_empty());

    let done = status_event();
    assert_eq!(
        process_invocation_with_options(
            &config,
            &state,
            false,
            Some(&done),
            &mut LogContext::hook(),
            &mut reasons,
            &options,
        )
        .unwrap(),
        "snoozed"
    );
    assert!(load_debounce_state(&state.join("last.json")).is_empty());
}

#[test]
fn speech_failure_and_held_lock_both_rollback_the_exact_reservation() {
    for held_lock in [false, true] {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("config");
        let state = temp.path().join("state");
        write_config(
            &config,
            &state,
            if held_lock { "exit 0" } else { "exit 7" },
            false,
        );
        let fake = FakeHerdr::start(2, "terminal output");
        let options = PipelineOptions {
            client: Some(Client::new(&fake.socket)),
            speech: SpeechOptions {
                playback_timeout: Duration::from_millis(50),
                playback_poll_interval: Duration::from_millis(10),
            },
        };
        let held = held_lock.then(|| {
            let file = OpenOptions::new()
                .create(true)
                .append(true)
                .open(state.join("speak.lock"))
                .unwrap();
            flock(&file, FlockOperation::LockExclusive).unwrap();
            file
        });
        let mut reasons = Vec::new();
        let result = process_invocation_with_options(
            &config,
            &state,
            false,
            Some(&status_event()),
            &mut LogContext::hook(),
            &mut reasons,
            &options,
        );
        if held_lock {
            assert_eq!(result.unwrap(), "gave-up-waiting");
            assert!(reasons.contains(&"playback-lock: timeout".to_owned()));
        } else {
            assert!(result.unwrap_err().contains("exited with"));
        }
        let debounce = load_debounce_state(&state.join("last.json"));
        assert!(
            !debounce.contains_key("w7:p1"),
            "held_lock={held_lock} debounce={debounce:?} reasons={reasons:?}"
        );
        if let Some(file) = held {
            flock(&file, FlockOperation::Unlock).unwrap();
        }
        assert_eq!(fake.finish().len(), 2);
    }
}

#[test]
fn missing_socket_degrades_with_one_exact_reason() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    let state = temp.path().join("state");
    write_config(&config, &state, "exit 0", false);
    let mut reasons = Vec::new();
    let result = process_invocation_with_options(
        &config,
        &state,
        false,
        Some(&status_event()),
        &mut LogContext::hook(),
        &mut reasons,
        &PipelineOptions {
            client: None,
            speech: SpeechOptions::default(),
        },
    )
    .unwrap();
    assert_eq!(result, "announced+summary-template+speak-command");
    assert_eq!(reasons, ["herdr: no socket path"]);
    assert_eq!(event_payload(&status_event()).unwrap()["pane_id"], "w7:p1");
}
