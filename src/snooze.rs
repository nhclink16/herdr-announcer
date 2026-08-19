use crate::atomicfile::write_json_atomic;
use jiff::Timestamp;
use jiff::tz::TimeZone;
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const SNOOZE_STEPS: [&str; 5] = ["5m", "30m", "2h", "tomorrow", "off"];
pub const SNOOZE_HOUR: i8 = 8;

fn wall_time() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

pub fn snooze_path(state: &Path) -> PathBuf {
    state.join("snooze.json")
}

pub fn read_snooze(state: &Path, now: f64) -> f64 {
    let Ok(text) = fs::read_to_string(snooze_path(state)) else {
        return 0.0;
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return 0.0;
    };
    let Some(until) = payload
        .as_object()
        .and_then(|payload| payload.get("until"))
        .and_then(Value::as_f64)
    else {
        return 0.0;
    };
    if until.is_finite() && until > now {
        until
    } else {
        0.0
    }
}

pub fn read_snooze_now(state: &Path) -> f64 {
    read_snooze(state, wall_time())
}

pub fn snooze_active(state: &Path, now: f64) -> bool {
    read_snooze(state, now) > 0.0
}

pub fn write_snooze(state: &Path, until: f64) -> std::io::Result<()> {
    write_json_atomic(&snooze_path(state), &json!({"until": until}))
}

fn zoned_at(now: f64, time_zone: TimeZone) -> Option<jiff::Zoned> {
    if !now.is_finite() || now < i64::MIN as f64 || now > i64::MAX as f64 {
        return None;
    }
    Timestamp::from_second(now as i64)
        .ok()
        .map(|timestamp| timestamp.to_zoned(time_zone))
}

fn next_morning_in(now: f64, time_zone: TimeZone) -> Option<f64> {
    let current = zoned_at(now, time_zone.clone())?;
    let mut date = current.date();
    let mut target = date.at(SNOOZE_HOUR, 0, 0, 0);
    if target <= current.datetime() {
        date = date.tomorrow().ok()?;
        target = date.at(SNOOZE_HOUR, 0, 0, 0);
    }
    target
        .to_zoned(time_zone)
        .ok()
        .map(|zoned| zoned.timestamp().as_second() as f64)
}

pub fn next_morning(now: f64) -> f64 {
    next_morning_in(now, TimeZone::system()).unwrap_or(0.0)
}

pub fn parse_duration(spec: &str) -> Option<f64> {
    let text = spec.trim().to_lowercase();
    if text.is_empty() {
        return None;
    }
    if matches!(text.as_str(), "off" | "0" | "none") {
        return Some(0.0);
    }
    let (number, multiplier) = match text.chars().last() {
        Some('s') => (&text[..text.len() - 1], 1_u64),
        Some('m') => (&text[..text.len() - 1], 60_u64),
        Some('h') => (&text[..text.len() - 1], 3600_u64),
        _ => (text.as_str(), 1_u64),
    };
    if number.is_empty() || !number.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    number
        .parse::<u64>()
        .ok()
        .and_then(|value| value.checked_mul(multiplier))
        .map(|value| value as f64)
}

pub fn set_snooze(state: &Path, spec: &str, now: f64) -> Option<f64> {
    let until = if spec.trim().eq_ignore_ascii_case("tomorrow") {
        next_morning(now)
    } else {
        let seconds = parse_duration(spec)?;
        if seconds <= 0.0 { 0.0 } else { now + seconds }
    };
    write_snooze(state, until).ok()?;
    Some(until)
}

pub fn format_snooze_remaining(until: f64, now: f64) -> String {
    let remaining = until - now;
    if !remaining.is_finite() || remaining <= 0.0 {
        return "off".to_owned();
    }
    let total = remaining as i64;
    if total >= 3600 {
        format!("{}h {:02}m left", total / 3600, (total % 3600) / 60)
    } else if total >= 60 {
        format!("{}m {:02}s left", total / 60, total % 60)
    } else {
        format!("{total}s left")
    }
}

