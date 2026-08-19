use crate::atomicfile::write_json_atomic;
use crate::config::{Config, load_config};
use crate::debounce::{reserve_debounce, rollback_debounce};
use crate::event::{event_payload, parse_event, payload_string};
use crate::ipc::Client;
use crate::mute::{is_pane_muted, remove_pane_mute, remove_workspace_mutes};
use crate::snooze::read_snooze_now;
use crate::speech::{SpeakError, SpeechOptions, speak_with_options};
pub use crate::summarize::{make_announcement, sanitize_summary, template_summary};
use serde_json::{Value, json};
use std::io;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Debug, PartialEq)]
pub struct LogContext {
    pub pane_id: String,
    pub status: String,
}

impl LogContext {
    pub fn hook() -> Self {
        Self {
            pane_id: "-".to_owned(),
            status: "-".to_owned(),
        }
    }

    pub fn test() -> Self {
        Self {
            pane_id: "-".to_owned(),
            status: "test".to_owned(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct PipelineOptions {
    pub client: Option<Client>,
    pub speech: SpeechOptions,
}

impl PipelineOptions {
    pub fn from_environment() -> Self {
        Self {
            client: Client::from_env().ok(),
            speech: SpeechOptions::default(),
        }
    }
}

fn push_missing_socket(reasons: &mut Vec<String>) {
    if !reasons
        .iter()
        .any(|reason| reason == "herdr: no socket path")
    {
        reasons.push("herdr: no socket path".to_owned());
    }
}

fn short_error(error: &dyn std::error::Error) -> String {
    let detail = error.to_string();
    let detail = detail.trim();
    if detail.is_empty() {
        "io error".to_owned()
    } else {
        detail.chars().take(120).collect()
    }
}

fn workspace_label(
    client: Option<&Client>,
    workspace_id: Option<&str>,
    reasons: &mut Vec<String>,
) -> String {
    let Some(workspace_id) = workspace_id else {
        return String::new();
    };
    let Some(client) = client else {
        push_missing_socket(reasons);
        return String::new();
    };
    match client.workspace_label(workspace_id) {
        Ok(Some(label)) => label,
        Ok(None) => {
            reasons.push("herdr: workspace-not-found".to_owned());
            String::new()
        }
        Err(error) => {
            reasons.push(format!("herdr-workspace: {}", short_error(&error)));
            String::new()
        }
    }
}

fn transcript(client: Option<&Client>, pane_id: &str, reasons: &mut Vec<String>) -> String {
    let Some(client) = client else {
        push_missing_socket(reasons);
        return String::new();
    };
    match client.pane_read(pane_id) {
        Ok(text) => text,
        Err(error) => {
            reasons.push(format!("herdr-read: {}", short_error(&error)));
            String::new()
        }
    }
}

fn toast(client: Option<&Client>, text: &str, reasons: &mut Vec<String>) {
    let Some(client) = client else {
        push_missing_socket(reasons);
        return;
    };
    if let Err(error) = client.notification_show(text) {
        reasons.push(format!("toast: {}", short_error(&error)));
    }
}

fn configured_statuses(config: &Config) -> Result<Vec<String>, String> {
    let values = config
        .get("announce")
        .as_array()
        .ok_or_else(|| "announce must be an array of strings".to_owned())?;
    Ok(values
        .iter()
        .filter_map(Value::as_str)
        .map(str::to_lowercase)
        .collect())
}

fn debounce_seconds(config: &Config) -> Result<i64, String> {
    config
        .get("debounce_seconds")
        .as_i64()
        .ok_or_else(|| "debounce_seconds must be an integer".to_owned())
}

fn name_from_payload(payload: &Value) -> &str {
    payload_string(payload, "display_agent")
        .or_else(|| payload_string(payload, "agent"))
        .unwrap_or("an agent")
}

fn wall_time() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

fn agent_is_muted(config: &Config, payload: &Value) -> bool {
    let muted: Vec<String> = config
        .get("mute_agents")
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_lowercase)
        .collect();
    ["agent", "display_agent"].into_iter().any(|key| {
        payload_string(payload, key)
            .map(str::to_lowercase)
            .is_some_and(|agent| muted.contains(&agent))
    })
}

fn detected_event(payload: &Value, raw_event: &str) -> bool {
    payload_string(payload, "type") == Some("pane_agent_detected")
        || serde_json::from_str::<Value>(raw_event)
            .ok()
            .and_then(|value| {
                value
                    .get("event")
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned)
            })
            .as_deref()
            == Some("pane_agent_detected")
}

fn finish_speech(
    config: &Config,
    state_dir: &Path,
    announcement: &str,
    summary_backend: &str,
    reasons: &mut Vec<String>,
    options: &PipelineOptions,
) -> Result<String, String> {
    if config.is_truthy("toast") {
        toast(options.client.as_ref(), announcement, reasons);
    }
    match speak_with_options(config, announcement, state_dir, reasons, options.speech) {
        Ok(backend) => Ok(format!(
            "announced+summary-{summary_backend}+speak-{backend}"
        )),
        Err(error) if error.is_playback_lock_timeout() => Ok("gave-up-waiting".to_owned()),
        Err(error) => Err(error.to_string()),
    }
}

fn process_detected(
    config: &Config,
    state_dir: &Path,
    payload: &Value,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
    options: &PipelineOptions,
) -> Result<String, String> {
    if !config.is_truthy("announce_on_detect")
        || payload.get("released").and_then(Value::as_bool) == Some(true)
    {
        return Ok(String::new());
    }
    let pane_id = payload_string(payload, "pane_id")
        .ok_or_else(|| "event payload has no string pane_id".to_owned())?;
    log_context.pane_id = pane_id.to_owned();
    log_context.status = "detected".to_owned();

    if agent_is_muted(config, payload) {
        return Ok("muted-agent".to_owned());
    }
    if is_pane_muted(state_dir, pane_id, wall_time()) {
        return Ok("muted-pane".to_owned());
    }
    if read_snooze_now(state_dir) > 0.0 {
        return Ok("snoozed".to_owned());
    }

    let seconds = debounce_seconds(config)?;
    let (debounced, reservation) = reserve_debounce(state_dir, pane_id, "detected", seconds)
        .map_err(|error| error.to_string())?;
    if debounced {
        return Ok("debounced".to_owned());
    }

    let name = name_from_payload(payload);
    let workspace = workspace_label(
        options.client.as_ref(),
        payload_string(payload, "workspace_id"),
        reasons,
    );
    let location = if workspace.is_empty() {
        String::new()
    } else {
        format!(" in {workspace}")
    };
    let announcement = sanitize_summary(&format!("{name} agent detected{location}."));
    let attempt = finish_speech(
        config,
        state_dir,
        &announcement,
        "template",
        reasons,
        options,
    );
    if attempt.as_ref().is_err() || attempt.as_deref() == Ok("gave-up-waiting") {
        rollback_debounce(state_dir, pane_id, "detected", reservation)
            .map_err(|error| error.to_string())?;
    }
    attempt
}

fn map_speak_error(error: SpeakError) -> Result<String, String> {
    if error.is_playback_lock_timeout() {
        Ok("gave-up-waiting".to_owned())
    } else {
        Err(error.to_string())
    }
}

pub fn process_invocation_with_options(
    config_dir: &Path,
    state_dir: &Path,
    test_mode: bool,
    raw_event: Option<&str>,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
    options: &PipelineOptions,
) -> Result<String, String> {
    let config = load_config(config_dir, reasons, None)?;
    if test_mode {
        let text = "Announcer is working";
        if config.is_truthy("toast") {
            toast(options.client.as_ref(), text, reasons);
        }
        return speak_with_options(&config, text, state_dir, reasons, options.speech)
            .map(|backend| format!("announced+{backend}"))
            .or_else(map_speak_error);
    }

    let raw_event = raw_event.ok_or_else(|| "event payload is missing".to_owned())?;
    let payload = event_payload(raw_event)?;
    if detected_event(&payload, raw_event) {
        return process_detected(&config, state_dir, &payload, log_context, reasons, options);
    }
    let (pane_id, status) = parse_event(raw_event)?;
    log_context.pane_id.clone_from(&pane_id);
    log_context.status.clone_from(&status);

    if !configured_statuses(&config)?.contains(&status) {
        return Ok("skipped-status".to_owned());
    }

    if agent_is_muted(&config, &payload) {
        return Ok("muted-agent".to_owned());
    }
    if is_pane_muted(state_dir, &pane_id, wall_time()) {
        return Ok("muted-pane".to_owned());
    }
    if read_snooze_now(state_dir) > 0.0 {
        return Ok("snoozed".to_owned());
    }

    let seconds = debounce_seconds(&config)?;
    let (debounced, reservation) =
        reserve_debounce(state_dir, &pane_id, &status, seconds).map_err(|e| e.to_string())?;
    if debounced {
        return Ok("debounced".to_owned());
    }

    let attempt: Result<String, String> = {
        let name = name_from_payload(&payload);
        let workspace = workspace_label(
            options.client.as_ref(),
            payload_string(&payload, "workspace_id"),
            reasons,
        );
        let transcript = transcript(options.client.as_ref(), &pane_id, reasons);
        let (generated, summary_backend) =
            make_announcement(&config, name, &workspace, &status, &transcript, reasons);
        let announcement = sanitize_summary(&generated);
        finish_speech(
            &config,
            state_dir,
            &announcement,
            summary_backend,
            reasons,
            options,
        )
    };

    let should_rollback = match &attempt {
        Ok(action) => action == "gave-up-waiting",
        Err(_) => true,
    };
    if should_rollback {
        rollback_debounce(state_dir, &pane_id, &status, reservation)
            .map_err(|error| error.to_string())?;
    }
    attempt
}

pub fn process_cleanup_with_options(
    state_dir: &Path,
    raw_event: &str,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
    client: Option<&Client>,
) -> Result<Option<String>, String> {
    let payload = event_payload(raw_event)?;
    // workspace.closed carries no pane_id; pane ids embed the workspace prefix,
    // and closing a workspace emits no pane.closed hooks on herdr 0.8.0.
    if payload_string(&payload, "type") == Some("workspace_closed") {
        let workspace_id = payload_string(&payload, "workspace_id")
            .ok_or_else(|| "event payload has no string workspace_id".to_owned())?;
        let removed = remove_workspace_mutes(state_dir, workspace_id);
        if removed.is_empty() {
            return Ok(None);
        }
        log_context.pane_id = removed.join(",");
        return Ok(Some("cleanup".to_owned()));
    }
    let pane_id = payload_string(&payload, "pane_id")
        .ok_or_else(|| "event payload has no string pane_id".to_owned())?;
    if !remove_pane_mute(state_dir, pane_id) {
        return Ok(None);
    }
    log_context.pane_id = pane_id.to_owned();
    if let Some(client) = client
        && let Err(error) = client.report_muted(pane_id, false)
    {
        reasons.push(format!("herdr-metadata: {}", short_error(&error)));
    }
    Ok(Some("cleanup".to_owned()))
}

pub fn process_cleanup(
    state_dir: &Path,
    raw_event: &str,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
) -> Result<Option<String>, String> {
    let client = Client::from_env().ok();
    process_cleanup_with_options(state_dir, raw_event, log_context, reasons, client.as_ref())
}

pub fn process_invocation(
    config_dir: &Path,
    state_dir: &Path,
    test_mode: bool,
    raw_event: Option<&str>,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
) -> Result<String, String> {
    process_invocation_with_options(
        config_dir,
        state_dir,
        test_mode,
        raw_event,
        log_context,
        reasons,
        &PipelineOptions::from_environment(),
    )
}

pub fn persist_last_error(state_dir: &Path, reasons: &[String]) -> io::Result<()> {
    if reasons.is_empty() {
        return Ok(());
    }
    let timestamp = jiff::Zoned::now()
        .strftime("%Y-%m-%dT%H:%M:%S%:z")
        .to_string();
    write_json_atomic(
        &state_dir.join("last-error.json"),
        &json!({"timestamp": timestamp, "reasons": reasons}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizer_matches_unicode_and_injection_vectors() {
        for (source, expected) in [
            ("$(curl evil | sh)", "curl evil sh"),
            ("`touch bad`", "touch bad"),
            ("; rm -rf /", "rm -rf"),
            ("agent's work", "agent s work"),
            (
                "Élodie 完成 １２, déjà-vu! Really? yes.",
                "Élodie 完成 １２, déjà-vu! Really? yes.",
            ),
        ] {
            assert_eq!(sanitize_summary(source), expected);
        }
        let words = (0..50)
            .map(|index| format!("word{index}"))
            .collect::<Vec<_>>()
            .join(" \t\n ");
        assert_eq!(sanitize_summary(&words).split_whitespace().count(), 40);
    }

    #[test]
    fn template_wording_is_exact() {
        assert_eq!(
            template_summary("builder", "billing", "done"),
            "builder finished in billing."
        );
        assert_eq!(
            template_summary("builder", "", "blocked"),
            "builder needs your input."
        );
        assert_eq!(
            template_summary("builder", "work", "idle"),
            "builder is now idle in work."
        );
    }

    #[test]
    fn template_mode_ignores_codex_fallback_and_empty_transcripts_skip_placeholders() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(
            temp.path().join("config.toml"),
            "summary = \"template\"\nsummary_fallback = \"codex\"\n",
        )
        .unwrap();
        let config = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        let mut reasons = Vec::new();
        assert_eq!(
            make_announcement(
                &config,
                "builder",
                "billing",
                "done",
                "transcript",
                &mut reasons
            ),
            ("builder finished in billing.".to_owned(), "template")
        );
        assert!(reasons.is_empty());

        std::fs::write(temp.path().join("config.toml"), "summary = \"codex\"\n").unwrap();
        let config = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        make_announcement(&config, "builder", "", "done", "", &mut reasons);
        assert!(reasons.is_empty());
    }
}
