use herdr_announcer::config::load_config;
use herdr_announcer::speech::speak;
use herdr_announcer::summarize::codex_summary;
use std::ffi::OsString;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;
use std::sync::{Mutex, MutexGuard};

static ENVIRONMENT: Mutex<()> = Mutex::new(());

struct EnvironmentGuard {
    _lock: MutexGuard<'static, ()>,
    values: Vec<(&'static str, Option<OsString>)>,
}

impl EnvironmentGuard {
    fn set(values: &[(&'static str, OsString)]) -> Self {
        let lock = ENVIRONMENT
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let original = values
            .iter()
            .map(|(key, _)| (*key, std::env::var_os(key)))
            .collect();
        for (key, value) in values {
            // SAFETY: all environment-mutating Phase 3 integration tests share
            // this process-wide mutex and restore their values on drop.
            unsafe { std::env::set_var(key, value) };
        }
        Self {
            _lock: lock,
            values: original,
        }
    }
}

impl Drop for EnvironmentGuard {
    fn drop(&mut self) {
        for (key, value) in &self.values {
            // SAFETY: guarded by the same process-wide mutex as construction.
            unsafe {
                if let Some(value) = value {
                    std::env::set_var(key, value);
                } else {
                    std::env::remove_var(key);
                }
            }
        }
    }
}

fn executable(path: &Path, contents: &str) {
    fs::write(path, contents).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

fn load_test_config(root: &Path, contents: &str) -> herdr_announcer::config::Config {
    fs::create_dir_all(root).unwrap();
    fs::write(root.join("config.toml"), contents).unwrap();
    load_config(root, &mut Vec::new(), None).unwrap()
}

#[test]
fn codex_ndjson_collector_handles_success_failures_deadlines_and_stderr() {
    let temp = tempfile::tempdir().unwrap();
    let codex = temp.path().join("codex");
    executable(
        &codex,
        r#"#!/bin/sh
[ -n "$STUB_MARKER" ] && : > "$STUB_MARKER"
case "$STUB_MODE" in
  happy)
    echo '{"type":"item.started","item":{"type":"reasoning"}}'
    echo '{"type":"item.completed","item":{"type":"agent_message","text":"Builder finished the migration cleanly."}}'
    echo '{"type":"turn.completed"}'
    ;;
  failed)
    echo first >&2
    echo 'last useful error' >&2
    echo '{"type":"turn.failed"}'
    ;;
  noactivity)
    sleep 3
    ;;
  slow)
    echo '{"type":"item.started","item":{"type":"reasoning"}}'
    sleep 3
    ;;
esac
"#,
    );
    let old_path = std::env::var_os("PATH").unwrap_or_default();
    let path = std::env::join_paths(
        std::iter::once(temp.path().to_path_buf()).chain(std::env::split_paths(&old_path)),
    )
    .unwrap();
    let config_dir = temp.path().join("config");
    let config = load_test_config(
        &config_dir,
        // Generous margins: the assertions are about ordering (first activity
        // arms the completion window), not speed, and a loaded machine can take
        // hundreds of milliseconds to spawn the stub.
        "summary_first_activity_timeout_seconds = 1.0\ncodex_timeout_seconds = 1.0\n",
    );

    for (mode, expected, reason) in [
        (
            "happy",
            Some("Builder finished the migration cleanly."),
            None,
        ),
        ("failed", None, Some("codex: turn.failed last useful error")),
        ("noactivity", None, Some("codex: timeout-first-activity")),
        ("slow", None, Some("codex: timeout-completion")),
    ] {
        let _environment = EnvironmentGuard::set(&[
            ("PATH", path.clone()),
            ("STUB_MODE", OsString::from(mode)),
            ("STUB_MARKER", OsString::new()),
        ]);
        let mut reasons = Vec::new();
        let output = codex_summary(
            &config,
            "builder",
            "work",
            "done",
            "terminal output",
            &mut reasons,
        );
        assert_eq!(output.as_deref(), expected, "mode {mode}");
        if let Some(reason) = reason {
            assert_eq!(reasons, [reason], "mode {mode}");
        } else {
            assert!(reasons.is_empty());
        }
    }

