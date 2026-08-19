use serde_json::{Value, json};
use std::env;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

const PLUGIN_ID: &str = "nhclink16.announcer";
const RPC_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_RESPONSE_BYTES: u64 = 4 * 1024 * 1024;
const TRANSCRIPT_CHARS: usize = 4000;

#[derive(Clone, Debug)]
pub struct Client {
    socket: PathBuf,
    timeout: Duration,
}

impl Client {
    pub fn from_env() -> io::Result<Self> {
        let socket = env::var_os("HERDR_SOCKET_PATH")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no socket path"))?;
        Ok(Self::new(socket))
    }

    pub fn new(socket: impl Into<PathBuf>) -> Self {
        Self {
            socket: socket.into(),
            timeout: RPC_TIMEOUT,
        }
    }

    pub fn with_timeout(socket: impl Into<PathBuf>, timeout: Duration) -> Self {
        Self {
            socket: socket.into(),
            timeout,
        }
    }

    pub fn socket_path(&self) -> &Path {
        &self.socket
    }

    pub fn call(&self, method: &str, params: Value) -> io::Result<Value> {
        let mut stream = UnixStream::connect(&self.socket)?;
        stream.set_read_timeout(Some(self.timeout))?;
        stream.set_write_timeout(Some(self.timeout))?;
        let request = json!({
            "id": format!("{PLUGIN_ID}:{method}"),
            "method": method,
            "params": params,
        });
        serde_json::to_writer(&mut stream, &request).map_err(io::Error::other)?;
        stream.write_all(b"\n")?;
        stream.flush()?;

        let mut reader = BufReader::new(stream.take(MAX_RESPONSE_BYTES + 1));
        let mut response = Vec::new();
        let read = reader.read_until(b'\n', &mut response)?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("herdr rpc {method}: empty response"),
            ));
        }
        if response.len() as u64 > MAX_RESPONSE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("herdr rpc {method}: response exceeds 4 MiB"),
            ));
        }
        let payload: Value = serde_json::from_slice(&response).map_err(io::Error::other)?;
        if let Some(error) = payload.get("error").and_then(Value::as_object) {
            let code = error.get("code").map(render_scalar).unwrap_or_default();
            let message = error.get("message").map(render_scalar).unwrap_or_default();
            return Err(io::Error::other(format!(
                "herdr rpc {method}: {code} {message}"
            )));
        }
        payload.get("result").cloned().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("herdr rpc {method}: response has no result"),
            )
        })
    }

    pub fn pane_read(&self, pane_id: &str) -> io::Result<String> {
        let result = self.call(
            "pane.read",
            json!({
                "pane_id": pane_id,
                "source": "recent_unwrapped",
                "format": "text",
                "lines": 100,
                "strip_ansi": true,
            }),
        )?;
        Ok(last_chars(
            &extract_text_value(&result).unwrap_or_default(),
            TRANSCRIPT_CHARS,
        ))
    }

    pub fn workspace_label(&self, workspace_id: &str) -> io::Result<Option<String>> {
        let result = self.call("workspace.list", json!({}))?;
        let records = result
            .get("workspaces")
            .and_then(Value::as_array)
            .into_iter()
            .flatten();
        for record in records {
            let Some(object) = record.as_object() else {
                continue;
            };
            let matches = ["workspace_id", "id"]
                .into_iter()
                .any(|key| object.get(key).and_then(Value::as_str) == Some(workspace_id));
            if !matches {
                continue;
            }
            for key in ["label", "title", "name"] {
                if let Some(value) = object
                    .get(key)
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    return Ok(Some(value.to_owned()));
                }
            }
            return Ok(None);
        }
        Ok(None)
    }

    pub fn notification_show(&self, text: &str) -> io::Result<()> {
        self.call(
            "notification.show",
            json!({
                "title": text,
                "body": Value::Null,
                "sound": "none",
                "position": Value::Null,
            }),
        )?;
        Ok(())
    }

    pub fn pane_current(&self, caller_pane_id: &str) -> io::Result<Value> {
        pane_from_result(self.call("pane.current", json!({"caller_pane_id": caller_pane_id}))?)
    }

    pub fn pane_list(&self, workspace_id: Option<&str>) -> io::Result<Vec<Value>> {
        let result = self.call("pane.list", json!({"workspace_id": workspace_id}))?;
        Ok(result
            .get("panes")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    pub fn pane_get(&self, pane_id: &str) -> io::Result<Value> {
        pane_from_result(self.call("pane.get", json!({"pane_id": pane_id}))?)
    }

    pub fn report_muted(&self, pane_id: &str, muted: bool) -> io::Result<()> {
        let mut params = json!({
            "pane_id": pane_id,
            "source": PLUGIN_ID,
            "tokens": {"muted": if muted { Value::String("1".to_owned()) } else { Value::Null }},
        });
        if muted {
            params["ttl_ms"] = json!(86_400_000);
        }
        self.call("pane.report_metadata", params)?;
        Ok(())
    }

    pub fn plugin_pane_open(
        &self,
        plugin_id: &str,
        entrypoint: &str,
        target_pane_id: Option<&str>,
        cwd: &Path,
        focus: bool,
    ) -> io::Result<Value> {
        let mut params = json!({
            "plugin_id": plugin_id,
            "entrypoint": entrypoint,
            "direction": "right",
            "cwd": cwd,
            "env": {},
            "focus": focus,
        });
        if let Some(target_pane_id) = target_pane_id {
            params["target_pane_id"] = json!(target_pane_id);
        }
        self.call("plugin.pane.open", params)
    }
}

fn pane_from_result(result: Value) -> io::Result<Value> {
    result
        .get("pane")
        .cloned()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "herdr response has no pane"))
}

