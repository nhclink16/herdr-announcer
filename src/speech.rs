use crate::config::Config;
use crate::redact::{redact_command, redact_command_text};
use rustix::fs::{FlockOperation, flock};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use tempfile::Builder;

pub const CAPABILITY_NAMES: [&str; 12] = [
    "codex",
    "claude",
    "say",
    "spd-say",
    "espeak-ng",
    "espeak",
    "mpv",
    "ffplay",
    "afplay",
    "paplay",
    "pw-play",
    "aplay",
];

#[derive(Clone, Copy, Debug)]
pub struct SpeechOptions {
    pub playback_timeout: Duration,
    pub playback_poll_interval: Duration,
}

impl Default for SpeechOptions {
    fn default() -> Self {
        Self {
            playback_timeout: Duration::from_secs(90),
            playback_poll_interval: Duration::from_millis(500),
        }
    }
}

#[derive(Debug)]
pub enum SpeakError {
    PlaybackLockTimeout,
    InvalidCommand(String),
    Spawn {
        command: Vec<String>,
        source: io::Error,
    },
    Timeout {
        command: Vec<String>,
    },
    Exit {
        command: Vec<String>,
        status: ExitStatus,
        stdout: String,
        stderr: String,
    },
    Other(String),
}

impl SpeakError {
    pub fn is_playback_lock_timeout(&self) -> bool {
        matches!(self, Self::PlaybackLockTimeout)
    }
}

impl fmt::Display for SpeakError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PlaybackLockTimeout => formatter.write_str("playback lock timed out"),
            Self::InvalidCommand(message) | Self::Other(message) => formatter.write_str(message),
            Self::Spawn { command, source } => {
                write!(formatter, "failed to run {command:?}: {source}")
            }
            Self::Timeout { command } => write!(formatter, "command {command:?} timed out"),
            Self::Exit {
                command,
                status,
                stdout,
                stderr,
            } => {
                write!(formatter, "command {command:?} exited with {status}")?;
                if !stderr.trim().is_empty() {
                    write!(formatter, ": {}", stderr.trim())?;
                } else if !stdout.trim().is_empty() {
                    write!(formatter, ": {}", stdout.trim())?;
                }
                Ok(())
            }
        }
    }
}

impl std::error::Error for SpeakError {}

#[derive(Debug)]
struct CommandOutput {
    status: ExitStatus,
    stdout: String,
    stderr: String,
}

fn read_pipe(mut pipe: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = pipe.read_to_end(&mut bytes);
        bytes
    })
}

fn run_command(
    command: &[String],
    input: Option<&str>,
    timeout: Duration,
) -> Result<CommandOutput, SpeakError> {
    let Some(program) = command.first() else {
        return Err(SpeakError::InvalidCommand(
            "speak_command must not be empty".to_owned(),
        ));
    };
    let mut process = Command::new(program);
    process
        .args(&command[1..])
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = process.spawn().map_err(|source| SpeakError::Spawn {
        command: redact_command(command),
        source,
    })?;
    let stdout = read_pipe(child.stdout.take().expect("piped stdout"));
    let stderr = read_pipe(child.stderr.take().expect("piped stderr"));
    if let Some(input) = input
        && let Some(mut stdin) = child.stdin.take()
    {
        // Match subprocess.communicate: an early child exit can close stdin,
        // but its real exit status is still the useful failure semantics.
        let _ = stdin.write_all(input.as_bytes());
    }
    drop(child.stdin.take());

    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < timeout => {
                thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout.join();
                let _ = stderr.join();
                return Err(SpeakError::Timeout {
                    command: redact_command(command),
                });
            }
            Err(source) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout.join();
                let _ = stderr.join();
                return Err(SpeakError::Spawn {
                    command: redact_command(command),
                    source,
                });
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

fn checked_command(
    command: &[String],
    input: Option<&str>,
    timeout: Duration,
) -> Result<(), SpeakError> {
    let output = run_command(command, input, timeout)?;
    if output.status.success() {
        return Ok(());
    }
    Err(SpeakError::Exit {
        command: redact_command(command),
        status: output.status,
        stdout: redact_command_text(&output.stdout, command),
        stderr: redact_command_text(&output.stderr, command),
    })
}

pub fn run_custom_speech(command_value: &Value, text: &str) -> Result<String, SpeakError> {
    let Some(values) = command_value.as_array() else {
        return Err(SpeakError::InvalidCommand(
            "speak_command must be an argv array of strings".to_owned(),
        ));
    };
    let command: Vec<String> = values
        .iter()
        .map(|value| {
            value.as_str().map(ToOwned::to_owned).ok_or_else(|| {
                SpeakError::InvalidCommand(
                    "speak_command must be an argv array of strings".to_owned(),
                )
            })
        })
        .collect::<Result<_, _>>()?;
    if command.is_empty() {
        return Err(SpeakError::InvalidCommand(
            "speak_command must not be empty".to_owned(),
        ));
    }
    let used_placeholder = command.iter().any(|argument| argument.contains("{text}"));
    let expanded: Vec<_> = command
        .iter()
        .map(|argument| argument.replace("{text}", text))
        .collect();
    checked_command(
        &expanded,
        (!used_placeholder).then_some(text),
        Duration::from_secs(60),
    )?;
    Ok("command".to_owned())
}

fn find_on_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|directory| directory.join(name))
        .find(|candidate| {
            let Ok(metadata) = fs::metadata(candidate) else {
                return false;
            };
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
            }
            #[cfg(not(unix))]
            {
                metadata.is_file()
            }
        })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct AudioPlayer {
    name: &'static str,
    raw_pcm: bool,
}

