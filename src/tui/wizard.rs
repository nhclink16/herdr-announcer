use super::widgets::{LinePrompter, MULTI_HINT, PromptError, PromptUi, TtyPrompter};
use crate::config::{Config, DEFAULT_KEYS, load_config, to_python_json};
use crate::config_write::{
    RollbackOutcome, SetupWriteBoundary, rollback_setup_write, write_setup_config,
};
use crate::redact::mask_secret;
use crate::speech::{capabilities, speak};
use crate::summarize::ANNOUNCEMENT_PROMPT;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{self, IsTerminal};
use std::path::Path;

const STATE_OPTIONS: [(&str, &str); 5] = [
    ("done", "done - an agent finished work you weren't watching"),
    ("blocked", "blocked - an agent is waiting on your input"),
    ("idle", "idle - an agent settled while you were watching"),
    (
        "working",
        "working - an agent started doing something (chatty)",
    ),
    ("unknown", "unknown - unrecognized agent activity (chatty)"),
];

fn value_string(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Null => String::new(),
        _ => value.to_string(),
    }
}

fn option_refs(options: &[(String, String)]) -> Vec<(&str, &str)> {
    options
        .iter()
        .map(|(value, label)| (value.as_str(), label.as_str()))
        .collect()
}

fn ask_owned_select(
    ui: &mut dyn PromptUi,
    title: &str,
    options: &[(String, String)],
    default: &str,
) -> Result<(String, bool), PromptError> {
    ui.select(title, &option_refs(options), default, "")
}

pub fn claude_summary_command() -> Vec<String> {
    vec![
        "claude".to_owned(),
        "-p".to_owned(),
        "--model".to_owned(),
        "haiku".to_owned(),
        format!("{ANNOUNCEMENT_PROMPT} The terminal output is provided on stdin."),
    ]
}

fn toml_value(value: &Value) -> Result<String, String> {
    match value {
        Value::Bool(value) => Ok(if *value { "true" } else { "false" }.to_owned()),
        Value::Number(value) if value.as_i64().is_some() => Ok(value.to_string()),
        Value::String(_) => Ok(to_python_json(value)),
        Value::Array(values) if values.iter().all(Value::is_string) => Ok(to_python_json(value)),
        _ => Err("cannot write configuration value".to_owned()),
    }
}

pub fn config_lines(config: &Config, explicitly_chosen: &[String]) -> Result<Vec<String>, String> {
    let chosen: BTreeSet<_> = explicitly_chosen.iter().map(String::as_str).collect();
    let defaults = Config::default();
    DEFAULT_KEYS
        .into_iter()
        .filter(|key| {
            !config.get(key).is_null()
                && (chosen.contains(key) || config.get(key) != defaults.get(key))
        })
        .map(|key| Ok(format!("{key} = {}", toml_value(config.get(key))?)))
        .collect()
}

pub fn preview_line(line: &str) -> String {
    let Some(value) = line.strip_prefix("elevenlabs_api_key = ") else {
        return line.to_owned();
    };
    let secret = serde_json::from_str::<String>(value).unwrap_or_default();
    format!(
        "elevenlabs_api_key = {}",
        to_python_json(&json!(mask_secret(&secret)))
    )
}

fn chosen_updates<'a>(config: &Config, chosen: &[String]) -> Vec<(&'a str, Value)> {
    let chosen: BTreeSet<_> = chosen.iter().map(String::as_str).collect();
    DEFAULT_KEYS
        .into_iter()
        .filter(|key| chosen.contains(key))
        .map(|key| (key, config.get(key).clone()))
        .collect()
}

fn add_chosen(chosen: &mut Vec<String>, key: &str) {
    chosen.push(key.to_owned());
}

