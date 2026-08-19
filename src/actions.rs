use crate::config::{Config, load_config};
use crate::hook::LogContext;
use crate::ipc::Client;
use crate::mute::{list_pane_mutes, remove_pane_mute, set_pane_mute};
use crate::snooze::format_snooze_remaining;
use crate::speech::{SpeechOptions, speak_with_options};
use crate::summarize::{make_announcement, sanitize_summary};
use serde_json::Value;
use std::env;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ActionContext {
    pub workspace_id: Option<String>,
    pub workspace_label: Option<String>,
    pub focused_pane_id: Option<String>,
    pub focused_pane_agent: Option<String>,
    pub focused_pane_status: Option<String>,
}

fn optional_string(value: &Value, key: &str) -> Option<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

pub fn parse_action_context(raw: Option<&str>) -> Result<ActionContext, String> {
    let Some(raw) = raw.filter(|raw| !raw.trim().is_empty()) else {
        return Ok(ActionContext::default());
    };
    let value: Value = serde_json::from_str(raw).map_err(|error| error.to_string())?;
    Ok(ActionContext {
        workspace_id: optional_string(&value, "workspace_id"),
        workspace_label: optional_string(&value, "workspace_label"),
        focused_pane_id: optional_string(&value, "focused_pane_id"),
        focused_pane_agent: optional_string(&value, "focused_pane_agent"),
        focused_pane_status: optional_string(&value, "focused_pane_status"),
    })
}

#[derive(Clone, Debug)]
pub struct ActionOptions {
    pub client: Option<Client>,
    pub caller_pane_id: Option<String>,
    pub speech: SpeechOptions,
    pub now: f64,
}

fn wall_time() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

impl ActionOptions {
    pub fn from_environment() -> Self {
        Self {
            client: Client::from_env().ok(),
            caller_pane_id: env::var("HERDR_PANE_ID")
                .ok()
                .filter(|value| !value.is_empty()),
            speech: SpeechOptions::default(),
            now: wall_time(),
        }
    }
}

struct Target {
    pane_id: String,
    current: Option<Value>,
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

fn toast(client: Option<&Client>, text: &str, reasons: &mut Vec<String>) {
    let Some(client) = client else {
        if !reasons
            .iter()
            .any(|reason| reason == "herdr: no socket path")
        {
            reasons.push("herdr: no socket path".to_owned());
        }
        return;
    };
    if let Err(error) = client.notification_show(text) {
        reasons.push(format!("toast: {}", short_error(&error)));
    }
}

fn report_muted(client: Option<&Client>, pane_id: &str, muted: bool, reasons: &mut Vec<String>) {
    if let Some(client) = client
        && let Err(error) = client.report_muted(pane_id, muted)
    {
        reasons.push(format!("herdr-metadata: {}", short_error(&error)));
    }
}

fn resolve_target(
    context: &ActionContext,
    options: &ActionOptions,
    reasons: &mut Vec<String>,
) -> Result<Target, String> {
    if let Some(pane_id) = &context.focused_pane_id {
        return Ok(Target {
            pane_id: pane_id.clone(),
            current: None,
        });
    }
    if let (Some(client), Some(caller)) = (&options.client, &options.caller_pane_id)
        && let Ok(pane) = client.pane_current(caller)
        && let Some(pane_id) = optional_string(&pane, "pane_id")
    {
        return Ok(Target {
            pane_id,
            current: Some(pane),
        });
    }
    reasons.push("action: no target pane".to_owned());
    toast(
        options.client.as_ref(),
        "Announcer: no target pane",
        reasons,
    );
    Err("no target pane".to_owned())
}

fn status_for_log(context: &ActionContext, pane: Option<&Value>) -> String {
    context
        .focused_pane_status
        .clone()
        .or_else(|| pane.and_then(|pane| optional_string(pane, "agent_status")))
        .unwrap_or_else(|| "-".to_owned())
}

fn cycle_step(until: f64, now: f64) -> Option<f64> {
    if until == 0.0 || !until.is_finite() || until <= now {
        return Some(now + 300.0);
    }
    let remaining = until - now;
    if remaining <= 300.0 {
        Some(now + 1800.0)
    } else if remaining <= 1800.0 {
        Some(now + 7200.0)
    } else {
        None
    }
}

fn pane_value<'a>(pane: &'a Value, keys: &[&str]) -> Option<&'a str> {
    keys.iter().find_map(|key| {
        pane.get(*key)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
    })
}

