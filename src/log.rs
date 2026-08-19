use crate::atomicfile::write_text_atomic;
use crate::lockfile::with_flock;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

pub const LOG_MAX_BYTES: u64 = 512 * 1024;
pub const LOG_TAIL_BYTES: u64 = 256 * 1024;
pub const LOG_SCAN_BYTES: u64 = 65_536;
pub const LOG_SCAN_ENTRIES: usize = 500;
pub const RECENT_LINES: usize = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct LogEntry {
    pub timestamp: String,
    pub pane_id: String,
    pub status: String,
    pub action: String,
    pub elapsed: f64,
    pub reasons: Vec<String>,
    pub raw: String,
}

pub fn parse_log_line(line: &str) -> Option<LogEntry> {
    let raw = line.trim_end_matches('\n').to_owned();
    if raw.trim().is_empty() {
        return None;
    }
    let (head, reason_tail) = raw
        .split_once(" reasons=")
        .map_or((raw.as_str(), None), |(head, tail)| (head, Some(tail)));
    let mut reasons: Vec<String> = reason_tail
        .map(|tail| {
            tail.split(';')
                .map(str::trim)
                .filter(|part| !part.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default();
    let tokens: Vec<_> = head.split_whitespace().collect();
    if !tokens.iter().any(|token| token.starts_with("action=")) {
        return None;
    }
    let timestamp = tokens
        .first()
        .filter(|token| !token.contains('='))
        .copied()
        .unwrap_or("")
        .to_owned();
    let mut pane_id = "-".to_owned();
    let mut status = "-".to_owned();
    let mut action = String::new();
    let mut elapsed = 0.0;
    let start = usize::from(!timestamp.is_empty());
    for token in tokens.iter().skip(start) {
        let Some((key, value)) = token.split_once('=') else {
            continue;
        };
        match key {
            "pane_id" => pane_id = value.to_owned(),
            "status" => status = value.to_owned(),
            "action" => action = value.to_owned(),
            "elapsed" => elapsed = value.parse().unwrap_or(0.0),
            "reasons" if reasons.is_empty() => {
                reasons = value
                    .split(';')
                    .filter(|part| !part.is_empty())
                    .map(ToOwned::to_owned)
                    .collect();
            }
            _ => {}
        }
    }
    Some(LogEntry {
        timestamp,
        pane_id,
        status,
        action,
        elapsed,
        reasons,
        raw,
    })
}

pub fn read_log(path: &Path, limit: usize) -> Vec<LogEntry> {
    let Ok(mut file) = File::open(path) else {
        return Vec::new();
    };
    let Ok(size) = file.metadata().map(|metadata| metadata.len()) else {
        return Vec::new();
    };
    let start = size.saturating_sub(LOG_SCAN_BYTES);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return Vec::new();
    }
    let mut blob = Vec::new();
    if file.read_to_end(&mut blob).is_err() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&blob);
    let mut lines: Vec<_> = text.lines().collect();
    if start != 0 && !lines.is_empty() {
        lines.remove(0);
    }
    let mut entries = Vec::new();
    for line in lines {
        if let Some(entry) = parse_log_line(line) {
            entries.push(entry);
        }
        if entries.len() > LOG_SCAN_ENTRIES {
            entries.remove(0);
        }
    }
    if limit == 0 {
        return Vec::new();
    }
    entries.split_off(entries.len().saturating_sub(limit))
}

fn log_field(value: &str) -> String {
    let collapsed = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        "-".to_owned()
    } else {
        collapsed
    }
}

fn trim_log(path: &Path) -> io::Result<()> {
    let size = match fs::metadata(path) {
        Ok(metadata) => metadata.len(),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if size <= LOG_MAX_BYTES {
        return Ok(());
    }
    let mut file = File::open(path)?;
    let start = size.saturating_sub(LOG_TAIL_BYTES);
    file.seek(SeekFrom::Start(start))?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail)?;
    let retained = tail
        .iter()
        .position(|byte| *byte == b'\n')
        .map_or(&[][..], |newline| &tail[newline + 1..]);
    let text = String::from_utf8_lossy(retained);
    write_text_atomic(path, &text)
}

fn local_timestamp() -> String {
    jiff::Zoned::now()
        .strftime("%Y-%m-%dT%H:%M:%S%:z")
        .to_string()
}