fn render_scalar(value: &Value) -> String {
    value
        .as_str()
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| value.to_string())
}

fn extract_text_value(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.clone()),
        Value::Object(values) => {
            for key in ["text", "output", "content", "transcript"] {
                match values.get(key) {
                    Some(Value::String(value)) => return Some(value.clone()),
                    Some(Value::Array(values))
                        if values.iter().all(|value| value.as_str().is_some()) =>
                    {
                        return Some(
                            values
                                .iter()
                                .filter_map(Value::as_str)
                                .collect::<Vec<_>>()
                                .join("\n"),
                        );
                    }
                    _ => {}
                }
            }
            values.values().find_map(extract_text_value)
        }
        Value::Array(values) => values.iter().find_map(extract_text_value),
        Value::Null | Value::Bool(_) | Value::Number(_) => None,
    }
}

pub fn extract_read_text(raw_output: &str) -> String {
    match serde_json::from_str::<Value>(raw_output) {
        Ok(value) => extract_text_value(&value).unwrap_or_default(),
        Err(_) => raw_output.to_owned(),
    }
}

fn last_chars(value: &str, count: usize) -> String {
    let total = value.chars().count();
    value.chars().skip(total.saturating_sub(count)).collect()
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

fn default_client(reasons: &mut Vec<String>) -> Option<Client> {
    match Client::from_env() {
        Ok(client) => Some(client),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if !reasons
                .iter()
                .any(|reason| reason == "herdr: no socket path")
            {
                reasons.push("herdr: no socket path".to_owned());
            }
            None
        }
        Err(error) => {
            reasons.push(format!("herdr: {}", short_error(&error)));
            None
        }
    }
}

pub fn pane_read(pane_id: &str, reasons: &mut Vec<String>) -> String {
    let Some(client) = default_client(reasons) else {
        return String::new();
    };
    match client.pane_read(pane_id) {
        Ok(value) => value,
        Err(error) => {
            reasons.push(format!("herdr-read: {}", short_error(&error)));
            String::new()
        }
    }
}

