use regex::Regex;
use std::collections::BTreeSet;
use std::env;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

const SENSITIVE_NAMES: [&str; 9] = [
    "api_key",
    "authorization",
    "auth_token",
    "access_token",
    "bearer_token",
    "password",
    "passwd",
    "secret",
    "token",
];
const SENSITIVE_SUFFIXES: [&str; 5] = ["_api_key", "_token", "_secret", "_password", "_passwd"];

fn header_secret_re() -> &'static Regex {
    static VALUE: OnceLock<Regex> = OnceLock::new();
    VALUE.get_or_init(|| {
        Regex::new(r"(?i)(\b(?:authorization|x-api-key|api-key)\s*:\s*(?:bearer\s+)?)([^\s,;]+)")
            .expect("valid header secret regex")
    })
}

fn query_secret_re() -> &'static Regex {
    static VALUE: OnceLock<Regex> = OnceLock::new();
    VALUE.get_or_init(|| {
        Regex::new(r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)=)([^&\s]+)")
            .expect("valid query secret regex")
    })
}

fn assignment_name_re() -> &'static Regex {
    static VALUE: OnceLock<Regex> = OnceLock::new();
    VALUE.get_or_init(|| Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*$").unwrap())
}

fn prefixed_credential_re() -> &'static Regex {
    static VALUE: OnceLock<Regex> = OnceLock::new();
    VALUE.get_or_init(|| {
        Regex::new(r"^(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_-]{8,}|xoxb-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16})$")
            .unwrap()
    })
}

fn jwt_re() -> &'static Regex {
    static VALUE: OnceLock<Regex> = OnceLock::new();
    VALUE.get_or_init(|| {
        Regex::new(r"^[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}$").unwrap()
    })
}

fn bearer_re() -> &'static Regex {
    static VALUE: OnceLock<Regex> = OnceLock::new();
    VALUE.get_or_init(|| Regex::new(r"(?i)^(bearer\s+)(\S+)$").unwrap())
}

pub fn mask_secret(value: &str) -> String {
    if value.is_empty() {
        String::new()
    } else if value.chars().count() > 4 {
        let tail: String = value
            .chars()
            .rev()
            .take(4)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        format!("****{tail}")
    } else {
        "****".to_owned()
    }
}

fn is_sensitive_name(value: &str) -> bool {
    let normalized = value
        .trim_start_matches('-')
        .to_lowercase()
        .replace('-', "_");
    SENSITIVE_NAMES.contains(&normalized.as_str())
        || SENSITIVE_SUFFIXES
            .iter()
            .any(|suffix| normalized.ends_with(suffix))
}

fn replace_embedded_regex(regex: &Regex, value: &str, secrets: &mut Vec<String>) -> String {
    let mut output = String::new();
    let mut end = 0;
    for captures in regex.captures_iter(value) {
        let whole = captures.get(0).expect("capture zero");
        let prefix = captures.get(1).expect("capture prefix");
        let secret = captures.get(2).expect("capture secret").as_str();
        output.push_str(&value[end..whole.start()]);
        output.push_str(prefix.as_str());
        output.push_str(&mask_secret(secret));
        secrets.push(secret.to_owned());
        end = whole.end();
    }
    output.push_str(&value[end..]);
    output
}

fn redact_embedded(value: &str) -> (String, Vec<String>) {
    let mut secrets = Vec::new();
    let redacted = replace_embedded_regex(header_secret_re(), value, &mut secrets);
    let redacted = replace_embedded_regex(query_secret_re(), &redacted, &mut secrets);
    (redacted, secrets)
}

fn expand_home(value: &str) -> PathBuf {
    if value == "~" {
        return env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(value));
    }
    if let Some(rest) = value.strip_prefix("~/")
        && let Some(home) = env::var_os("HOME")
    {
        return PathBuf::from(home).join(rest);
    }
    PathBuf::from(value)
}

fn existing_path(value: &str) -> bool {
    let path = expand_home(value);
    Path::new(&path).exists()
}

fn looks_high_entropy(value: &str) -> bool {
    value.chars().count() >= 20
        && !value.chars().any(char::is_whitespace)
        && value.chars().any(char::is_lowercase)
        && value.chars().any(char::is_uppercase)
        && value.chars().any(char::is_numeric)
}