    let marker = temp.path().join("spawned");
    let invalid = load_test_config(
        &config_dir,
        "summary_first_activity_timeout_seconds = -1\ncodex_timeout_seconds = 1\n",
    );
    let _environment = EnvironmentGuard::set(&[
        ("PATH", path),
        ("STUB_MODE", OsString::from("happy")),
        ("STUB_MARKER", marker.clone().into_os_string()),
    ]);
    assert!(codex_summary(&invalid, "a", "w", "done", "x", &mut Vec::new()).is_none());
    assert!(!marker.exists(), "invalid timeout must abort before spawn");
}

fn serve_audio(listener: TcpListener, audio: &'static [u8]) -> std::thread::JoinHandle<String> {
    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        loop {
            let count = stream.read(&mut buffer).unwrap();
            request.extend_from_slice(&buffer[..count]);
            let Some(header_end) = request.windows(4).position(|part| part == b"\r\n\r\n") else {
                continue;
            };
            let headers = String::from_utf8_lossy(&request[..header_end + 4]);
            let length = headers
                .lines()
                .find_map(|line| {
                    line.trim_end()
                        .to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|value| value.parse::<usize>().ok())
                })
                .unwrap();
            if request.len() >= header_end + 4 + length {
                break;
            }
        }
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            audio.len()
        )
        .unwrap();
        stream.write_all(audio).unwrap();
        String::from_utf8(request).unwrap()
    })
}

#[test]
fn elevenlabs_mp3_reaches_player_and_no_player_skips_http_entirely() {
    let temp = tempfile::tempdir().unwrap();
    let tools = temp.path().join("tools");
    let config_dir = temp.path().join("config");
    let state_dir = temp.path().join("state");
    fs::create_dir_all(&tools).unwrap();
    fs::create_dir_all(&state_dir).unwrap();
    let capture = temp.path().join("player-args");
    let captured_audio = temp.path().join("played-audio");
    executable(
        &tools.join("mpv"),
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$MPV_CAPTURE\"\n/bin/cp \"$2\" \"$MPV_AUDIO\"\n",
    );
    let config = load_test_config(
        &config_dir,
        "elevenlabs_api_key = \"sk-live-test-4321\"\nelevenlabs_voice_id = \"voice one\"\nelevenlabs_model = \"model-test\"\n",
    );
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = serve_audio(listener, b"FAKE MP3 BODY");
    let _environment = EnvironmentGuard::set(&[
        ("PATH", tools.clone().into_os_string()),
        ("HERDR_ANNOUNCER_ELEVENLABS_BASE", OsString::from(base)),
        ("MPV_CAPTURE", capture.clone().into_os_string()),
        ("MPV_AUDIO", captured_audio.clone().into_os_string()),
    ]);
    let mut reasons = Vec::new();
    assert_eq!(
        speak(&config, "hello there", &state_dir, &mut reasons).unwrap(),
        "elevenlabs"
    );
    assert!(reasons.is_empty());
    assert_eq!(fs::read(&captured_audio).unwrap(), b"FAKE MP3 BODY");
    assert!(
        fs::read_to_string(&capture)
            .unwrap()
            .starts_with("--no-video\n")
    );
    assert!(
        !fs::read_dir(&state_dir)
            .unwrap()
            .flatten()
            .any(|entry| entry.path().extension().is_some_and(|ext| ext == "mp3"))
    );
    let request = server.join().unwrap();
    assert!(request.starts_with("POST /v1/text-to-speech/voice%20one?output_format=mp3_44100_128"));
    assert!(
        request
            .to_ascii_lowercase()
            .contains("xi-api-key: sk-live-test-4321")
    );

    fs::remove_file(tools.join("mpv")).unwrap();
    // Local TTS differs per platform: macOS uses `say`, Linux probes espeak-ng.
    executable(&tools.join("espeak-ng"), "#!/bin/sh\n/bin/cat >/dev/null\n");
    executable(&tools.join("say"), "#!/bin/sh\n/bin/cat >/dev/null\n");
    let local_backend = if cfg!(target_os = "macos") {
        "say"
    } else {
        "espeak-ng"
    };
    let unpaid = TcpListener::bind("127.0.0.1:0").unwrap();
    unpaid.set_nonblocking(true).unwrap();
    let unpaid_base = format!("http://{}", unpaid.local_addr().unwrap());
    drop(_environment);
    let _environment = EnvironmentGuard::set(&[
        ("PATH", tools.into_os_string()),
        (
            "HERDR_ANNOUNCER_ELEVENLABS_BASE",
            OsString::from(unpaid_base),
        ),
    ]);
    reasons.clear();
    assert_eq!(
        speak(&config, "hello", &state_dir, &mut reasons).unwrap(),
        local_backend
    );
    assert!(
        matches!(unpaid.accept(), Err(error) if error.kind() == std::io::ErrorKind::WouldBlock)
    );
    assert_eq!(
        &reasons[..2],
        ["elevenlabs: no-player", "play: mpv/ffplay missing"]
    );
}