pub fn workspace_label(workspace_id: &str, reasons: &mut Vec<String>) -> String {
    let Some(client) = default_client(reasons) else {
        return String::new();
    };
    match client.workspace_label(workspace_id) {
        Ok(Some(value)) => value,
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

pub fn notification_show(text: &str, reasons: &mut Vec<String>) {
    let Some(client) = default_client(reasons) else {
        return;
    };
    if let Err(error) = client.notification_show(text) {
        reasons.push(format!("toast: {}", short_error(&error)));
    }
}

pub fn pane_current() -> io::Result<Value> {
    let caller = env::var("HERDR_PANE_ID")
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no caller pane id"))?;
    Client::from_env()?.pane_current(&caller)
}

pub fn pane_list(workspace_id: Option<&str>) -> io::Result<Vec<Value>> {
    Client::from_env()?.pane_list(workspace_id)
}

pub fn pane_get(pane_id: &str) -> io::Result<Value> {
    Client::from_env()?.pane_get(pane_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;
    use std::thread;
    use std::time::Instant;

    fn one_response(response: Value) -> (tempfile::TempDir, Client, thread::JoinHandle<Value>) {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("herdr.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let handle = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream);
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let request = serde_json::from_str(&line).unwrap();
            let mut stream = reader.into_inner();
            serde_json::to_writer(&mut stream, &response).unwrap();
            stream.write_all(b"\n").unwrap();
            request
        });
        let client = Client::new(path);
        (temp, client, handle)
    }

    #[test]
    fn call_uses_contract_id_and_reports_rpc_errors_exactly() {
        let response = json!({
            "id": "ignored",
            "error": {"code": "bad", "message": "broken"}
        });
        let (_temp, client, handle) = one_response(response);
        let error = client.call("pane.get", json!({"pane_id":"p"})).unwrap_err();
        assert_eq!(error.to_string(), "herdr rpc pane.get: bad broken");
        let request = handle.join().unwrap();
        assert_eq!(request["id"], "nhclink16.announcer:pane.get");
    }

    #[test]
    fn pane_read_uses_nested_read_and_last_four_thousand_characters() {
        let text = format!("prefix{}", "界".repeat(4_100));
        let response = json!({"id":"x", "result":{"type":"pane_read", "read":{"text":text}}});
        let (_temp, client, handle) = one_response(response);
        let result = client.pane_read("p1").unwrap();
        assert_eq!(result.chars().count(), 4000);
        assert!(result.chars().all(|character| character == '界'));
        let request = handle.join().unwrap();
        assert_eq!(request["params"]["source"], "recent_unwrapped");
        assert_eq!(request["params"]["lines"], 100);
    }

    #[test]
    fn typed_wrappers_match_phase_zero_response_shapes_and_params() {
        let fixture: Value =
            serde_json::from_str(include_str!("../tests/fixtures/socket-pane-current.json"))
                .unwrap();
        let (_temp, client, handle) = one_response(fixture["response"].clone());
        let pane = client.pane_current("w7:p1").unwrap();
        assert_eq!(pane["pane_id"], "w7:p1");
        let request = handle.join().unwrap();
        assert_eq!(request["params"], json!({"caller_pane_id":"w7:p1"}));

        let fixture: Value =
            serde_json::from_str(include_str!("../tests/fixtures/socket-pane-list.json")).unwrap();
        let (_temp, client, handle) = one_response(fixture["response"].clone());
        assert_eq!(client.pane_list(Some("w7")).unwrap().len(), 1);
        assert_eq!(handle.join().unwrap()["params"]["workspace_id"], "w7");

        let fixture: Value =
            serde_json::from_str(include_str!("../tests/fixtures/socket-pane-get.json")).unwrap();
        let (_temp, client, handle) = one_response(fixture["response"].clone());
        assert_eq!(client.pane_get("w7:p1").unwrap()["pane_id"], "w7:p1");
        assert_eq!(handle.join().unwrap()["params"]["pane_id"], "w7:p1");

        let response = json!({"id":"x", "result":{"type":"ok"}});
        let (_temp, client, handle) = one_response(response);
        client.report_muted("w7:p1", true).unwrap();
        assert_eq!(
            handle.join().unwrap()["params"],
            json!({
                "pane_id":"w7:p1",
                "source":"nhclink16.announcer",
                "tokens":{"muted":"1"},
                "ttl_ms":86_400_000
            })
        );

        let response = json!({"id":"x", "result":{"type":"ok"}});
        let (_temp, client, handle) = one_response(response);
        client.report_muted("w7:p1", false).unwrap();
        assert_eq!(
            handle.join().unwrap()["params"],
            json!({
                "pane_id":"w7:p1",
                "source":"nhclink16.announcer",
                "tokens":{"muted":null}
            })
        );
    }

    #[test]
    fn extract_text_is_permissive() {
        assert_eq!(extract_read_text("plain output"), "plain output");
        assert_eq!(
            extract_read_text(r#"{"result":{"content":["one","two"]}}"#),
            "one\ntwo"
        );
    }

    #[test]
    fn accepting_silent_socket_times_out_in_under_two_seconds() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silent.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (_stream, _) = listener.accept().unwrap();
            thread::sleep(Duration::from_secs(1));
        });
        let client = Client::with_timeout(path, Duration::from_millis(100));
        let started = Instant::now();
        assert!(client.call("pane.list", json!({})).is_err());
        assert!(started.elapsed() < Duration::from_secs(2));
        server.join().unwrap();
    }
}
