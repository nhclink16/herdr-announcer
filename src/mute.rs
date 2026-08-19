use crate::atomicfile::write_json_atomic;
use crate::lockfile::with_flock;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::Path;

const RETENTION_SECONDS: f64 = 7.0 * 24.0 * 60.0 * 60.0;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct PaneMute {
    pub until: f64,
    pub at: f64,
    pub agent: String,
}

pub type PaneMutes = BTreeMap<String, PaneMute>;

fn state_path(state: &Path) -> std::path::PathBuf {
    state.join("pane-mutes.json")
}

fn load(path: &Path) -> PaneMutes {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn save(state: &Path, values: &PaneMutes) -> io::Result<()> {
    let value = serde_json::to_value(values).map_err(io::Error::other)?;
    write_json_atomic(&state_path(state), &value)
}

fn drop_expired(values: &mut PaneMutes, now: f64) -> bool {
    let before = values.len();
    values.retain(|_, entry| entry.until == 0.0 || entry.until > now);
    before != values.len()
}

fn list_locked(state: &Path, now: f64) -> io::Result<PaneMutes> {
    fs::create_dir_all(state)?;
    let path = state_path(state);
    with_flock(&state.join("mutes.lock"), || -> io::Result<PaneMutes> {
        let mut values = load(&path);
        if drop_expired(&mut values, now) {
            save(state, &values)?;
        }
        Ok(values)
    })?
}

pub fn list_pane_mutes(state: &Path, now: f64) -> PaneMutes {
    list_locked(state, now).unwrap_or_default()
}

pub fn is_pane_muted(state: &Path, pane_id: &str, now: f64) -> bool {
    list_locked(state, now)
        .map(|values| values.contains_key(pane_id))
        .unwrap_or(false)
}

pub fn set_pane_mute(
    state: &Path,
    pane_id: &str,
    until: f64,
    agent: &str,
    now: f64,
) -> io::Result<()> {
    fs::create_dir_all(state)?;
    let path = state_path(state);
    with_flock(&state.join("mutes.lock"), || -> io::Result<()> {
        let mut values = load(&path);
        drop_expired(&mut values, now);
        values.retain(|_, entry| entry.at >= now - RETENTION_SECONDS);
        values.insert(
            pane_id.to_owned(),
            PaneMute {
                until,
                at: now,
                agent: agent.to_owned(),
            },
        );
        save(state, &values)
    })?
}

fn remove_locked(state: &Path, pane_id: &str) -> io::Result<bool> {
    fs::create_dir_all(state)?;
    let path = state_path(state);
    with_flock(&state.join("mutes.lock"), || -> io::Result<bool> {
        let mut values = load(&path);
        let removed = values.remove(pane_id).is_some();
        if removed {
            save(state, &values)?;
        }
        Ok(removed)
    })?
}

pub fn remove_pane_mute(state: &Path, pane_id: &str) -> bool {
    remove_locked(state, pane_id).unwrap_or(false)
}

// Closing a workspace does not emit pane.closed hooks (verified live on herdr
// 0.8.0), so workspace.closed prunes every entry for that workspace by the
// "<workspace_id>:" pane-id prefix instead.
pub fn remove_workspace_mutes(state: &Path, workspace_id: &str) -> Vec<String> {
    remove_prefix_locked(state, &format!("{workspace_id}:")).unwrap_or_default()
}

fn remove_prefix_locked(state: &Path, prefix: &str) -> io::Result<Vec<String>> {
    fs::create_dir_all(state)?;
    let path = state_path(state);
    with_flock(&state.join("mutes.lock"), || -> io::Result<Vec<String>> {
        let mut values = load(&path);
        let removed: Vec<String> = values
            .keys()
            .filter(|pane_id| pane_id.starts_with(prefix))
            .cloned()
            .collect();
        if !removed.is_empty() {
            values.retain(|pane_id, _| !pane_id.starts_with(prefix));
            save(state, &values)?;
        }
        Ok(removed)
    })?
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: f64 = 1_700_000_000.0;

    #[test]
    fn round_trip_is_locked_compact_sorted_and_newline_terminated() {
        let temp = tempfile::tempdir().unwrap();
        set_pane_mute(temp.path(), "z-pane", 0.0, "Codex", NOW).unwrap();
        set_pane_mute(temp.path(), "a-pane", NOW + 300.0, "", NOW + 1.0).unwrap();
        let values = list_pane_mutes(temp.path(), NOW + 2.0);
        assert_eq!(values["z-pane"].agent, "Codex");
        assert_eq!(values["a-pane"].until, NOW + 300.0);
        assert!(temp.path().join("mutes.lock").is_file());
        assert_eq!(
            fs::read_to_string(temp.path().join("pane-mutes.json")).unwrap(),
            "{\"a-pane\":{\"agent\":\"\",\"at\":1700000001.0,\"until\":1700000300.0},\"z-pane\":{\"agent\":\"Codex\",\"at\":1700000000.0,\"until\":0.0}}\n"
        );
    }

    #[test]
    fn timed_entries_expire_on_read_and_are_persistently_removed() {
        let temp = tempfile::tempdir().unwrap();
        set_pane_mute(temp.path(), "timed", NOW + 10.0, "codex", NOW).unwrap();
        set_pane_mute(temp.path(), "closed", 0.0, "claude", NOW).unwrap();
        assert!(is_pane_muted(temp.path(), "timed", NOW + 9.0));
        assert!(!is_pane_muted(temp.path(), "timed", NOW + 10.0));
        let values = list_pane_mutes(temp.path(), NOW + 10.0);
        assert!(!values.contains_key("timed"));
        assert!(values.contains_key("closed"));
    }

    #[test]
    fn writes_prune_entries_with_at_older_than_seven_days() {
        let temp = tempfile::tempdir().unwrap();
        set_pane_mute(
            temp.path(),
            "old",
            0.0,
            "codex",
            NOW - RETENTION_SECONDS - 1.0,
        )
        .unwrap();
        set_pane_mute(temp.path(), "boundary", 0.0, "", NOW - RETENTION_SECONDS).unwrap();
        set_pane_mute(temp.path(), "new", 0.0, "", NOW).unwrap();
        let values = list_pane_mutes(temp.path(), NOW);
        assert!(!values.contains_key("old"));
        assert!(values.contains_key("boundary"));
        assert!(values.contains_key("new"));
    }

    #[test]
    fn remove_reports_whether_an_entry_existed() {
        let temp = tempfile::tempdir().unwrap();
        set_pane_mute(temp.path(), "pane", 0.0, "", NOW).unwrap();
        assert!(remove_pane_mute(temp.path(), "pane"));
        assert!(!remove_pane_mute(temp.path(), "pane"));
        assert_eq!(
            fs::read_to_string(temp.path().join("pane-mutes.json")).unwrap(),
            "{}\n"
        );
    }
}