fn audio_player_with(
    system: &str,
    resolve: impl Fn(&str) -> Option<PathBuf>,
) -> Option<AudioPlayer> {
    let mp3_players: &[&str] = if system == "macos" {
        &["afplay", "mpv", "ffplay"]
    } else {
        &["mpv", "ffplay", "afplay"]
    };
    for &name in mp3_players {
        if resolve(name).is_some() {
            return Some(AudioPlayer {
                name,
                raw_pcm: false,
            });
        }
    }
    for name in ["paplay", "pw-play", "aplay"] {
        if resolve(name).is_some() {
            return Some(AudioPlayer {
                name,
                raw_pcm: true,
            });
        }
    }
    None
}

fn audio_player() -> Option<AudioPlayer> {
    audio_player_with(std::env::consts::OS, find_on_path)
}

fn percent_encode_path_segment(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(*byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

#[derive(Debug)]
enum ElevenLabsError {
    Http(u16),
    Other(String),
}

fn elevenlabs_reason(error: &ElevenLabsError) -> String {
    match error {
        ElevenLabsError::Http(code) => format!("elevenlabs: HTTP {code}"),
        ElevenLabsError::Other(detail) => format!(
            "elevenlabs: {}",
            if detail.trim().is_empty() {
                "ElevenLabs request failed".to_owned()
            } else {
                detail.trim().chars().take(120).collect()
            }
        ),
    }
}

fn synthesize_elevenlabs_at(
    config: &Config,
    text: &str,
    state_dir: &Path,
    output_format: &str,
    base: &str,
) -> Result<PathBuf, ElevenLabsError> {
    let voice_id = percent_encode_path_segment(
        config
            .string("elevenlabs_voice_id")
            .unwrap_or("21m00Tcm4TlvDq8ikWAM"),
    );
    let url = format!(
        "{}/v1/text-to-speech/{voice_id}?output_format={output_format}",
        base.trim_end_matches('/')
    );
    let body = serde_json::to_string(&serde_json::json!({
        "text": text,
        "model_id": config.string("elevenlabs_model").unwrap_or("eleven_turbo_v2_5"),
    }))
    .map_err(|error| ElevenLabsError::Other(error.to_string()))?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(30)))
        .build()
        .into();
    let response = agent
        .post(&url)
        .header(
            "xi-api-key",
            config.string("elevenlabs_api_key").unwrap_or(""),
        )
        .header("Content-Type", "application/json")
        .send(body.as_bytes());
    let mut response = match response {
        Ok(response) => response,
        Err(ureq::Error::StatusCode(code)) => return Err(ElevenLabsError::Http(code)),
        Err(error) => return Err(ElevenLabsError::Other(error.to_string())),
    };
    let audio = response
        .body_mut()
        .read_to_vec()
        .map_err(|error| ElevenLabsError::Other(error.to_string()))?;
    if audio.is_empty() {
        return Err(ElevenLabsError::Other(
            "ElevenLabs returned no audio".to_owned(),
        ));
    }
    fs::create_dir_all(state_dir).map_err(|error| ElevenLabsError::Other(error.to_string()))?;
    let suffix = if output_format == "pcm_22050" {
        ".pcm"
    } else {
        ".mp3"
    };
    let mut temporary = Builder::new()
        .suffix(suffix)
        .tempfile_in(state_dir)
        .map_err(|error| ElevenLabsError::Other(error.to_string()))?;
    temporary
        .write_all(&audio)
        .map_err(|error| ElevenLabsError::Other(error.to_string()))?;
    let (_, path) = temporary
        .keep()
        .map_err(|error| ElevenLabsError::Other(error.error.to_string()))?;
    Ok(path)
}

