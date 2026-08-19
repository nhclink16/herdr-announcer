use crate::atomicfile::write_json_atomic;
use crate::lockfile::with_flock;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEBOUNCE_MAX_AGE_SECONDS: f64 = 24.0 * 60.0 * 60.0;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct DebounceEntry {
    pub status: String,
    pub ts: f64,
}

pub type DebounceState = BTreeMap<String, DebounceEntry>;

fn wall_time() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

pub fn load_debounce_state(path: &Path) -> DebounceState {
    let Ok(text) = fs::read_to_string(path) else {
        return BTreeMap::new();
    };
    serde_json::from_str(&text).unwrap_or_default()
}

pub fn is_debounced(
    state: &DebounceState,
    pane: &str,
    status: &str,
    now: f64,
    seconds: i64,
) -> bool {
    let Some(previous) = state.get(pane) else {
        return false;
    };
    previous.status == status
        && previous.ts.is_finite()
        && previous.ts <= now
        && now - previous.ts <= seconds as f64
}

fn prune(state: &mut DebounceState, now: f64) {
    state.retain(|_, value| {
        value.ts.is_finite() && value.ts <= now && now - value.ts <= DEBOUNCE_MAX_AGE_SECONDS
    });
}

fn write_state(state_dir: &Path, state: &DebounceState) -> io::Result<()> {
    let value = serde_json::to_value(state).map_err(io::Error::other)?;
    write_json_atomic(&state_dir.join("last.json"), &value)
}

pub fn save_debounce_state(
    state_dir: &Path,
    state: &mut DebounceState,
    pane: &str,
    status: &str,
    now: f64,
) -> io::Result<()> {
    prune(state, now);
    state.insert(
        pane.to_owned(),
        DebounceEntry {
            status: status.to_owned(),
            ts: now,
        },
    );
    write_state(state_dir, state)
}

fn reserve_debounce_at(
    state: &Path,
    pane: &str,
    status: &str,
    seconds: i64,
    now: f64,
) -> io::Result<(bool, Option<f64>)> {
    fs::create_dir_all(state)?;
    let lock = state.join("debounce.lock");
    with_flock(&lock, || -> io::Result<(bool, Option<f64>)> {
        let mut values = load_debounce_state(&state.join("last.json"));
        if is_debounced(&values, pane, status, now, seconds) {
            return Ok((true, None));
        }
        save_debounce_state(state, &mut values, pane, status, now)?;
        let persisted = load_debounce_state(&state.join("last.json"))
            .get(pane)
            .map_or(now, |entry| entry.ts);
        Ok((false, Some(persisted)))
    })?
}

pub fn reserve_debounce(
    state: &Path,
    pane: &str,
    status: &str,
    secs: i64,
) -> io::Result<(bool, Option<f64>)> {
    reserve_debounce_at(state, pane, status, secs, wall_time())
}

pub fn check_and_record_debounce(
    state: &Path,
    pane: &str,
    status: &str,
    secs: i64,
) -> io::Result<bool> {
    reserve_debounce(state, pane, status, secs).map(|value| value.0)
}

pub fn rollback_debounce(
    state: &Path,
    pane: &str,
    status: &str,
    reservation: Option<f64>,
) -> io::Result<()> {
    fs::create_dir_all(state)?;
    let lock = state.join("debounce.lock");
    with_flock(&lock, || -> io::Result<()> {
        let mut values = load_debounce_state(&state.join("last.json"));
        let remove = values.get(pane).is_some_and(|value| {
            value.status == status && reservation.is_none_or(|token| value.ts == token)
        });
        if remove {
            values.remove(pane);
            write_state(state, &values)?;
        }
        Ok(())
    })?
}

pub fn state_as_json(state: &DebounceState) -> Value {
    serde_json::to_value(state).unwrap_or_else(|_| Value::Object(Default::default()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonfinite_and_future_timestamps_do_not_debounce() {
        for timestamp in [f64::INFINITY, f64::NAN, 101.0] {
            let mut state = DebounceState::new();
            state.insert(
                "pane".to_owned(),
                DebounceEntry {
                    status: "done".to_owned(),
                    ts: timestamp,
                },
            );
            assert!(!is_debounced(&state, "pane", "done", 100.0, 30));
        }
    }

    #[test]
    fn old_rollback_cannot_delete_a_newer_reservation() {
        let temp = tempfile::tempdir().unwrap();
        let (_, first) = reserve_debounce_at(temp.path(), "pane", "done", 30, 100.0).unwrap();
        let (_, second) = reserve_debounce_at(temp.path(), "pane", "done", 30, 131.0).unwrap();
        assert_ne!(first, second);
        rollback_debounce(temp.path(), "pane", "done", first).unwrap();
        assert_eq!(
            load_debounce_state(&temp.path().join("last.json"))["pane"].ts,
            second.unwrap()
        );
    }

    #[test]
    fn record_and_rollback_use_real_lock_file() {
        let temp = tempfile::tempdir().unwrap();
        assert!(!check_and_record_debounce(temp.path(), "pane", "done", 30).unwrap());
        assert!(check_and_record_debounce(temp.path(), "pane", "done", 30).unwrap());
        rollback_debounce(temp.path(), "pane", "done", None).unwrap();
        assert!(!load_debounce_state(&temp.path().join("last.json")).contains_key("pane"));
        assert!(temp.path().join("debounce.lock").is_file());
    }

    #[test]
    fn saving_state_prunes_old_invalid_and_future_entries() {
        let temp = tempfile::tempdir().unwrap();
        let mut state = DebounceState::from([
            (
                "old".to_owned(),
                DebounceEntry {
                    status: "done".to_owned(),
                    ts: 1.0,
                },
            ),
            (
                "recent".to_owned(),
                DebounceEntry {
                    status: "done".to_owned(),
                    ts: 90_000.0,
                },
            ),
            (
                "future".to_owned(),
                DebounceEntry {
                    status: "done".to_owned(),
                    ts: 90_002.0,
                },
            ),
        ]);
        save_debounce_state(temp.path(), &mut state, "new", "blocked", 90_001.0).unwrap();
        let saved = load_debounce_state(&temp.path().join("last.json"));
        assert!(!saved.contains_key("old"));
        assert!(!saved.contains_key("future"));
        assert!(saved.contains_key("recent"));
        assert!(saved.contains_key("new"));
        let raw = fs::read_to_string(temp.path().join("last.json")).unwrap();
        assert!(raw.ends_with('\n'));
        assert!(raw.find("\"new\"").unwrap() < raw.find("\"recent\"").unwrap());
    }
}
