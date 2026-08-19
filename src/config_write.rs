use crate::atomicfile::write_text_atomic;
use crate::config::{Config, load_config};
use crate::lockfile::with_flock;
use serde_json::Value;
use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use toml_edit::{Array, DocumentMut, Item};

fn json_value(value: &Value) -> io::Result<toml_edit::Value> {
    match value {
        Value::Bool(value) => Ok((*value).into()),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                Ok(value.into())
            } else if let Some(value) = value.as_f64() {
                Ok(value.into())
            } else {
                Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "unsupported JSON number",
                ))
            }
        }
        Value::String(value) => Ok(value.as_str().into()),
        Value::Array(values) => {
            let mut array = Array::new();
            for value in values {
                array.push(json_value(value)?);
            }
            Ok(array.into())
        }
        Value::Null | Value::Object(_) => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "unsupported config value",
        )),
    }
}

fn write_locked(dir: &Path, updates: &[(&str, Value)]) -> io::Result<Config> {
    let path = dir.join("config.toml");
    let old = match fs::read_to_string(&path) {
        Ok(text) => Some(text),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    let mut document = old
        .as_deref()
        .unwrap_or("")
        .parse::<DocumentMut>()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    for (key, value) in updates {
        if value.is_null() {
            document.remove(key);
        } else {
            let mut replacement = json_value(value)?;
            if let Some(existing) = document.get(key).and_then(Item::as_value) {
                *replacement.decor_mut() = existing.decor().clone();
            }
            document[key] = Item::Value(replacement);
        }
    }
    if let Some(old) = old {
        write_text_atomic(&dir.join("config.toml.bak"), &old)?;
    }
    write_text_atomic(&path, &document.to_string())?;
    load_config(dir, &mut Vec::new(), None)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

pub fn write_config_keys(dir: &Path, updates: &[(&str, Value)]) -> io::Result<Config> {
    fs::create_dir_all(dir)?;
    with_flock(&dir.join("config.toml.lock"), || write_locked(dir, updates))?
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FileSnapshot {
    pub bytes: Option<Vec<u8>>,
    pub mode: Option<u32>,
}

#[derive(Clone, Debug)]
pub struct SetupWriteBoundary {
    pub before_config: FileSnapshot,
    pub before_backup: FileSnapshot,
    pub after_config: FileSnapshot,
    pub after_backup: FileSnapshot,
}

pub struct SetupWriteResult {
    pub config: Config,
    pub boundary: SetupWriteBoundary,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RollbackOutcome {
    Restored,
    ConcurrentChange,
}

fn snapshot(path: &Path) -> io::Result<FileSnapshot> {
    match fs::read(path) {
        Ok(bytes) => Ok(FileSnapshot {
            bytes: Some(bytes),
            mode: Some(fs::metadata(path)?.permissions().mode() & 0o7777),
        }),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(FileSnapshot {
            bytes: None,
            mode: None,
        }),
        Err(error) => Err(error),
    }
}

fn restore(path: &Path, state: &FileSnapshot) -> io::Result<()> {
    let Some(bytes) = &state.bytes else {
        return match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        };
    };
    if snapshot(path)? == *state {
        return Ok(());
    }
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::Builder::new()
        .prefix(&format!(
            "{}.restore.",
            path.file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("config")
        ))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    temporary.write_all(bytes)?;
    temporary.flush()?;
    if let Some(mode) = state.mode {
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(mode))?;
    }
    temporary
        .persist(path)
        .map(|_| ())
        .map_err(|error| error.error)
}

fn pair(dir: &Path) -> io::Result<(FileSnapshot, FileSnapshot)> {
    Ok((
        snapshot(&dir.join("config.toml"))?,
        snapshot(&dir.join("config.toml.bak"))?,
    ))
}

fn restore_pair(dir: &Path, config: &FileSnapshot, backup: &FileSnapshot) -> io::Result<()> {
    restore(&dir.join("config.toml"), config)?;
    restore(&dir.join("config.toml.bak"), backup)
}

fn write_setup_config_inner(
    dir: &Path,
    updates: &[(&str, Value)],
    after_write: impl FnOnce() -> io::Result<()>,
) -> io::Result<SetupWriteResult> {
    fs::create_dir_all(dir)?;
    with_flock(&dir.join("config.toml.lock"), || {
        let (before_config, before_backup) = pair(dir)?;
        let attempt = (|| {
            let config = write_locked(dir, updates)?;
            after_write()?;
            let (after_config, after_backup) = pair(dir)?;
            Ok(SetupWriteResult {
                config,
                boundary: SetupWriteBoundary {
                    before_config: before_config.clone(),
                    before_backup: before_backup.clone(),
                    after_config,
                    after_backup,
                },
            })
        })();
        if attempt.is_err() {
            restore_pair(dir, &before_config, &before_backup)?;
        }
        attempt
    })?
}

pub fn write_setup_config(dir: &Path, updates: &[(&str, Value)]) -> io::Result<SetupWriteResult> {
    write_setup_config_inner(dir, updates, || Ok(()))
}

pub fn rollback_setup_write(
    dir: &Path,
    boundary: &SetupWriteBoundary,
) -> io::Result<RollbackOutcome> {
    with_flock(&dir.join("config.toml.lock"), || {
        let (config, backup) = pair(dir)?;
        if config != boundary.after_config || backup != boundary.after_backup {
            return Ok(RollbackOutcome::ConcurrentChange);
        }
        restore_pair(dir, &boundary.before_config, &boundary.before_backup)?;
        Ok(RollbackOutcome::Restored)
    })?
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn creates_merges_backs_up_and_preserves_comments_and_unknown_keys() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path();
        let first = write_config_keys(dir, &[("toast", json!(true))]).unwrap();
        assert!(first.is_truthy("toast"));
        assert!(!dir.join("config.toml.bak").exists());

        fs::write(
            dir.join("config.toml"),
            "# hand edited\nfuture_thing = 1\ntoast = true # keep me\n",
        )
        .unwrap();
        let second =
            write_config_keys(dir, &[("toast", json!(false)), ("announce", json!([]))]).unwrap();
        let text = fs::read_to_string(dir.join("config.toml")).unwrap();
        assert!(text.contains("# hand edited"));
        assert!(text.contains("future_thing = 1"));
        assert!(text.contains("toast = false # keep me"));
        assert!(text.contains("announce = []"));
        assert!(!second.is_truthy("toast"));
        assert_eq!(second.get("announce"), &json!([]));
        assert_eq!(
            fs::read_to_string(dir.join("config.toml.bak")).unwrap(),
            "# hand edited\nfuture_thing = 1\ntoast = true # keep me\n"
        );
        assert!(dir.join("config.toml.lock").is_file());
    }

    #[test]
    fn setup_abort_restores_config_backup_modes_and_fresh_files() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path();
        let config_path = dir.join("config.toml");
        let backup_path = dir.join("config.toml.bak");
        fs::write(&config_path, "# original\nsummary = \"codex\"\n").unwrap();
        fs::write(&backup_path, "# prior backup\nsummary = \"command\"\n").unwrap();
        fs::set_permissions(&config_path, fs::Permissions::from_mode(0o640)).unwrap();
        fs::set_permissions(&backup_path, fs::Permissions::from_mode(0o600)).unwrap();

        let written = write_setup_config(dir, &[("summary", json!("template"))]).unwrap();
        assert_eq!(
            rollback_setup_write(dir, &written.boundary).unwrap(),
            RollbackOutcome::Restored
        );
        assert_eq!(
            fs::read_to_string(&config_path).unwrap(),
            "# original\nsummary = \"codex\"\n"
        );
        assert_eq!(
            fs::read_to_string(&backup_path).unwrap(),
            "# prior backup\nsummary = \"command\"\n"
        );
        assert_eq!(
            fs::metadata(&config_path).unwrap().permissions().mode() & 0o7777,
            0o640
        );
        assert_eq!(
            fs::metadata(&backup_path).unwrap().permissions().mode() & 0o7777,
            0o600
        );

        let fresh = tempfile::tempdir().unwrap();
        let written = write_setup_config(fresh.path(), &[("toast", json!(true))]).unwrap();
        rollback_setup_write(fresh.path(), &written.boundary).unwrap();
        assert!(!fresh.path().join("config.toml").exists());
        assert!(!fresh.path().join("config.toml.bak").exists());
    }

    #[test]
    fn setup_abort_keeps_a_concurrent_change() {
        let temp = tempfile::tempdir().unwrap();
        let written = write_setup_config(temp.path(), &[("summary", json!("template"))]).unwrap();
        write_config_keys(temp.path(), &[("voice", json!("Alex"))]).unwrap();
        assert_eq!(
            rollback_setup_write(temp.path(), &written.boundary).unwrap(),
            RollbackOutcome::ConcurrentChange
        );
        let loaded = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        assert_eq!(loaded.string("summary"), Some("template"));
        assert_eq!(loaded.string("voice"), Some("Alex"));
    }

    #[test]
    fn interrupted_setup_write_restores_its_before_image_under_the_lock() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("config.toml"), "toast = false\n").unwrap();
        let error = write_setup_config_inner(temp.path(), &[("toast", json!(true))], || {
            Err(io::Error::new(io::ErrorKind::Interrupted, "injected"))
        })
        .err()
        .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        assert_eq!(
            fs::read_to_string(temp.path().join("config.toml")).unwrap(),
            "toast = false\n"
        );
        assert!(!temp.path().join("config.toml.bak").exists());
    }

    #[test]
    fn stale_setup_writers_rebase_only_chosen_keys_and_preserve_toml() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("config.toml");
        fs::write(
            &path,
            "# hand edited\nfuture_thing = 1\ntoast = false # toast comment\n[future]\nprovider = \"new\"\n",
        )
        .unwrap();
        let stale_toast = json!(true);
        let stale_voice = json!("Alex");
        write_setup_config(temp.path(), &[("toast", stale_toast)]).unwrap();
        write_setup_config(temp.path(), &[("voice", stale_voice)]).unwrap();
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.contains("# hand edited"));
        assert!(text.contains("future_thing = 1"));
        assert!(text.contains("toast = true # toast comment"));
        assert!(text.contains("[future]\nprovider = \"new\""));
        let loaded = load_config(temp.path(), &mut Vec::new(), None).unwrap();
        assert!(loaded.is_truthy("toast"));
        assert_eq!(loaded.string("voice"), Some("Alex"));
        assert!(temp.path().join("config.toml.bak").exists());
    }
}
