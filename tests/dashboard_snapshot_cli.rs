use std::path::Path;
use std::process::Command;

#[test]
fn non_tty_dashboard_dispatch_is_byte_identical_to_the_snapshot_golden() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let output = Command::new(env!("CARGO_BIN_EXE_herdr-announcer"))
        .arg("dashboard")
        .current_dir(root)
        .env("HERDR_PLUGIN_CONFIG_DIR", "tests/fixtures/dirs/config")
        .env("HERDR_PLUGIN_STATE_DIR", "tests/fixtures/dirs/state")
        .env("PATH", "/definitely/no/capabilities")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        include_str!("golden/snapshot.txt")
    );
}