pub fn log_invocation(
    state: &Path,
    pane: &str,
    status: &str,
    action: &str,
    elapsed: f64,
    trace: &str,
    reasons: &[String],
) -> io::Result<()> {
    fs::create_dir_all(state)?;
    let mut line = format!(
        "{} pane_id={} status={} action={} elapsed={elapsed:.3}",
        local_timestamp(),
        log_field(pane),
        log_field(status),
        log_field(action),
    );
    if !reasons.is_empty() {
        line.push_str(" reasons=");
        line.push_str(
            &reasons
                .iter()
                .map(|reason| log_field(reason))
                .collect::<Vec<_>>()
                .join(";"),
        );
    }
    line.push('\n');
    let lock = state.join("announcer-log.lock");
    let path = state.join("announcer.log");
    with_flock(&lock, || -> io::Result<()> {
        trim_log(&path)?;
        let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
        file.write_all(line.as_bytes())?;
        if !trace.is_empty() {
            file.write_all(trace.as_bytes())?;
            if !trace.ends_with('\n') {
                file.write_all(b"\n")?;
            }
        }
        Ok(())
    })?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(action: &str) -> String {
        format!(
            "2026-08-17T12:04:11+02:00 pane_id=pane-1 status=done action={action} elapsed=0.421"
        )
    }

    #[test]
    fn legacy_line_parses_every_field() {
        let parsed = parse_log_line(&line("announced")).unwrap();
        assert_eq!(parsed.timestamp, "2026-08-17T12:04:11+02:00");
        assert_eq!(parsed.pane_id, "pane-1");
        assert_eq!(parsed.status, "done");
        assert_eq!(parsed.action, "announced");
        assert_eq!(parsed.elapsed, 0.421);
        assert!(parsed.reasons.is_empty());
    }

    #[test]
    fn reasons_ignore_empty_segments() {
        let parsed =
            parse_log_line(&(line("snoozed") + " reasons=debounced;;quiet-hours")).unwrap();
        assert_eq!(parsed.reasons, ["debounced", "quiet-hours"]);
    }

    #[test]
    fn future_fields_are_ignored_and_missing_fields_default() {
        let parsed = parse_log_line("action=error elapsed=nope foo=bar").unwrap();
        assert_eq!(parsed.timestamp, "");
        assert_eq!(parsed.pane_id, "-");
        assert_eq!(parsed.status, "-");
        assert_eq!(parsed.action, "error");
        assert_eq!(parsed.elapsed, 0.0);
    }

    #[test]
    fn traceback_blank_and_actionless_lines_are_not_records() {
        for value in [
            "",
            "   \n",
            "Traceback (most recent call last):",
            "ValueError: boom",
            "2026-08-17T12:04:11+02:00 pane_id=p status=done",
        ] {
            assert!(parse_log_line(value).is_none(), "{value:?}");
        }
    }

    #[test]
    fn raw_keeps_original_line_without_newline() {
        let value = line("announced");
        assert_eq!(parse_log_line(&(value.clone() + "\n")).unwrap().raw, value);
    }

    #[test]
    fn read_log_is_oldest_first_and_keeps_newest_limit() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("announcer.log");
        let text = (0..10)
            .map(|index| line(&format!("a{index}")) + "\n")
            .collect::<String>();
        fs::write(&path, text).unwrap();
        let actions: Vec<_> = read_log(&path, 3)
            .into_iter()
            .map(|entry| entry.action)
            .collect();
        assert_eq!(actions, ["a7", "a8", "a9"]);
        assert!(read_log(&temp.path().join("missing"), 5).is_empty());
        assert!(read_log(temp.path(), 5).is_empty());
    }

    #[test]
    fn read_log_skips_tracebacks_and_leading_partial_tail() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("announcer.log");
        let huge = "x".repeat(LOG_SCAN_BYTES as usize + 4096) + " action=phantom\n";
        let tail = (0..3)
            .map(|index| line(&format!("tail{index}")) + "\n")
            .collect::<String>();
        fs::write(&path, huge + &tail).unwrap();
        let actions: Vec<_> = read_log(&path, 50)
            .into_iter()
            .map(|entry| entry.action)
            .collect();
        assert_eq!(actions, ["tail0", "tail1", "tail2"]);
    }

    #[test]
    fn log_fields_collapse_whitespace_and_reasons_follow_elapsed() {
        let temp = tempfile::tempdir().unwrap();
        log_invocation(
            temp.path(),
            "pane  1",
            "done",
            "template",
            1.25,
            "",
            &["codex:  failed".to_owned()],
        )
        .unwrap();
        let text = fs::read_to_string(temp.path().join("announcer.log")).unwrap();
        assert!(text.ends_with(
            " pane_id=pane 1 status=done action=template elapsed=1.250 reasons=codex: failed\n"
        ));
    }

    #[test]
    fn large_log_is_trimmed_before_append() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("announcer.log");
        fs::write(&path, b"old line\n".repeat(70_000)).unwrap();
        log_invocation(temp.path(), "p", "done", "new", 0.0, "", &[]).unwrap();
        assert!(fs::metadata(&path).unwrap().len() < LOG_TAIL_BYTES + 1024);
        assert!(
            fs::read(&path)
                .unwrap()
                .ends_with(b"action=new elapsed=0.000\n")
        );
    }
}