fn local_zoned(until: f64) -> Option<jiff::Zoned> {
    zoned_at(until, TimeZone::system())
}

pub fn snooze_step(until: f64, now: f64) -> &'static str {
    let remaining = until - now;
    if !remaining.is_finite() || remaining <= 0.0 {
        return "off";
    }
    if local_zoned(until).is_some_and(|stamp| {
        stamp.hour() == SNOOZE_HOUR && stamp.minute() == 0 && stamp.second() == 0
    }) {
        return "tomorrow";
    }
    if remaining <= 300.0 {
        "5m"
    } else if remaining <= 1800.0 {
        "30m"
    } else {
        "2h"
    }
}

pub fn next_snooze_step(until: f64, now: f64) -> &'static str {
    let current = snooze_step(until, now);
    let index = SNOOZE_STEPS
        .iter()
        .position(|step| *step == current)
        .unwrap_or(SNOOZE_STEPS.len() - 1);
    SNOOZE_STEPS[(index + 1) % SNOOZE_STEPS.len()]
}

pub fn snooze_target_label(until: f64) -> String {
    local_zoned(until).map_or_else(
        || "until tomorrow".to_owned(),
        |stamp| format!("until {}", stamp.strftime("%H:%M")),
    )
}

pub fn snooze_label(until: f64, now: f64) -> String {
    let step = snooze_step(until, now);
    if step == "off" {
        return "off".to_owned();
    }
    let head = if step == "tomorrow" {
        snooze_target_label(until)
    } else {
        step.to_owned()
    };
    format!("{head} · {}", format_snooze_remaining(until, now))
}

