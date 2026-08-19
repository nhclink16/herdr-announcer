use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use toml_edit::{DocumentMut, Item};

pub const DEFAULT_KEYS: [&str; 20] = [
    "announce",
    "debounce_seconds",
    "summary",
    "summary_fallback",
    "summary_first_activity_timeout_seconds",
    "codex_model",
    "codex_effort",
    "codex_timeout_seconds",
    "summary_command",
    "summary_command_timeout_seconds",
    "style",
    "custom_prompt",
    "speak_command",
    "elevenlabs_api_key",
    "elevenlabs_voice_id",
    "elevenlabs_model",
    "voice",
    "toast",
    "mute_agents",
    "announce_on_detect",
];

#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    values: BTreeMap<String, Value>,
}

impl Default for Config {
    fn default() -> Self {
        let values = [
            ("announce", json!(["done", "blocked"])),
            ("debounce_seconds", json!(30)),
            ("summary", json!("codex")),
            ("summary_fallback", json!("template")),
            ("summary_first_activity_timeout_seconds", json!(5)),
            ("codex_model", json!("gpt-5.6-luna")),
            ("codex_effort", json!("low")),
            ("codex_timeout_seconds", json!(45)),
            ("summary_command", Value::Null),
            ("summary_command_timeout_seconds", json!(60)),
            ("style", json!("announcement")),
            ("custom_prompt", json!("")),
            ("speak_command", Value::Null),
            ("elevenlabs_api_key", json!("")),
            ("elevenlabs_voice_id", json!("21m00Tcm4TlvDq8ikWAM")),
            ("elevenlabs_model", json!("eleven_turbo_v2_5")),
            ("voice", json!("")),
            ("toast", json!(false)),
            ("mute_agents", json!([])),
            ("announce_on_detect", json!(false)),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value))
        .collect();
        Self { values }
    }
}

impl Config {
    pub fn get(&self, key: &str) -> &Value {
        self.values.get(key).unwrap_or(&Value::Null)
    }

    pub fn set(&mut self, key: &str, value: Value) {
        if DEFAULT_KEYS.contains(&key) {
            self.values.insert(key.to_owned(), value);
        }
    }

    pub fn string(&self, key: &str) -> Option<&str> {
        self.get(key).as_str()
    }

    pub fn string_array(&self, key: &str) -> Option<Vec<String>> {
        self.get(key)
            .as_array()?
            .iter()
            .map(|value| value.as_str().map(ToOwned::to_owned))
            .collect()
    }

    pub fn is_truthy(&self, key: &str) -> bool {
        match self.get(key) {
            Value::Null => false,
            Value::Bool(value) => *value,
            Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
            Value::String(value) => !value.is_empty(),
            Value::Array(value) => !value.is_empty(),
            Value::Object(value) => !value.is_empty(),
        }
    }
}

fn toml_value_to_json(value: &toml_edit::Value) -> Result<Value, String> {
    if let Some(value) = value.as_str() {
        return Ok(Value::String(value.to_owned()));
    }
    if let Some(value) = value.as_integer() {
        return Ok(json!(value));
    }
    if let Some(value) = value.as_float() {
        return serde_json::Number::from_f64(value)
            .map(Value::Number)
            .ok_or_else(|| "non-finite TOML floats are not supported in Phase 1".to_owned());
    }
    if let Some(value) = value.as_bool() {
        return Ok(Value::Bool(value));
    }
    if let Some(value) = value.as_datetime() {
        return Ok(Value::String(value.to_string()));
    }
    if let Some(array) = value.as_array() {
        return array.iter().map(toml_value_to_json).collect();
    }
    if let Some(table) = value.as_inline_table() {
        let mut result = Map::new();
        for (key, value) in table.iter() {
            result.insert(key.to_owned(), toml_value_to_json(value)?);
        }
        return Ok(Value::Object(result));
    }
    Err("unsupported TOML value".to_owned())
}

fn item_to_json(item: &Item) -> Result<Value, String> {
    if let Some(value) = item.as_value() {
        return toml_value_to_json(value);
    }
    if let Some(table) = item.as_table() {
        let mut result = Map::new();
        for (key, value) in table.iter() {
            result.insert(key.to_owned(), item_to_json(value)?);
        }
        return Ok(Value::Object(result));
    }
    if let Some(tables) = item.as_array_of_tables() {
        let mut result = Vec::new();
        for table in tables.iter() {
            let mut object = Map::new();
            for (key, value) in table.iter() {
                object.insert(key.to_owned(), item_to_json(value)?);
            }
            result.push(Value::Object(object));
        }
        return Ok(Value::Array(result));
    }
    Err("unsupported TOML item".to_owned())
}