fn run_wizard_flow(
    ui: &mut dyn PromptUi,
    config_dir: &Path,
    state_dir: &Path,
    boundary: &mut Option<SetupWriteBoundary>,
    detected: &BTreeMap<String, bool>,
) -> Result<u8, PromptError> {
    let config_path = config_dir.join("config.toml");
    let existed = config_path.exists();
    let mut reasons = Vec::new();
    let mut config = load_config(config_dir, &mut reasons, None)
        .map_err(|error| PromptError::Io(io::Error::new(io::ErrorKind::InvalidData, error)))?;
    let mut chosen = Vec::new();

    ui.print_title("herdr-announcer setup")?;
    ui.print_line(&format!("Config: {}", config_path.display()))?;
    ui.print_line(if ui.is_fancy() {
        "Arrows move, Enter confirms. q quits choices; Esc or Ctrl-C exits without writing anything."
    } else {
        "Enter keeps the value in [brackets]. q quits choices; Ctrl-C exits without writing anything."
    })?;
    ui.print_blank()?;

    let default_states = config
        .string_array("announce")
        .unwrap_or_else(|| vec!["done".to_owned(), "blocked".to_owned()]);
    let (states, changed) = ui.multiselect(
        "When should it speak?",
        &STATE_OPTIONS,
        &default_states,
        MULTI_HINT,
    )?;
    config.set("announce", json!(states));
    if changed {
        add_chosen(&mut chosen, "announce");
    }

    let has_custom_summary = config
        .get("summary_command")
        .as_array()
        .is_some_and(|command| !command.is_empty());
    let mut summary_options = Vec::new();
    if detected.get("codex").copied().unwrap_or(false) {
        summary_options.push((
            "codex".to_owned(),
            "Codex - one-sentence summary via codex exec".to_owned(),
        ));
    }
    if has_custom_summary {
        summary_options.push((
            "command".to_owned(),
            "Custom - keep your current summary command".to_owned(),
        ));
    } else if detected.get("claude").copied().unwrap_or(false) {
        summary_options.push((
            "command".to_owned(),
            "Claude Code - one-sentence summary via claude -p".to_owned(),
        ));
    }
    summary_options.push((
        "template".to_owned(),
        "None - instant fixed phrasing, no LLM".to_owned(),
    ));
    let current_summary = config.string("summary").unwrap_or_default();
    let current_summary = if summary_options
        .iter()
        .any(|(value, _)| value == current_summary)
    {
        current_summary
    } else {
        &summary_options[0].0
    };
    let (summary, changed) = ask_owned_select(
        ui,
        "Who writes the summary sentence?",
        &summary_options,
        current_summary,
    )?;
    config.set("summary", json!(summary));
    if changed {
        add_chosen(&mut chosen, "summary");
    }
    if summary == "command" {
        if !has_custom_summary {
            config.set("summary_command", json!(claude_summary_command()));
        }
        add_chosen(&mut chosen, "summary_command");
    }
    if summary == "codex" {
        let (model, explicit) = ui.text(
            "Codex model",
            config.string("codex_model").unwrap_or_default(),
            None,
        )?;
        config.set("codex_model", json!(model));
        if explicit {
            add_chosen(&mut chosen, "codex_model");
        }
        let (effort, changed) = ui.select(
            "Codex reasoning effort",
            &[
                ("low", "low - fast, plenty for a one-line summary"),
                ("medium", "medium - a touch more careful"),
                ("high", "high - slow, rarely worth it here"),
            ],
            config.string("codex_effort").unwrap_or("low"),
            "",
        )?;
        config.set("codex_effort", json!(effort));
        if changed {
            add_chosen(&mut chosen, "codex_effort");
        }
    }

    if summary != "template" {
        let current_style = config
            .string("style")
            .filter(|value| matches!(*value, "announcement" | "summary" | "custom"))
            .unwrap_or("announcement");
        let (style, changed) = ui.select(
            "How should it sound?",
            &[
                (
                    "announcement",
                    "Announcer - \"Builder finished the work and tests passed.\"",
                ),
                ("summary", "Factual - plain report, no radio voice"),
                ("custom", "Custom - write your own prompt"),
            ],
            current_style,
            "",
        )?;
        config.set("style", json!(style));
        if changed {
            add_chosen(&mut chosen, "style");
        }
        if style == "custom" {
            loop {
                let (prompt, explicit) = ui.text(
                    "Prompt template ({agent} {workspace} {status} substituted; transcript appended)",
                    config.string("custom_prompt").unwrap_or_default(),
                    None,
                )?;
                if !prompt.is_empty() {
                    config.set("custom_prompt", json!(prompt));
                    add_chosen(&mut chosen, "custom_prompt");
                    break;
                }
                if !explicit {
                    ui.print_line("No template given - keeping announcement style.")?;
                    config.set("style", json!("announcement"));
                    break;
                }
            }
        }
    }

    let local_names: Vec<_> = ["say", "spd-say", "espeak-ng", "espeak"]
        .into_iter()
        .filter(|name| detected.get(*name).copied().unwrap_or(false))
        .collect();
    let local_detected = if local_names.is_empty() {
        "nothing detected!".to_owned()
    } else {
        local_names.join(", ")
    };
    let mut voice_options = Vec::new();
    if existed {
        voice_options.push(("keep".to_owned(), "Keep current voice settings".to_owned()));
    }
    voice_options.extend([
        (
            "local".to_owned(),
            format!("This machine - built-in text-to-speech ({local_detected})"),
        ),
        (
            "elevenlabs".to_owned(),
            "ElevenLabs - natural voice, needs an API key".to_owned(),
        ),
        (
            "custom".to_owned(),
            "Custom command - ssh somewhere, ntfy push, any script".to_owned(),
        ),
    ]);
    let (voice_backend, _) = ask_owned_select(
        ui,
        "Where should the voice come out?",
        &voice_options,
        if existed { "keep" } else { "local" },
    )?;
    match voice_backend.as_str() {
        "local" => {
            config.set("speak_command", Value::Null);
            config.set("elevenlabs_api_key", json!(""));
            add_chosen(&mut chosen, "speak_command");
            add_chosen(&mut chosen, "elevenlabs_api_key");
            if cfg!(target_os = "macos") {
                let (voice, explicit) = ui.text(
                    "macOS voice name (blank = system voice)",
                    config.string("voice").unwrap_or_default(),
                    None,
                )?;
                config.set("voice", json!(voice));
                if explicit {
                    add_chosen(&mut chosen, "voice");
                }
            }
        }
        "elevenlabs" => {
            let current_key = config.string("elevenlabs_api_key").unwrap_or_default();
            let masked = mask_secret(current_key);
            let (api_key, key_explicit) =
                ui.secret("ElevenLabs API key", current_key, Some(&masked))?;
            if api_key.is_empty() {
                ui.print_line(
                    "No key entered - ElevenLabs stays inactive; local TTS will be used.",
                )?;
            }
            let (voice_id, voice_explicit) = ui.text(
                "ElevenLabs voice id",
                config.string("elevenlabs_voice_id").unwrap_or_default(),
                None,
            )?;
            let (model, model_explicit) = ui.text(
                "ElevenLabs model",
                config.string("elevenlabs_model").unwrap_or_default(),
                None,
            )?;
            config.set("speak_command", Value::Null);
            config.set("elevenlabs_api_key", json!(api_key));
            config.set("elevenlabs_voice_id", json!(voice_id));
            config.set("elevenlabs_model", json!(model));
            add_chosen(&mut chosen, "speak_command");
            add_chosen(&mut chosen, "elevenlabs_api_key");
            if voice_explicit {
                add_chosen(&mut chosen, "elevenlabs_voice_id");
            }
            if model_explicit {
                add_chosen(&mut chosen, "elevenlabs_model");
            }
            if key_explicit {
                add_chosen(&mut chosen, "elevenlabs_api_key");
            }
        }
        "custom" => {
            let current_line = config
                .string_array("speak_command")
                .map(shell_words::join)
                .unwrap_or_default();
            loop {
                let (line, explicit) = ui.text(
                    "Command ({text} substituted, or announcement on stdin)",
                    &current_line,
                    None,
                )?;
                match shell_words::split(&line) {
                    Ok(command) if !command.is_empty() => {
                        config.set("speak_command", json!(command));
                        config.set("elevenlabs_api_key", json!(""));
                        add_chosen(&mut chosen, "speak_command");
                        add_chosen(&mut chosen, "elevenlabs_api_key");
                        break;
                    }
                    Ok(_) if !explicit => {
                        ui.print_line("No command given - keeping current voice settings.")?;
                        break;
                    }
                    Ok(_) => {}
                    Err(error) => {
                        ui.print_line(&format!("Invalid command: {error}"))?;
                    }
                }
            }
        }
        _ => {}
    }

    let (toast, changed) = ui.confirm(
        "Also show each announcement as a Herdr notification? (reaches you over SSH)",
        config.get("toast").as_bool().unwrap_or(false),
    )?;
    config.set("toast", json!(toast));
    if changed {
        add_chosen(&mut chosen, "toast");
    }

    loop {
        let (debounce, explicit) = ui.text(
            "Ignore repeats within how many seconds?",
            &value_string(config.get("debounce_seconds")),
            None,
        )?;
        match debounce.parse::<i64>() {
            Ok(value) if value >= 0 => {
                config.set("debounce_seconds", json!(value));
                if explicit {
                    add_chosen(&mut chosen, "debounce_seconds");
                }
                break;
            }
            _ => ui.print_line("Please enter a non-negative integer.")?,
        }
    }

    let preview = config_lines(&config, &chosen)
        .map_err(|error| PromptError::Io(io::Error::new(io::ErrorKind::InvalidData, error)))?;
    ui.print_blank()?;
    ui.print_line(&format!("About to write {}:", config_path.display()))?;
    if preview.is_empty() {
        ui.print_line("  (empty file - everything matches the defaults)")?;
    } else {
        for line in preview {
            ui.print_line(&format!("  {}", preview_line(&line)))?;
        }
    }
    if existed {
        ui.print_line("Your current file will be kept as config.toml.bak.")?;
    }
    let (write_now, _) = ui.confirm("Write it?", true)?;
    if !write_now {
        ui.print_line("Nothing written.")?;
        return Ok(0);
    }
    let updates = chosen_updates(&config, &chosen);
    match write_setup_config(config_dir, &updates) {
        Ok(written) => *boundary = Some(written.boundary),
        Err(error) if error.kind() == io::ErrorKind::Interrupted => {
            return Err(PromptError::Abort);
        }
        Err(error) => return Err(PromptError::Io(error)),
    }

    let (test_voice, _) = ui.confirm("Test the voice now?", true)?;
    if test_voice {
        match fs::create_dir_all(state_dir) {
            Ok(()) => {
                let mut reasons = Vec::new();
                let result = load_config(config_dir, &mut reasons, None)
                    .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
                    .and_then(|config| {
                        speak(&config, "Announcer is configured", state_dir, &mut reasons)
                            .map_err(io::Error::other)
                    });
                match result {
                    Ok(backend) => ui.print_line(&format!("Spoke via: {backend}"))?,
                    Err(error) => ui.print_line(&format!("Voice test failed: {error}"))?,
                }
            }
            Err(error) => ui.print_line(&format!("Voice test failed: {error}"))?,
        }
    }
    ui.print_blank()?;
    ui.print_line("Done. Re-run this wizard anytime; the file is safe to hand-edit too.")?;
    Ok(0)
}