fn process_announce_now(
    config: &Config,
    state_dir: &Path,
    context: &ActionContext,
    target: &Target,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
    options: &ActionOptions,
) -> Result<String, String> {
    reasons.push("manual".to_owned());
    let needs_pane = context.focused_pane_status.is_none()
        || context.focused_pane_agent.is_none()
        || (context.workspace_label.is_none() && context.workspace_id.is_none());
    let fetched = if needs_pane {
        if let Some(client) = &options.client {
            match client.pane_get(&target.pane_id) {
                Ok(pane) => Some(pane),
                Err(error) => {
                    reasons.push(format!("herdr-pane: {}", short_error(&error)));
                    None
                }
            }
        } else {
            None
        }
    } else {
        None
    };
    let pane = fetched.as_ref().or(target.current.as_ref());
    let status = context
        .focused_pane_status
        .as_deref()
        .or_else(|| pane.and_then(|pane| pane_value(pane, &["agent_status"])))
        .unwrap_or("unknown");
    let name = context
        .focused_pane_agent
        .as_deref()
        .or_else(|| pane.and_then(|pane| pane_value(pane, &["display_agent", "agent"])))
        .unwrap_or("an agent");
    let workspace_id = context
        .workspace_id
        .as_deref()
        .or_else(|| pane.and_then(|pane| pane_value(pane, &["workspace_id"])));
    let workspace = if let Some(label) = &context.workspace_label {
        label.clone()
    } else if let (Some(client), Some(workspace_id)) = (&options.client, workspace_id) {
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
    } else {
        String::new()
    };
    let transcript = if let Some(client) = &options.client {
        match client.pane_read(&target.pane_id) {
            Ok(text) => text,
            Err(error) => {
                reasons.push(format!("herdr-read: {}", short_error(&error)));
                String::new()
            }
        }
    } else {
        String::new()
    };
    let (generated, summary_backend) =
        make_announcement(config, name, &workspace, status, &transcript, reasons);
    let announcement = sanitize_summary(&generated);
    if config.is_truthy("toast") {
        toast(options.client.as_ref(), &announcement, reasons);
    }
    log_context.status = status.to_owned();
    match speak_with_options(config, &announcement, state_dir, reasons, options.speech) {
        Ok(backend) => Ok(format!(
            "announced+summary-{summary_backend}+speak-{backend}"
        )),
        Err(error) if error.is_playback_lock_timeout() => Ok("gave-up-waiting".to_owned()),
        Err(error) => Err(error.to_string()),
    }
}

pub fn process_action_with_options(
    config_dir: &Path,
    state_dir: &Path,
    action_id: &str,
    raw_context: Option<&str>,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
    options: &ActionOptions,
) -> Result<String, String> {
    let config = load_config(config_dir, reasons, None)?;
    let context = parse_action_context(raw_context)?;
    let target = resolve_target(&context, options, reasons)?;
    log_context.pane_id.clone_from(&target.pane_id);
    log_context.status = status_for_log(&context, target.current.as_ref());

    match action_id {
        "mute-pane" => {
            if list_pane_mutes(state_dir, options.now).contains_key(&target.pane_id) {
                remove_pane_mute(state_dir, &target.pane_id);
                report_muted(options.client.as_ref(), &target.pane_id, false, reasons);
                toast(options.client.as_ref(), "Announcer: pane unmuted", reasons);
            } else {
                set_pane_mute(
                    state_dir,
                    &target.pane_id,
                    0.0,
                    context.focused_pane_agent.as_deref().unwrap_or(""),
                    options.now,
                )
                .map_err(|error| error.to_string())?;
                report_muted(options.client.as_ref(), &target.pane_id, true, reasons);
                toast(
                    options.client.as_ref(),
                    "Announcer: pane muted until it closes",
                    reasons,
                );
            }
            Ok("mute-pane".to_owned())
        }
        "snooze-pane" => {
            let until = list_pane_mutes(state_dir, options.now)
                .get(&target.pane_id)
                .map_or(0.0, |entry| entry.until);
            if let Some(next) = cycle_step(until, options.now) {
                set_pane_mute(
                    state_dir,
                    &target.pane_id,
                    next,
                    context.focused_pane_agent.as_deref().unwrap_or(""),
                    options.now,
                )
                .map_err(|error| error.to_string())?;
                report_muted(options.client.as_ref(), &target.pane_id, true, reasons);
                toast(
                    options.client.as_ref(),
                    &format!(
                        "Announcer: pane snoozed · {}",
                        format_snooze_remaining(next, options.now)
                    ),
                    reasons,
                );
            } else {
                remove_pane_mute(state_dir, &target.pane_id);
                report_muted(options.client.as_ref(), &target.pane_id, false, reasons);
                toast(
                    options.client.as_ref(),
                    "Announcer: pane snooze off",
                    reasons,
                );
            }
            Ok("snooze-pane".to_owned())
        }
        "announce-now" => process_announce_now(
            &config,
            state_dir,
            &context,
            &target,
            log_context,
            reasons,
            options,
        ),
        _ => Err(format!("unknown action: {action_id}")),
    }
}

pub fn process_action(
    config_dir: &Path,
    state_dir: &Path,
    action_id: &str,
    raw_context: Option<&str>,
    log_context: &mut LogContext,
    reasons: &mut Vec<String>,
) -> Result<String, String> {
    process_action_with_options(
        config_dir,
        state_dir,
        action_id,
        raw_context,
        log_context,
        reasons,
        &ActionOptions::from_environment(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phase_zero_context_fixture_parses_focused_pane_fields() {
        let fixture: Value =
            serde_json::from_str(include_str!("../tests/fixtures/action-pane-context.json"))
                .unwrap();
        let raw = serde_json::to_string(&fixture["context_json"]).unwrap();
        let context = parse_action_context(Some(&raw)).unwrap();
        assert_eq!(context.focused_pane_id.as_deref(), Some("w7:p1"));
        assert_eq!(
            context.workspace_label.as_deref(),
            Some("contract-probe-phase0")
        );
        assert_eq!(context.focused_pane_status.as_deref(), Some("unknown"));
        assert_eq!(context.focused_pane_agent, None);
    }

    #[test]
    fn pane_snooze_cycle_is_five_minutes_thirty_minutes_two_hours_off() {
        let now = 1000.0;
        assert_eq!(cycle_step(0.0, now), Some(1300.0));
        assert_eq!(cycle_step(1300.0, now), Some(2800.0));
        assert_eq!(cycle_step(2800.0, now), Some(8200.0));
        assert_eq!(cycle_step(8200.0, now), None);
    }
}