fn synthesize_elevenlabs(
    config: &Config,
    text: &str,
    state_dir: &Path,
    output_format: &str,
) -> Result<PathBuf, ElevenLabsError> {
    // Test-only override: production always defaults to the official HTTPS API.
    let base = std::env::var("HERDR_ANNOUNCER_ELEVENLABS_BASE")
        .unwrap_or_else(|_| "https://api.elevenlabs.io".to_owned());
    synthesize_elevenlabs_at(config, text, state_dir, output_format, &base)
}

fn audio_player_command(path: &Path, player: AudioPlayer) -> Result<Vec<String>, SpeakError> {
    let command = match (player.name, player.raw_pcm) {
        ("paplay", true) => [
            "paplay",
            "--raw",
            "--rate=22050",
            "--channels=1",
            "--format=s16le",
        ]
        .into_iter()
        .map(ToOwned::to_owned)
        .chain(std::iter::once(path.to_string_lossy().into_owned()))
        .collect(),
        ("pw-play", true) => ["pw-play", "--rate=22050", "--channels=1", "--format=s16"]
            .into_iter()
            .map(ToOwned::to_owned)
            .chain(std::iter::once(path.to_string_lossy().into_owned()))
            .collect(),
        ("aplay", true) => [
            "aplay",
            "--file-type=raw",
            "--format=S16_LE",
            "--rate=22050",
            "--channels=1",
        ]
        .into_iter()
        .map(ToOwned::to_owned)
        .chain(std::iter::once(path.to_string_lossy().into_owned()))
        .collect(),
        ("mpv", false) => vec![
            "mpv".to_owned(),
            "--no-video".to_owned(),
            path.to_string_lossy().into_owned(),
        ],
        ("ffplay", false) => vec![
            "ffplay".to_owned(),
            "-nodisp".to_owned(),
            "-autoexit".to_owned(),
            path.to_string_lossy().into_owned(),
        ],
        ("afplay", false) => vec!["afplay".to_owned(), path.to_string_lossy().into_owned()],
        _ => return Err(SpeakError::Other("unsupported audio player".to_owned())),
    };
    Ok(command)
}

fn play_audio_file(path: &Path, player: AudioPlayer) -> Result<String, SpeakError> {
    let command = audio_player_command(path, player)?;
    checked_command(&command, None, Duration::from_secs(60))?;
    Ok("elevenlabs".to_owned())
}

fn failure_reason(name: &str, error: &SpeakError) -> String {
    if matches!(error, SpeakError::Timeout { .. }) {
        format!("{name}: timeout")
    } else {
        let detail: String = error.to_string().trim().chars().take(120).collect();
        format!(
            "{name}: {}",
            if detail.is_empty() {
                "speech failed"
            } else {
                &detail
            }
        )
    }
}

