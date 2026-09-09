use crate::config::{Config, to_python_json};
use crate::log::LogEntry;
use crate::redact::redact_command;
use crate::snooze::{SNOOZE_STEPS, format_snooze_remaining, snooze_label};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

pub const WIDTH: usize = 86;
pub const FRAME_HEIGHT: usize = 28;
pub const RECENT_LINES: usize = 5;
pub const STATE_ORDER: [&str; 5] = ["done", "blocked", "idle", "working", "unknown"];
pub const CAPABILITY_NAMES: [&str; 6] =
    ["codex", "claude", "say", "spd-say", "espeak-ng", "espeak"];
pub const FOOTER: &str =
    "j/k or arrows move · space/enter toggle · s snooze · t test · w wizard · r reload · q quit";

fn clip(value: &str, width: usize) -> String {
    let plain = value
        .replace("\r\n", " ")
        .replace(['\n', '\r', '\t', '\u{0b}', '\u{0c}'], " ");
    if width == 0 {
        return String::new();
    }
    if plain.chars().count() <= width {
        return plain;
    }
    plain.chars().take(width - 1).collect::<String>() + "…"
}

fn pad(value: &str, width: usize) -> String {
    let mut value = clip(value, width);
    let missing = width.saturating_sub(value.chars().count());
    value.push_str(&" ".repeat(missing));
    value
}

pub fn format_timestamp(timestamp: &str) -> String {
    timestamp.split_once('T').map_or_else(
        || clip(timestamp, 8),
        |(_, clock)| clock.chars().take(8).collect(),
    )
}

fn announce_states(config: &Config) -> Vec<&'static str> {
    let selected: BTreeSet<_> = config
        .get("announce")
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_lowercase)
        .collect();
    STATE_ORDER
        .into_iter()
        .filter(|state| selected.contains(*state))
        .collect()
}

fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        _ => to_python_json(value),
    }
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&byte))
    {
        return value.to_owned();
    }
    if value.is_empty() {
        return "''".to_owned();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

fn voice_backend_label(config: &Config, caps: &BTreeMap<String, bool>) -> String {
    let command = config.get("speak_command");
    if !command.is_null() {
        let Some(command) = command.as_array() else {
            return "custom command  (invalid)".to_owned();
        };
        let command: Vec<_> = command.iter().map(python_str).collect();
        let rendered = redact_command(&command)
            .iter()
            .map(|argument| shell_quote(argument))
            .collect::<Vec<_>>()
            .join(" ");
        return format!("custom command  {}", clip(&rendered, 48));
    }
    if config.is_truthy("elevenlabs_api_key") {
        return format!(
            "ElevenLabs  voice {}",
            python_str(config.get("elevenlabs_voice_id"))
        );
    }
    if cfg!(target_os = "macos") {
        let voice = config.string("voice").unwrap_or("");
        if !voice.is_empty() {
            return format!("local say  voice {voice}");
        }
        return "local say  system voice".to_owned();
    }
    if cfg!(target_os = "linux") {
        let found: Vec<_> = ["spd-say", "espeak-ng", "espeak"]
            .into_iter()
            .filter(|name| caps.get(*name).copied().unwrap_or(false))
            .collect();
        if !found.is_empty() {
            return format!("local {}", found.join(" / "));
        }
        return "local  nothing detected!".to_owned();
    }
    format!("unsupported platform: {}", std::env::consts::OS)
}

fn fit(mut lines: Vec<String>, droppable: Vec<usize>) -> Vec<String> {
    let surplus = lines.len().saturating_sub(FRAME_HEIGHT);
    if surplus > 0 {
        let removed: BTreeSet<_> = droppable.into_iter().take(surplus).collect();
        lines = lines
            .into_iter()
            .enumerate()
            .filter_map(|(index, line)| (!removed.contains(&index)).then_some(line))
            .collect();
    }
    lines.resize(FRAME_HEIGHT, String::new());
    lines.truncate(FRAME_HEIGHT);
    lines
}

pub fn render(
    config_path: &Path,
    config: &Config,
    entries: &[LogEntry],
    snooze_until: f64,
    caps: &BTreeMap<String, bool>,
    now: f64,
) -> String {
    let states = announce_states(config);
    let badge = if states.is_empty() {
        "silent · no states selected".to_owned()
    } else if snooze_until > now {
        format!("snoozed · {}", format_snooze_remaining(snooze_until, now))
    } else {
        "live".to_owned()
    };
    let rail = "│";
    let mut recent_extra = Vec::new();
    let mut spacers = Vec::new();
    let mut optional = Vec::new();
    let mut last_resort = Vec::new();
    let mut lines = vec![
        format!("◆ Announcer  {badge}"),
        format!(
            "{rail}  {}",
            clip(&format!("config {}", config_path.display()), WIDTH - 3)
        ),
        rail.to_owned(),
        format!("{rail}  Recent"),
    ];
    last_resort.push(1);
    spacers.push(2);
    last_resort.push(3);

    let mut rows = Vec::new();
    let recent: Vec<_> = entries.iter().rev().take(RECENT_LINES).collect();
    if recent.is_empty() {
        rows.push(format!("{rail}    no announcements logged yet"));
    }
    for entry in recent {
        let mut text = format!(
            "{}  {}  {}  {}",
            pad(&format_timestamp(&entry.timestamp), 8),
            pad(&entry.status, 8),
            pad(&entry.pane_id, 12),
            entry.action
        );
        if !entry.reasons.is_empty() {
            text.push_str("  (");
            text.push_str(&entry.reasons.join(";"));
            text.push(')');
        }
        rows.push(format!("{rail}    {}", clip(&text, WIDTH - 5)));
    }
    rows.resize(RECENT_LINES, rail.to_owned());
    for (position, row) in rows.into_iter().enumerate() {
        lines.push(row);
        if position != 0 {
            recent_extra.push(lines.len() - 1);
        }
    }
    recent_extra.reverse();

    lines.push(rail.to_owned());
    spacers.push(lines.len() - 1);
    let text = format!("{}{}", pad("Snooze", 9), snooze_label(snooze_until, now));
    let hint = format!("s cycles {}", SNOOZE_STEPS.join(" / "));
    lines.push(format!(
        "{rail} ❯ {}  {}",
        clip(&text, 40),
        clip(&hint, WIDTH.saturating_sub(46))
    ));

    lines.push(rail.to_owned());
    spacers.push(lines.len() - 1);
    lines.push(format!("{rail}  Announce on"));
    let help = [
        ("done", "an agent finished work you weren't watching"),
        ("blocked", "an agent is waiting on your input"),
        ("idle", "an agent settled while you were watching"),
        ("working", "an agent started doing something (chatty)"),
        ("unknown", "unrecognized agent activity (chatty)"),
    ];
    for (state, description) in help {
        let checked = if states.contains(&state) {
            "◼"
        } else {
            "◻"
        };
        let text = format!("{}{description}", pad(state, 9));
        lines.push(format!("{rail}  {checked} {}", clip(&text, WIDTH - 5)));
    }

    lines.push(rail.to_owned());
    spacers.push(lines.len() - 1);
    let checked = if config.is_truthy("toast") {
        "◼"
    } else {
        "◻"
    };
    let text = format!(
        "{}mirror each announcement as a Herdr notification",
        pad("toast", 9)
    );
    lines.push(format!("{rail}  {checked} {}", clip(&text, WIDTH - 5)));
    lines.push(format!(
        "{rail}  {}{}",
        pad("voice", 9),
        clip(&voice_backend_label(config, caps), WIDTH - 14)
    ));
    optional.push(lines.len() - 1);
    let mut parts = Vec::new();
    let mut used = 0;
    for name in CAPABILITY_NAMES {
        let cost = name.len() + 2 + if parts.is_empty() { 0 } else { 2 };
        if used + cost > WIDTH - 12 {
            break;
        }
        parts.push(format!(
            "{name} {}",
            if caps.get(name).copied().unwrap_or(false) {
                "✓"
            } else {
                "✗"
            }
        ));
        used += cost;
    }
    lines.push(format!("{rail}  {}{}", pad("tools", 9), parts.join("  ")));
    optional.push(lines.len() - 1);
    optional.reverse();

    lines.push(rail.to_owned());
    spacers.push(lines.len() - 1);
    lines.push(format!(
        "{rail}  ▸ {}",
        clip(
            &format!("{}speak a sample announcement now", pad("Test voice", 15)),
            WIDTH - 5
        )
    ));
    lines.push(format!(
        "{rail}  ▸ {}",
        clip(
            &format!(
                "{}open the setup wizard (replaces this screen)",
                pad("Full setup", 15)
            ),
            WIDTH - 5
        )
    ));
    lines.push("└".to_owned());
    lines.push(FOOTER.to_owned());
    lines.push(String::new());

    spacers.reverse();
    let droppable = recent_extra
        .into_iter()
        .chain(spacers)
        .chain(optional)
        .chain(last_resort)
        .collect();
    let mut output = fit(lines, droppable).join("\n");
    while output.ends_with(char::is_whitespace) {
        output.pop();
    }
    output.push('\n');
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::load_config;

    #[test]
    fn text_helpers_match_character_based_python_behavior() {
        assert_eq!(clip("  a \n b\tc  ", 40), "  a   b c  ");
        assert_eq!(clip("a\r\nb", 40), "a b");
        assert_eq!(clip(&"x".repeat(200), 12).chars().count(), 12);
        assert_eq!(pad("ab", 9).chars().count(), 9);
        assert_eq!(format_timestamp("2026-08-17T12:04:11+02:00"), "12:04:11");
        assert_eq!(format_timestamp("not-a-timestamp"), "not-a-t…");
    }

    #[test]
    fn pure_render_is_28_lines_newest_first_and_has_no_mute_info() {
        let temp = tempfile::tempdir().unwrap();
        let config = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        let entries = [
            LogEntry {
                timestamp: "2026-08-17T12:04:11+02:00".to_owned(),
                pane_id: "pane-1".to_owned(),
                status: "done".to_owned(),
                action: "older".to_owned(),
                elapsed: 0.1,
                reasons: Vec::new(),
                raw: String::new(),
            },
            LogEntry {
                timestamp: "2026-08-17T12:05:11+02:00".to_owned(),
                pane_id: "pane-2".to_owned(),
                status: "blocked".to_owned(),
                action: "newer".to_owned(),
                elapsed: 0.2,
                reasons: vec!["reason".to_owned()],
                raw: String::new(),
            },
        ];
        let output = render(
            &temp.path().join("config.toml"),
            &config,
            &entries,
            0.0,
            &BTreeMap::new(),
            1_700_000_000.0,
        );
        // The pure render has 28 rows. Python's snapshot serializer applies
        // rstrip() to its joined rows, so the final blank message row is not
        // represented in the byte stream.
        assert_eq!(output.lines().count() + 1, FRAME_HEIGHT);
        // Match whole log rows: the config-path row above them can contain
        // arbitrary substrings (macOS temp dirs live under /var/folders).
        let newer_row = output.find("blocked   pane-2").unwrap();
        let older_row = output.find("done      pane-1").unwrap();
        assert!(newer_row < older_row, "newest log row must render first");
        assert!(!output.contains("Muted"));
        assert!(output.ends_with('\n'));
    }
}