pub fn run_with_ui(
    ui: &mut dyn PromptUi,
    config_dir: &Path,
    state_dir: &Path,
    detected: &BTreeMap<String, bool>,
) -> Result<u8, PromptError> {
    let mut boundary = None;
    match run_wizard_flow(ui, config_dir, state_dir, &mut boundary, detected) {
        Ok(code) => Ok(code),
        Err(PromptError::Abort) => {
            let outcome = if let Some(boundary) = &boundary {
                rollback_setup_write(config_dir, boundary)?
            } else {
                RollbackOutcome::Restored
            };
            match outcome {
                RollbackOutcome::Restored => {
                    ui.print_line("\nsetup aborted, nothing written")?;
                }
                RollbackOutcome::ConcurrentChange => {
                    ui.print_line("\nsetup aborted; config changed concurrently and was kept")?;
                }
            }
            Ok(130)
        }
        Err(error) => Err(error),
    }
}

pub fn run_setup(config_dir: &Path, state_dir: &Path) -> Result<u8, String> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let detected = capabilities();
    let result = if stdin.is_terminal() && stdout.is_terminal() {
        let mut ui = TtyPrompter::new().map_err(|error| error.to_string())?;
        run_with_ui(&mut ui, config_dir, state_dir, &detected)
    } else {
        let secure_secret = stdin.is_terminal();
        let mut ui = LinePrompter::new(stdin.lock(), stdout.lock(), secure_secret);
        run_with_ui(&mut ui, config_dir, state_dir, &detected)
    };
    result.map_err(|error| error.to_string())
}

pub fn run_with_line_io<R: io::BufRead, W: io::Write>(
    input: R,
    output: W,
    config_dir: &Path,
    state_dir: &Path,
    detected: &BTreeMap<String, bool>,
) -> (Result<u8, PromptError>, R, W) {
    let mut ui = LinePrompter::new(input, output, false);
    let result = run_with_ui(&mut ui, config_dir, state_dir, detected);
    let (input, output) = ui.into_parts();
    (result, input, output)
}