fn run_local_speech_with(
    config: &Config,
    text: &str,
    reasons: &mut Vec<String>,
    system: &str,
    resolve: impl Fn(&str) -> Option<PathBuf>,
) -> Result<String, SpeakError> {
    if system == "macos" {
        let program = resolve("say").unwrap_or_else(|| PathBuf::from("say"));
        let mut command = vec![program.to_string_lossy().into_owned()];
        if let Some(voice) = config.string("voice").filter(|voice| !voice.is_empty()) {
            command.extend(["-v".to_owned(), voice.to_owned()]);
        }
        checked_command(&command, Some(text), Duration::from_secs(60))?;
        return Ok("say".to_owned());
    }
    if system == "linux" {
        let attempts: [(&str, &[&str], Duration); 3] = [
            ("spd-say", &["-e", "-w"], Duration::from_secs(5)),
            ("espeak-ng", &[], Duration::from_secs(60)),
            ("espeak", &[], Duration::from_secs(60)),
        ];
        let mut last_error = None;
        for (name, arguments, timeout) in attempts {
            let program = resolve(name).unwrap_or_else(|| PathBuf::from(name));
            let command: Vec<_> = std::iter::once(program.to_string_lossy().into_owned())
                .chain(arguments.iter().map(|argument| (*argument).to_owned()))
                .collect();
            match checked_command(&command, Some(text), timeout) {
                Ok(()) => return Ok(name.to_owned()),
                Err(error) => {
                    reasons.push(failure_reason(name, &error));
                    last_error = Some(error);
                }
            }
        }
        if let Some(error) = last_error {
            return Err(error);
        }
    }
    Err(SpeakError::Other(format!(
        "local text-to-speech is unsupported on {system}"
    )))
}

pub fn run_local_speech(
    config: &Config,
    text: &str,
    reasons: &mut Vec<String>,
) -> Result<String, SpeakError> {
    run_local_speech_with(config, text, reasons, std::env::consts::OS, find_on_path)
}

pub fn with_playback_lock<T>(
    state_dir: &Path,
    reasons: &mut Vec<String>,
    options: SpeechOptions,
    operation: impl FnOnce(&mut Vec<String>) -> Result<T, SpeakError>,
) -> Result<T, SpeakError> {
    fs::create_dir_all(state_dir).map_err(|error| SpeakError::Other(error.to_string()))?;
    let path = state_dir.join("speak.lock");
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| SpeakError::Other(error.to_string()))?;
    let started = Instant::now();
    loop {
        match flock(&file, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => break,
            Err(error) => {
                let io_error = io::Error::from(error);
                if io_error.kind() != io::ErrorKind::WouldBlock {
                    return Err(SpeakError::Other(io_error.to_string()));
                }
                let remaining = options.playback_timeout.saturating_sub(started.elapsed());
                if remaining.is_zero() {
                    reasons.push("playback-lock: timeout".to_owned());
                    return Err(SpeakError::PlaybackLockTimeout);
                }
                thread::sleep(options.playback_poll_interval.min(remaining));
            }
        }
    }
    let result = operation(reasons);
    let unlock = flock(&file, FlockOperation::Unlock)
        .map_err(io::Error::from)
        .map_err(|error| SpeakError::Other(error.to_string()));
    match (result, unlock) {
        (Err(error), _) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Ok(value), Ok(())) => Ok(value),
    }
}

pub fn speak_with_options(
    config: &Config,
    text: &str,
    state_dir: &Path,
    reasons: &mut Vec<String>,
    options: SpeechOptions,
) -> Result<String, SpeakError> {
    if !config.get("speak_command").is_null() {
        return with_playback_lock(state_dir, reasons, options, |_| {
            run_custom_speech(config.get("speak_command"), text)
        });
    }
    if config.is_truthy("elevenlabs_api_key") {
        let selected = audio_player();
        if let Some(player) = selected {
            let format = if player.raw_pcm {
                "pcm_22050"
            } else {
                "mp3_44100_128"
            };
            let mut audio_path = None;
            let attempt = match synthesize_elevenlabs(config, text, state_dir, format) {
                Ok(path) => {
                    audio_path = Some(path.clone());
                    with_playback_lock(state_dir, reasons, options, |_| {
                        play_audio_file(&path, player)
                    })
                    .map_err(|error| {
                        if error.is_playback_lock_timeout() {
                            error
                        } else {
                            SpeakError::Other(elevenlabs_reason(&ElevenLabsError::Other(
                                error.to_string(),
                            )))
                        }
                    })
                }
                Err(error) => Err(SpeakError::Other(elevenlabs_reason(&error))),
            };
            if let Some(path) = audio_path {
                let _ = fs::remove_file(path);
            }
            match attempt {
                Ok(backend) => return Ok(backend),
                Err(error) if error.is_playback_lock_timeout() => return Err(error),
                Err(error) => reasons.push(error.to_string()),
            }
        } else {
            reasons.push("elevenlabs: no-player".to_owned());
            reasons.push("play: mpv/ffplay missing".to_owned());
        }
    }
    with_playback_lock(state_dir, reasons, options, |reasons| {
        run_local_speech(config, text, reasons)
    })
}

