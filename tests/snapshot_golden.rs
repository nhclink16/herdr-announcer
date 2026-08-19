use herdr_announcer::config::load_config;
use herdr_announcer::log::read_log;
use herdr_announcer::snapshot::{CAPABILITY_NAMES, render};
use herdr_announcer::snooze::read_snooze;
use std::collections::BTreeMap;
use std::path::Path;

const NOW: f64 = 1_700_000_000.0;

#[test]
fn snapshot_is_byte_identical_to_python() {
    let config_dir = Path::new("tests/fixtures/dirs/config");
    let state_dir = Path::new("tests/fixtures/dirs/state");
    let config = load_config(config_dir, &mut Vec::new(), None).unwrap();
    let entries = read_log(&state_dir.join("announcer.log"), 5);
    let snooze = read_snooze(state_dir, NOW);
    let caps: BTreeMap<_, _> = CAPABILITY_NAMES
        .into_iter()
        .map(|name| (name.to_owned(), false))
        .collect();

    let actual = render(
        &config_dir.join("config.toml"),
        &config,
        &entries,
        snooze,
        &caps,
        NOW,
    );
    assert_eq!(actual, include_str!("golden/snapshot.txt"));
    assert!(!actual.to_lowercase().contains("mute"));
}
