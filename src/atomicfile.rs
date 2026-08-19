use serde_json::Value;
use std::fs;
use std::io::{self, Write};
use std::path::Path;

fn persist(mut file: tempfile::NamedTempFile, path: &Path) -> io::Result<()> {
    file.flush()?;
    file.persist(path).map(|_| ()).map_err(|error| error.error)
}

pub fn write_json_atomic(path: &Path, value: &Value) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let mut file = tempfile::Builder::new()
        .prefix(&format!(
            "{}.",
            path.file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or("state")
        ))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    serde_json::to_writer(file.as_file_mut(), value).map_err(io::Error::other)?;
    file.write_all(b"\n")?;
    persist(file, path)
}

pub fn write_text_atomic(path: &Path, text: &str) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let mut file = tempfile::Builder::new()
        .prefix(&format!(
            "{}.",
            path.file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or("state")
        ))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    file.write_all(text.as_bytes())?;
    persist(file, path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn json_is_compact_sorted_and_newline_terminated() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("value.json");
        write_json_atomic(&path, &json!({"z": 1, "a": {"y": 2, "b": 3}})).unwrap();
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            "{\"a\":{\"b\":3,\"y\":2},\"z\":1}\n"
        );
    }

    #[test]
    fn text_replaces_existing_content_and_leaves_no_tempfile() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("value.txt");
        fs::write(&path, "old").unwrap();
        write_text_atomic(&path, "new\n").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "new\n");
        assert_eq!(fs::read_dir(temp.path()).unwrap().count(), 1);
    }
}