pub fn speak(
    config: &Config,
    text: &str,
    state_dir: &Path,
    reasons: &mut Vec<String>,
) -> Result<String, SpeakError> {
    speak_with_options(config, text, state_dir, reasons, SpeechOptions::default())
}

pub fn capabilities() -> BTreeMap<String, bool> {
    CAPABILITY_NAMES
        .into_iter()
        .map(|name| (name.to_owned(), find_on_path(name).is_some()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::load_config;
    use std::net::TcpListener;

    fn defaults() -> Config {
        load_config(Path::new("/definitely/missing"), &mut Vec::new(), None).unwrap()
    }

    #[test]
    fn sanitizer_adjacent_custom_command_input_and_placeholder_shapes_work() {
        let temp = tempfile::tempdir().unwrap();
        let output = temp.path().join("spoken.txt");
        let command = serde_json::json!(["/bin/sh", "-c", format!("cat > {}", output.display())]);
        assert_eq!(run_custom_speech(&command, "hello").unwrap(), "command");
        assert_eq!(fs::read_to_string(&output).unwrap(), "hello");
        let command = serde_json::json!([
            "/bin/sh",
            "-c",
            format!("printf %s {{text}} > {}", output.display())
        ]);
        run_custom_speech(&command, "again").unwrap();
        assert_eq!(fs::read_to_string(&output).unwrap(), "again");
    }

    #[test]
    fn failed_custom_command_preserves_exit_and_redacts_secrets() {
        let secret = "sk-sentinel-secret-4321";
        let command = serde_json::json!([
            "/bin/sh",
            "-c",
            "echo \"$2\" >&2; exit 7",
            "_",
            "--api-key",
            secret
        ]);
        let error = run_custom_speech(&command, "hello").unwrap_err();
        assert!(matches!(error, SpeakError::Exit { .. }));
        let rendered = error.to_string();
        assert!(!rendered.contains(secret));
        assert!(rendered.contains("****4321"));
    }

    #[test]
    fn linux_probe_order_and_timeout_reason_match_python() {
        let temp = tempfile::tempdir().unwrap();
        for (name, body) in [("spd-say", "exit 7"), ("espeak-ng", "cat >/dev/null")] {
            let path = temp.path().join(name);
            fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        let mut reasons = Vec::new();
        let result = run_local_speech_with(&defaults(), "-danger", &mut reasons, "linux", |name| {
            (name != "espeak").then(|| temp.path().join(name))
        });
        assert_eq!(result.unwrap(), "espeak-ng");
        assert!(reasons.first().unwrap().starts_with("spd-say:"));

        let command = vec!["/bin/sh".to_owned(), "-c".to_owned(), "sleep 1".to_owned()];
        assert!(matches!(
            checked_command(&command, None, Duration::from_millis(20)),
            Err(SpeakError::Timeout { .. })
        ));
    }

    #[test]
    fn macos_say_gets_voice_arguments_and_text_on_stdin() {
        let temp = tempfile::tempdir().unwrap();
        let config_dir = temp.path().join("config");
        fs::create_dir_all(&config_dir).unwrap();
        fs::write(config_dir.join("config.toml"), "voice = \"Samantha\"\n").unwrap();
        let config = load_config(&config_dir, &mut Vec::new(), None).unwrap();
        let args = temp.path().join("args.txt");
        let input = temp.path().join("input.txt");
        let say = temp.path().join("say");
        fs::write(
            &say,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > {}\ncat > {}\n",
                args.display(),
                input.display()
            ),
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&say, fs::Permissions::from_mode(0o755)).unwrap();
        let backend = run_local_speech_with(&config, "-danger", &mut Vec::new(), "macos", |_| {
            Some(say.clone())
        })
        .unwrap();
        assert_eq!(backend, "say");
        assert_eq!(fs::read_to_string(args).unwrap(), "-v\nSamantha\n");
        assert_eq!(fs::read_to_string(input).unwrap(), "-danger");
    }

    #[test]
    fn held_playback_lock_times_out_and_is_reusable_after_release() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("speak.lock");
        let held = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .unwrap();
        flock(&held, FlockOperation::LockExclusive).unwrap();
        let options = SpeechOptions {
            playback_timeout: Duration::from_millis(40),
            playback_poll_interval: Duration::from_millis(10),
        };
        let mut reasons = Vec::new();
        let error = with_playback_lock(temp.path(), &mut reasons, options, |_| Ok(())).unwrap_err();
        assert!(error.is_playback_lock_timeout());
        assert_eq!(reasons, ["playback-lock: timeout"]);
        flock(&held, FlockOperation::Unlock).unwrap();
        let failure = with_playback_lock(temp.path(), &mut reasons, options, |_| {
            Err::<(), _>(SpeakError::Other("boom".to_owned()))
        })
        .unwrap_err();
        assert_eq!(failure.to_string(), "boom");
        with_playback_lock(temp.path(), &mut reasons, options, |_| Ok(())).unwrap();
    }

    #[test]
    fn player_probe_order_and_pcm_commands_are_exact() {
        let player = audio_player_with("macos", |name| (name == "afplay").then(PathBuf::new));
        assert_eq!(
            player,
            Some(AudioPlayer {
                name: "afplay",
                raw_pcm: false
            })
        );
        let player = audio_player_with("linux", |name| (name == "paplay").then(PathBuf::new));
        assert_eq!(
            player,
            Some(AudioPlayer {
                name: "paplay",
                raw_pcm: true
            })
        );
        assert_eq!(
            audio_player_command(Path::new("audio.pcm"), player.unwrap()).unwrap(),
            [
                "paplay",
                "--raw",
                "--rate=22050",
                "--channels=1",
                "--format=s16le",
                "audio.pcm",
            ]
        );
        assert!(audio_player_with("linux", |_| None).is_none());
    }

    #[test]
    fn elevenlabs_http_request_shape_and_mp3_file_are_exact() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            loop {
                let count = stream.read(&mut buffer).unwrap();
                request.extend_from_slice(&buffer[..count]);
                let Some(header_end) = request.windows(4).position(|part| part == b"\r\n\r\n")
                else {
                    continue;
                };
                let headers = String::from_utf8_lossy(&request[..header_end + 4]);
                let length = headers
                    .lines()
                    .find_map(|line| {
                        line.trim_end()
                            .to_ascii_lowercase()
                            .strip_prefix("content-length: ")
                            .and_then(|value| value.parse::<usize>().ok())
                    })
                    .unwrap();
                if request.len() >= header_end + 4 + length {
                    break;
                }
            }
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nFAKE-MP3",
                )
                .unwrap();
            String::from_utf8(request).unwrap()
        });
        let temp = tempfile::tempdir().unwrap();
        let config_dir = temp.path().join("config");
        let state_dir = temp.path().join("state");
        fs::create_dir_all(&config_dir).unwrap();
        fs::create_dir_all(&state_dir).unwrap();
        fs::write(
            config_dir.join("config.toml"),
            "elevenlabs_api_key = \"sk-eleven-secret-4321\"\nelevenlabs_voice_id = \"voice / one\"\nelevenlabs_model = \"model-test\"\n",
        )
        .unwrap();
        let config = load_config(&config_dir, &mut Vec::new(), None).unwrap();
        let audio = synthesize_elevenlabs_at(
            &config,
            "hello there",
            &state_dir,
            "mp3_44100_128",
            &format!("http://{address}"),
        )
        .unwrap();
        assert_eq!(
            audio.extension().and_then(|value| value.to_str()),
            Some("mp3")
        );
        assert_eq!(fs::read(&audio).unwrap(), b"FAKE-MP3");
        fs::remove_file(audio).unwrap();
        let request = server.join().unwrap();
        let lower = request.to_ascii_lowercase();
        assert!(request.starts_with(
            "POST /v1/text-to-speech/voice%20%2F%20one?output_format=mp3_44100_128 HTTP/1.1\r\n"
        ));
        assert!(lower.contains("xi-api-key: sk-eleven-secret-4321\r\n"));
        assert!(lower.contains("content-type: application/json\r\n"));
        let body = request.split_once("\r\n\r\n").unwrap().1;
        assert_eq!(
            serde_json::from_str::<Value>(body).unwrap(),
            serde_json::json!({"text":"hello there", "model_id":"model-test"})
        );
    }
}
