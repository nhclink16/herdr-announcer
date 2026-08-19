use std::process::Command;

const CONFIG_DIR: &str = "tests/fixtures/dirs/config";
const STATE_DIR: &str = "tests/fixtures/dirs/state";
const NEW_LINES: [&str; 2] = ["  mute_agents = []\n", "  announce_on_detect = false\n"];

#[test]
fn status_matches_python_plus_the_two_new_config_keys() {
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .arg("status")
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .env("HERDR_PLUGIN_CONFIG_DIR", CONFIG_DIR)
        .env("HERDR_PLUGIN_STATE_DIR", STATE_DIR)
        .env("PATH", "/nonexistent")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());

    let actual = String::from_utf8(output.stdout).unwrap();
    for line in NEW_LINES {
        assert_eq!(
            actual.matches(line).count(),
            1,
            "missing or duplicate {line:?}"
        );
    }
    let without_new_keys = NEW_LINES
        .into_iter()
        .fold(actual, |text, line| text.replacen(line, "", 1));
    assert_eq!(without_new_keys, include_str!("golden/status.txt"));
}
