use crate::config::{Config, DEFAULT_KEYS, load_config, to_python_json};
use crate::redact::{mask_secret, redact_command, redact_command_text};
pub use crate::speech::{CAPABILITY_NAMES, capabilities};
use serde_json::Value;
use std::fs;
use std::path::Path;

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

fn command_value(value: &Value) -> Option<Vec<String>> {
    value.as_array().and_then(|values| {
        values
            .iter()
            .map(|value| value.as_str().map(ToOwned::to_owned))
            .collect()
    })
}

pub fn redact_configured_text(text: &str, config: &Config) -> String {
    let mut redacted = text.to_owned();
    for key in ["summary_command", "speak_command"] {
        if let Some(command) = command_value(config.get(key)) {
            redacted = redact_command_text(&redacted, &command);
        }
    }
    let api_key = python_str(config.get("elevenlabs_api_key"));
    if config.is_truthy("elevenlabs_api_key") && !api_key.is_empty() {
        redacted = redacted.replace(&api_key, &mask_secret(&api_key));
    }
    redacted
}

fn last_error(state: &Path) -> Option<(String, Vec<String>)> {
    let text = fs::read_to_string(state.join("last-error.json")).ok()?;
    let payload: Value = serde_json::from_str(&text).ok()?;
    let timestamp = payload.get("timestamp")?.as_str()?.to_owned();
    let reasons = payload
        .get("reasons")?
        .as_array()?
        .iter()
        .map(|reason| reason.as_str().map(ToOwned::to_owned))
        .collect::<Option<Vec<_>>>()?;
    Some((timestamp, reasons))
}

pub fn status(config_dir: &Path, state_dir: &Path) -> Result<String, String> {
    let config_path = config_dir.join("config.toml");
    let mut unknown = Vec::new();
    let config = load_config(config_dir, &mut Vec::new(), Some(&mut unknown))?;
    let detected = capabilities();
    let mut output = String::new();
    output.push_str("herdr-announcer status\n");
    output.push_str(&format!(
        "config: {} ({})\n",
        config_path.display(),
        if config_path.exists() {
            "exists"
        } else {
            "missing"
        }
    ));
    output.push_str(&format!("state: {}\n", state_dir.display()));
    output.push_str("values:\n");
    for key in DEFAULT_KEYS {
        let mut value = config.get(key).clone();
        if key == "elevenlabs_api_key" && config.is_truthy(key) {
            value = Value::String(mask_secret(&python_str(&value)));
        } else if matches!(key, "summary_command" | "speak_command")
            && let Some(command) = command_value(&value)
        {
            value = Value::Array(
                redact_command(&command)
                    .into_iter()
                    .map(Value::String)
                    .collect(),
            );
        }
        output.push_str(&format!("  {key} = {}\n", to_python_json(&value)));
    }
    output.push_str("capabilities:\n");
    for name in CAPABILITY_NAMES {
        output.push_str(&format!(
            "  {name}: {}\n",
            if detected.get(name).copied().unwrap_or(false) {
                "yes"
            } else {
                "no"
            }
        ));
    }
    output.push_str(&format!(
        "unrecognized keys: {}\n",
        if unknown.is_empty() {
            "none".to_owned()
        } else {
            unknown.join(", ")
        }
    ));
    if let Some((timestamp, reasons)) = last_error(state_dir) {
        let detail = redact_configured_text(&reasons.join(";"), &config);
        output.push_str(&format!("last error: {timestamp} {detail}\n"));
    } else {
        output.push_str("last error: none\n");
    }
    let log_path = state_dir.join("announcer.log");
    if log_path.exists() {
        output.push_str("log (last 8 lines):\n");
        let bytes = fs::read(&log_path).map_err(|error| error.to_string())?;
        let text = String::from_utf8_lossy(&bytes);
        let lines: Vec<_> = text.split_terminator('\n').collect();
        for line in lines.iter().skip(lines.len().saturating_sub(8)) {
            output.push_str("  ");
            output.push_str(&redact_configured_text(line, &config));
            output.push('\n');
        }
    } else {
        output.push_str(&format!("log: not found ({})\n", log_path.display()));
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_last_error_and_log_match_python_wording() {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("config");
        let state = temp.path().join("state");
        let output = status(&config, &state).unwrap();
        assert!(output.contains("last error: none\n"));
        assert!(output.contains(&format!(
            "log: not found ({})\n",
            state.join("announcer.log").display()
        )));
    }

    #[test]
    fn configured_secrets_are_redacted_from_error_and_log() {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("config");
        let state = temp.path().join("state");
        fs::create_dir_all(&config).unwrap();
        fs::create_dir_all(&state).unwrap();
        fs::write(
            config.join("config.toml"),
            "elevenlabs_api_key = \"eleven-secret-9876\"\nspeak_command = [\"helper\", \"--api-key\", \"sk-sentinel-4321\"]\n",
        )
        .unwrap();
        fs::write(
            state.join("last-error.json"),
            "{\"timestamp\":\"2026-08-18T10:00:00-04:00\",\"reasons\":[\"sk-sentinel-4321 eleven-secret-9876\"]}\n",
        )
        .unwrap();
        fs::write(
            state.join("announcer.log"),
            "line sk-sentinel-4321 eleven-secret-9876\n",
        )
        .unwrap();
        let output = status(&config, &state).unwrap();
        assert!(!output.contains("sk-sentinel-4321"));
        assert!(!output.contains("eleven-secret-9876"));
        assert!(output.contains("****4321"));
        assert!(output.contains("****9876"));
    }
}