pub fn load_config(
    dir: &Path,
    reasons: &mut Vec<String>,
    unknown: Option<&mut Vec<String>>,
) -> Result<Config, String> {
    let path = dir.join("config.toml");
    if !path.exists() {
        return Ok(Config::default());
    }
    let text = fs::read_to_string(&path).map_err(|error| error.to_string())?;
    let document = text
        .parse::<DocumentMut>()
        .map_err(|error| error.to_string())?;
    let mut config = Config::default();
    if let Some(unknown) = unknown {
        let mut keys: Vec<_> = document
            .iter()
            .map(|(key, _)| key.to_owned())
            .filter(|key| !DEFAULT_KEYS.contains(&key.as_str()))
            .collect();
        keys.sort();
        unknown.extend(keys);
    }
    for key in DEFAULT_KEYS {
        if let Some(item) = document.get(key) {
            config.values.insert(key.to_owned(), item_to_json(item)?);
        }
    }
    if !config.get("mute_agents").is_array() {
        reasons.push("config: mute_agents ignored".to_owned());
        config.values.insert("mute_agents".to_owned(), json!([]));
    }
    Ok(config)
}

fn push_python_string(output: &mut String, value: &str) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            character if character == ' ' || character.is_ascii_graphic() => {
                output.push(character);
            }
            character => {
                let code = character as u32;
                if code <= 0xffff {
                    output.push_str(&format!("\\u{code:04x}"));
                } else {
                    let value = code - 0x1_0000;
                    let high = 0xd800 + (value >> 10);
                    let low = 0xdc00 + (value & 0x3ff);
                    output.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
                }
            }
        }
    }
    output.push('"');
}

fn push_python_json(output: &mut String, value: &Value) {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(true) => output.push_str("true"),
        Value::Bool(false) => output.push_str("false"),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => push_python_string(output, value),
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    output.push_str(", ");
                }
                push_python_json(output, value);
            }
            output.push(']');
        }
        Value::Object(values) => {
            output.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index != 0 {
                    output.push_str(", ");
                }
                push_python_string(output, key);
                output.push_str(": ");
                push_python_json(output, value);
            }
            output.push('}');
        }
    }
}

pub fn to_python_json(value: &Value) -> String {
    let mut output = String::new();
    push_python_json(&mut output, value);
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_have_the_documented_order_and_values() {
        let config = Config::default();
        assert_eq!(DEFAULT_KEYS[0], "announce");
        assert_eq!(DEFAULT_KEYS[17], "toast");
        assert_eq!(DEFAULT_KEYS[18..], ["mute_agents", "announce_on_detect"]);
        assert_eq!(config.get("announce"), &json!(["done", "blocked"]));
        assert_eq!(config.get("summary_command"), &Value::Null);
    }

    #[test]
    fn missing_file_returns_defaults_without_creating_it() {
        let temp = tempfile::tempdir().unwrap();
        let config = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        assert_eq!(config, Config::default());
        assert!(!temp.path().join("config.toml").exists());
    }

    #[test]
    fn known_keys_merge_and_unknown_keys_are_sorted_and_dropped() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(
            temp.path().join("config.toml"),
            "summary = \"template\"\nzebra = 1\nalpha = \"x\"\n",
        )
        .unwrap();
        let mut unknown = Vec::new();
        let config = load_config(temp.path(), &mut Vec::new(), Some(&mut unknown)).unwrap();
        assert_eq!(config.get("summary"), &json!("template"));
        assert_eq!(config.get("zebra"), &Value::Null);
        assert_eq!(unknown, ["alpha", "zebra"]);
    }

    #[test]
    fn standard_toml_values_are_loaded() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(
            temp.path().join("config.toml"),
            "announce = [\"done\", \"blocked\"]\ndebounce_seconds = 12\nsummary = \"command\"\ncustom_prompt = \"keep # inside\"\ntoast = true\n",
        )
        .unwrap();
        let config = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        assert_eq!(config.get("announce"), &json!(["done", "blocked"]));
        assert_eq!(config.get("debounce_seconds"), &json!(12));
        assert_eq!(config.get("summary"), &json!("command"));
        assert_eq!(config.get("custom_prompt"), &json!("keep # inside"));
        assert_eq!(config.get("toast"), &json!(true));
    }

    #[test]
    fn non_array_mute_agents_degrades_to_empty_with_reason() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("config.toml"), "mute_agents = \"codex\"\n").unwrap();
        let mut reasons = Vec::new();
        let config = load_config(temp.path(), &mut reasons, None).unwrap();
        assert_eq!(config.get("mute_agents"), &json!([]));
        assert_eq!(reasons, ["config: mute_agents ignored"]);
    }

    #[test]
    fn malformed_toml_is_an_error() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("config.toml"), "announce = [\n").unwrap();
        assert!(load_config(temp.path(), &mut Vec::new(), None).is_err());
    }

    #[test]
    fn python_json_uses_python_spacing_literals_and_ascii_escaping() {
        let value = json!({"array": [1, true, null, "café 😀"], "object": {"x": false}});
        assert_eq!(
            to_python_json(&value),
            "{\"array\": [1, true, null, \"caf\\u00e9 \\ud83d\\ude00\"], \"object\": {\"x\": false}}"
        );
    }
}