fn redact_value(value: &str) -> (String, Vec<String>) {
    if existing_path(value) {
        return (value.to_owned(), Vec::new());
    }
    let (embedded, secrets) = redact_embedded(value);
    if !secrets.is_empty() {
        return (embedded, secrets);
    }
    if let Some(captures) = bearer_re().captures(value) {
        let secret = captures.get(2).unwrap().as_str();
        return (
            format!(
                "{}{}",
                captures.get(1).unwrap().as_str(),
                mask_secret(secret)
            ),
            vec![secret.to_owned()],
        );
    }
    if value.chars().any(char::is_whitespace) {
        return (value.to_owned(), Vec::new());
    }
    if prefixed_credential_re().is_match(value)
        || jwt_re().is_match(value)
        || looks_high_entropy(value)
    {
        return (mask_secret(value), vec![value.to_owned()]);
    }
    (value.to_owned(), Vec::new())
}

fn sensitive_option_prefixes() -> &'static Vec<String> {
    static VALUE: OnceLock<Vec<String>> = OnceLock::new();
    VALUE.get_or_init(|| {
        let mut values: Vec<_> = SENSITIVE_NAMES
            .iter()
            .flat_map(|name| {
                [
                    format!("--{}", name.replace('_', "-")),
                    format!("--{}", name),
                ]
            })
            .collect();
        values.sort_by_key(|value| std::cmp::Reverse(value.len()));
        values.dedup();
        values
    })
}

fn attached_option_value(argument: &str) -> (&str, &str) {
    if argument.starts_with('-') && !argument.starts_with("--") && argument.len() > 2 {
        return argument.split_at(2);
    }
    for prefix in sensitive_option_prefixes() {
        if argument.starts_with(prefix) && argument.len() > prefix.len() {
            return argument.split_at(prefix.len());
        }
    }
    ("", "")
}

fn redact_command_with_secrets(command: &[String]) -> (Vec<String>, Vec<String>) {
    let mut redacted = Vec::with_capacity(command.len());
    let mut secrets = Vec::new();
    let mut mask_next = false;
    for argument in command {
        if mask_next {
            redacted.push(mask_secret(argument));
            secrets.push(argument.clone());
            mask_next = false;
            continue;
        }
        if let Some((name, value)) = argument.split_once('=')
            && (name.starts_with('-') || assignment_name_re().is_match(name))
        {
            let (rendered, found) = if is_sensitive_name(name) {
                (mask_secret(value), vec![value.to_owned()])
            } else {
                redact_value(value)
            };
            redacted.push(format!("{name}={rendered}"));
            secrets.extend(found);
            continue;
        }
        if argument.starts_with('-') {
            let (prefix, value) = attached_option_value(argument);
            if !value.is_empty() {
                let (rendered, found) = if prefix == "-p" || is_sensitive_name(prefix) {
                    (mask_secret(value), vec![value.to_owned()])
                } else {
                    redact_value(value)
                };
                redacted.push(format!("{prefix}{rendered}"));
                secrets.extend(found);
                continue;
            }
            redacted.push(argument.clone());
            if is_sensitive_name(argument) {
                mask_next = true;
            }
            continue;
        }
        let (rendered, found) = redact_value(argument);
        redacted.push(rendered);
        secrets.extend(found);
    }
    (redacted, secrets)
}

pub fn redact_command(argv: &[String]) -> Vec<String> {
    redact_command_with_secrets(argv).0
}

pub fn redact_command_text(text: &str, argv: &[String]) -> String {
    let (_, secrets) = redact_command_with_secrets(argv);
    let mut unique: Vec<_> = BTreeSet::from_iter(secrets).into_iter().collect();
    unique.sort_by_key(|secret| std::cmp::Reverse(secret.len()));
    let mut redacted = text.to_owned();
    for secret in unique {
        if !secret.is_empty() {
            redacted = redacted.replace(&secret, &mask_secret(&secret));
        }
    }
    redact_embedded(&redacted).0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existing_high_entropy_path_is_visible() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("AbCdEfGhIjKlMnOp1234");
        std::fs::write(&path, "").unwrap();
        let command = vec![path.to_string_lossy().into_owned()];
        assert_eq!(redact_command(&command), command);
    }

    #[test]
    fn prose_with_whitespace_is_visible() {
        let prose = "One spoken sentence, maximum 25 words, plain words only".to_owned();
        let command = vec!["helper".to_owned(), prose.clone()];
        assert_eq!(redact_command(&command), command);
    }
}