#[test]
fn end_to_end_failure_never_leaks_configured_secrets() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    let state_dir = temp.path().join("state");
    fs::create_dir_all(&config_dir).unwrap();
    fs::create_dir_all(&state_dir).unwrap();
    let secret = "sk-end-to-end-secret-7391";
    fs::write(
        config_dir.join("config.toml"),
        format!(
            "elevenlabs_api_key = \"{secret}\"\nspeak_command = [\"/bin/sh\", \"-c\", \"echo $2 >&2; exit 9\", \"_\", \"--api-key\", \"{secret}\"]\n"
        ),
    )
    .unwrap();
    let binary = env!("CARGO_BIN_EXE_herdr-announcer");
    let output = Command::new(binary)
        .arg("--test")
        .env("HERDR_PLUGIN_CONFIG_DIR", &config_dir)
        .env("HERDR_PLUGIN_STATE_DIR", &state_dir)
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    let log = fs::read_to_string(state_dir.join("announcer.log")).unwrap();
    let last_error = fs::read_to_string(state_dir.join("last-error.json")).unwrap();
    let status = Command::new(binary)
        .arg("status")
        .env("HERDR_PLUGIN_CONFIG_DIR", &config_dir)
        .env("HERDR_PLUGIN_STATE_DIR", &state_dir)
        .output()
        .unwrap();
    assert!(status.status.success());
    let status = String::from_utf8(status.stdout).unwrap();
    for (name, text) in [
        ("stderr", stderr),
        ("announcer.log", log),
        ("last-error.json", last_error),
        ("status", status),
    ] {
        assert!(!text.contains(secret), "secret leaked in {name}: {text}");
        assert!(
            text.contains("****7391"),
            "mask missing from {name}: {text}"
        );
    }
}

#[test]
fn elevenlabs_pcm_selection_uses_raw_player_and_pcm_suffix() {
    let temp = tempfile::tempdir().unwrap();
    let tools = temp.path().join("tools");
    let config_dir = temp.path().join("config");
    let state_dir = temp.path().join("state");
    fs::create_dir_all(&tools).unwrap();
    fs::create_dir_all(&state_dir).unwrap();
    let arguments = temp.path().join("paplay-args");
    executable(
        &tools.join("paplay"),
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PAPLAY_ARGS\"\n",
    );
    let config = load_test_config(&config_dir, "elevenlabs_api_key = \"secret\"\n");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = serve_audio(listener, b"PCM");
    let _environment = EnvironmentGuard::set(&[
        ("PATH", tools.into_os_string()),
        ("HERDR_ANNOUNCER_ELEVENLABS_BASE", OsString::from(base)),
        ("PAPLAY_ARGS", arguments.clone().into_os_string()),
    ]);
    assert_eq!(
        speak(&config, "hello", &state_dir, &mut Vec::new()).unwrap(),
        "elevenlabs"
    );
    let request = server.join().unwrap();
    assert!(request.contains("output_format=pcm_22050"));
    let args = fs::read_to_string(arguments).unwrap();
    assert!(args.starts_with("--raw\n--rate=22050\n--channels=1\n--format=s16le\n"));
    assert!(args.lines().last().unwrap().ends_with(".pcm"));
}
