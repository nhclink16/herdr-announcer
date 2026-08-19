use std::io::Write;
use std::process::{Command, Stdio};

fn run(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .args(args)
        .output()
        .unwrap()
}

fn run_setup(
    input: &str,
    config: &std::path::Path,
    state: &std::path::Path,
) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .arg("setup")
        .env("HERDR_PLUGIN_CONFIG_DIR", config)
        .env("HERDR_PLUGIN_STATE_DIR", state)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn setup_abort_has_exit_130_and_never_creates_config() {
    let temp = tempfile::tempdir().unwrap();
    let output = run_setup(
        "q\n",
        &temp.path().join("config"),
        &temp.path().join("state"),
    );
    assert_eq!(output.status.code(), Some(130));
    assert!(String::from_utf8_lossy(&output.stdout).contains("setup aborted, nothing written"));
    assert!(!temp.path().join("config/config.toml").exists());
}

#[test]
fn setup_declined_write_has_exit_zero() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    std::fs::create_dir_all(&config).unwrap();
    std::fs::write(config.join("config.toml"), "summary = \"template\"\n").unwrap();
    let output = run_setup("\n\n\n\n\nn\n", &config, &temp.path().join("state"));
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&output.stdout).ends_with("Nothing written.\n"));
}

#[test]
fn pane_action_without_context_or_socket_fails_without_touching_live_state() {
    let temp = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .args(["action", "mute-pane"])
        .env("HERDR_PLUGIN_CONFIG_DIR", temp.path().join("config"))
        .env("HERDR_PLUGIN_STATE_DIR", temp.path().join("state"))
        .env_remove("HERDR_PLUGIN_CONTEXT_JSON")
        .env_remove("HERDR_SOCKET_PATH")
        .env_remove("HERDR_PANE_ID")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("no target pane"));
}

#[test]
fn test_mode_is_fully_wired_and_logs_the_selected_backend() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    let state = temp.path().join("state");
    std::fs::create_dir_all(&config).unwrap();
    std::fs::write(
        config.join("config.toml"),
        "speak_command = [\"/bin/sh\", \"-c\", \"cat >/dev/null\"]\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .arg("--test")
        .env("HERDR_PLUGIN_CONFIG_DIR", &config)
        .env("HERDR_PLUGIN_STATE_DIR", &state)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let log = std::fs::read_to_string(state.join("announcer.log")).unwrap();
    assert!(log.contains("pane_id=- status=test action=announced+command "));
}

#[test]
fn failed_custom_speech_never_persists_or_prints_argv_secrets() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("config");
    let state = temp.path().join("state");
    std::fs::create_dir_all(&config).unwrap();
    let secret = "sk-sentinel-secret-4321";
    std::fs::write(
        config.join("config.toml"),
        format!("speak_command = [\"{secret}\"]\n"),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .arg("--test")
        .env("HERDR_PLUGIN_CONFIG_DIR", &config)
        .env("HERDR_PLUGIN_STATE_DIR", &state)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let combined = format!(
        "{}\n{}\n{}",
        String::from_utf8_lossy(&output.stderr),
        std::fs::read_to_string(state.join("last-error.json")).unwrap(),
        std::fs::read_to_string(state.join("announcer.log")).unwrap()
    );
    assert!(!combined.contains(secret));
    assert!(combined.contains("****4321"));
}

#[test]
fn help_and_bad_arguments_have_usage_exit_codes() {
    let help = run(&["--help"]);
    assert_eq!(help.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&help.stdout).starts_with("usage: herdr-announcer "));

    let bad = run(&["not-a-command"]);
    assert_eq!(bad.status.code(), Some(2));
    assert!(bad.stdout.is_empty());
    assert!(String::from_utf8_lossy(&bad.stderr).starts_with("usage: herdr-announcer "));
}