pub fn snooze_message(until: f64, now: f64) -> String {
    match snooze_step(until, now) {
        "off" => "snooze off".to_owned(),
        "tomorrow" => format!("snoozed {}", snooze_target_label(until)),
        _ => format!("snoozed · {}", format_snooze_remaining(until, now)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    const NOW: f64 = 1_700_000_000.0;

    #[test]
    fn snooze_path_is_state_dir_snooze_json() {
        assert_eq!(
            snooze_path(Path::new("state")),
            Path::new("state/snooze.json")
        );
    }

    #[test]
    fn write_read_round_trip_is_atomic_and_exact() {
        let temp = tempfile::tempdir().unwrap();
        write_snooze(temp.path(), NOW + 60.0).unwrap();
        assert_eq!(read_snooze(temp.path(), NOW), NOW + 60.0);
        assert!(snooze_active(temp.path(), NOW));
        assert_eq!(
            fs::read_to_string(snooze_path(temp.path())).unwrap(),
            "{\"until\":1700000060.0}\n"
        );
        assert!(!fs::read_dir(temp.path()).unwrap().any(|entry| {
            entry
                .unwrap()
                .path()
                .extension()
                .is_some_and(|ext| ext == "tmp")
        }));
    }

    #[test]
    fn write_creates_missing_state_dir() {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("fresh");
        write_snooze(&state, NOW + 10.0).unwrap();
        assert!(snooze_path(&state).exists());
    }

    #[test]
    fn expired_missing_corrupt_and_invalid_values_are_off_without_rewrite() {
        let temp = tempfile::tempdir().unwrap();
        for content in [
            "{not json",
            "[1, 2, 3]",
            "42",
            "{\"until\":\"soon\"}",
            "{\"until\":1e999}",
            "{\"until\":Infinity}",
            "{\"until\":-Infinity}",
            "{\"until\":NaN}",
            "{\"other\":1}",
        ] {
            fs::write(snooze_path(temp.path()), content).unwrap();
            assert_eq!(read_snooze(temp.path(), NOW), 0.0, "{content}");
            assert_eq!(
                fs::read_to_string(snooze_path(temp.path())).unwrap(),
                content
            );
        }
        write_snooze(temp.path(), NOW).unwrap();
        assert_eq!(read_snooze(temp.path(), NOW), 0.0);
        write_snooze(temp.path(), NOW - 1.0).unwrap();
        assert_eq!(read_snooze(temp.path(), NOW), 0.0);
        assert_eq!(read_snooze(&temp.path().join("gone"), NOW), 0.0);
    }

    #[test]
    fn parse_duration_matches_python_table_and_rejects_junk() {
        for (spec, expected) in [
            ("30m", 1800.0),
            ("2h", 7200.0),
            ("45s", 45.0),
            ("90", 90.0),
            ("  30m  ", 1800.0),
            ("2H", 7200.0),
            ("off", 0.0),
            ("OFF", 0.0),
            ("0", 0.0),
            ("none", 0.0),
            ("None", 0.0),
        ] {
            assert_eq!(parse_duration(spec), Some(expected), "{spec}");
        }
        for spec in ["", "   ", "abc", "5x", "m30", "1.5h", "-", "30 m", "5min"] {
            assert_eq!(parse_duration(spec), None, "{spec}");
        }
    }

    #[test]
    fn set_snooze_writes_deadline_off_and_preserves_on_invalid() {
        let temp = tempfile::tempdir().unwrap();
        assert_eq!(set_snooze(temp.path(), "30m", NOW), Some(NOW + 1800.0));
        assert_eq!(set_snooze(temp.path(), "banana", NOW), None);
        assert_eq!(read_snooze(temp.path(), NOW), NOW + 1800.0);
        assert_eq!(set_snooze(temp.path(), "off", NOW), Some(0.0));
        assert!(snooze_path(temp.path()).exists());
        assert_eq!(read_snooze(temp.path(), NOW), 0.0);
    }

    #[test]
    fn remaining_strings_match_python() {
        for (until, expected) in [
            (0.0, "off"),
            (NOW - 5.0, "off"),
            (NOW, "off"),
            (NOW + 7200.0, "2h 00m left"),
            (NOW + 3600.0, "1h 00m left"),
            (NOW + 3599.0, "59m 59s left"),
            (NOW + 1800.0, "30m 00s left"),
            (NOW + 90.0, "1m 30s left"),
            (NOW + 60.0, "1m 00s left"),
            (NOW + 59.0, "59s left"),
            (NOW + 0.5, "0s left"),
        ] {
            assert_eq!(format_snooze_remaining(until, NOW), expected);
        }
    }

    #[test]
    fn step_and_cycle_boundaries_match_python() {
        for (until, expected) in [
            (0.0, "off"),
            (NOW + 60.0, "5m"),
            (NOW + 300.0, "5m"),
            (NOW + 301.0, "30m"),
            (NOW + 1800.0, "30m"),
            (NOW + 1801.0, "2h"),
        ] {
            assert_eq!(snooze_step(until, NOW), expected);
        }
        for (until, expected) in [
            (0.0, "5m"),
            (NOW + 60.0, "30m"),
            (NOW + 1500.0, "2h"),
            (NOW + 1801.0, "tomorrow"),
            (next_morning(NOW), "off"),
        ] {
            assert_eq!(next_snooze_step(until, NOW), expected);
        }
    }

    #[test]
    fn tomorrow_is_local_eight_and_messages_match() {
        let until = next_morning(NOW);
        let stamp = local_zoned(until).unwrap();
        assert_eq!((stamp.hour(), stamp.minute(), stamp.second()), (8, 0, 0));
        assert_eq!(snooze_step(until, NOW), "tomorrow");
        assert!(snooze_label(until, NOW).starts_with("until 08:00 · "));
        assert_eq!(snooze_message(until, NOW), "snoozed until 08:00");
        assert_eq!(snooze_label(NOW + 300.0, NOW), "5m · 5m 00s left");
        assert_eq!(snooze_message(NOW + 300.0, NOW), "snoozed · 5m 00s left");
    }

    #[test]
    fn next_morning_is_dst_correct() {
        let zone = TimeZone::get("America/New_York").unwrap();
        let before = jiff::civil::date(2026, 3, 7)
            .at(9, 0, 0, 0)
            .to_zoned(zone.clone())
            .unwrap()
            .timestamp()
            .as_second() as f64;
        let target = next_morning_in(before, zone).unwrap();
        assert_eq!(target - before, 22.0 * 3600.0);
    }
}
