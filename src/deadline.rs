use std::io::{BufRead, BufReader, Read};
use std::process::Child;
use std::sync::mpsc::Sender;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LineMessage {
    Line(String),
    Eof,
}

#[derive(Debug)]
pub struct DeadlineTimeout(pub &'static str);

impl std::fmt::Display for DeadlineTimeout {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for DeadlineTimeout {}

/// Wait for initial model activity, then grant a fresh completion window.
pub struct TwoPhaseDeadline {
    first_activity_deadline: Option<Instant>,
    completion_timeout: Option<Duration>,
    deadline: Option<Instant>,
    first_timeout_message: &'static str,
    completion_timeout_message: &'static str,
    activity_seen: bool,
}

impl TwoPhaseDeadline {
    pub fn new(
        first_activity_deadline: Option<Instant>,
        completion_timeout: Option<Duration>,
        deadline: Option<Instant>,
        first_timeout_message: &'static str,
        completion_timeout_message: &'static str,
    ) -> Self {
        Self {
            first_activity_deadline,
            completion_timeout,
            deadline,
            first_timeout_message,
            completion_timeout_message,
            activity_seen: first_activity_deadline.is_none(),
        }
    }

    pub fn active_deadline(&self) -> Result<Instant, &'static str> {
        (if self.activity_seen {
            self.deadline
        } else {
            self.first_activity_deadline
        })
        .ok_or("a response deadline is required")
    }

    pub fn remaining(&self) -> Result<Duration, DeadlineTimeout> {
        let deadline = self
            .active_deadline()
            .map_err(|_| DeadlineTimeout("a response deadline is required"))?;
        deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or_else(|| self.timeout())
    }

    pub fn timeout(&self) -> DeadlineTimeout {
        DeadlineTimeout(if self.activity_seen {
            self.completion_timeout_message
        } else {
            self.first_timeout_message
        })
    }

    pub fn record_activity(&mut self) {
        if self.activity_seen {
            return;
        }
        self.activity_seen = true;
        if let Some(timeout) = self.completion_timeout {
            self.deadline = Some(Instant::now() + timeout);
        }
    }

    pub fn activity_seen(&self) -> bool {
        self.activity_seen
    }
}

/// Pump decoded lines and always deliver EOF, including after a read error.
pub fn pump_stdout_lines<R>(stream: R, messages: Sender<LineMessage>) -> JoinHandle<()>
where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut reader = BufReader::new(stream);
        let mut bytes = Vec::new();
        loop {
            bytes.clear();
            match reader.read_until(b'\n', &mut bytes) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    let line = String::from_utf8_lossy(&bytes).into_owned();
                    if messages.send(LineMessage::Line(line)).is_err() {
                        return;
                    }
                }
            }
        }
        let _ = messages.send(LineMessage::Eof);
    })
}

/// Retain at most the first `max_chars`, while continuing to drain the pipe.
pub fn drain_stderr<R>(stream: R, max_chars: usize) -> JoinHandle<String>
where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut reader = BufReader::new(stream);
        let mut bytes = Vec::new();
        let mut output = String::new();
        loop {
            bytes.clear();
            match reader.read_until(b'\n', &mut bytes) {
                Ok(0) | Err(_) => break,
                Ok(_) if output.chars().count() < max_chars => {
                    let remaining = max_chars - output.chars().count();
                    output.extend(String::from_utf8_lossy(&bytes).chars().take(remaining));
                }
                Ok(_) => {}
            }
        }
        output
    })
}

/// Best-effort terminate, bounded wait, then kill. Cleanup errors are ignored.
pub fn stop_subprocess(child: &mut Child) {
    if child.try_wait().ok().flatten().is_some() {
        return;
    }

    #[cfg(unix)]
    {
        unsafe extern "C" {
            fn kill(pid: i32, signal: i32) -> i32;
        }
        // SIGTERM is 15 on the Unix targets supported by Herdr.
        let _ = unsafe { kill(child.id() as i32, 15) };
    }
    #[cfg(not(unix))]
    let _ = child.kill();

    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        thread::sleep(Duration::from_millis(10));
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{self, Cursor};
    use std::sync::mpsc;

    #[test]
    fn first_activity_timeout_uses_exact_message() {
        let deadline = TwoPhaseDeadline::new(
            Some(Instant::now()),
            Some(Duration::from_secs(5)),
            None,
            "Codex produced no model activity",
            "Codex summary timed out",
        );
        assert_eq!(
            deadline.remaining().unwrap_err().to_string(),
            "Codex produced no model activity"
        );
    }

    #[test]
    fn activity_starts_a_fresh_completion_window_and_is_idempotent() {
        let mut deadline = TwoPhaseDeadline::new(
            Some(Instant::now() + Duration::from_secs(1)),
            Some(Duration::from_secs(4)),
            None,
            "first",
            "completion",
        );
        deadline.record_activity();
        let first = deadline.active_deadline().unwrap();
        deadline.record_activity();
        assert_eq!(deadline.active_deadline().unwrap(), first);
        assert!(first > Instant::now() + Duration::from_secs(3));
    }

    #[test]
    fn single_phase_deadline_is_immediately_active() {
        let target = Instant::now() + Duration::from_secs(10);
        let deadline = TwoPhaseDeadline::new(None, None, Some(target), "first", "completion");
        assert!(deadline.activity_seen());
        assert_eq!(deadline.active_deadline().unwrap(), target);
    }

    #[test]
    fn stdout_pump_sends_lines_then_eof() {
        let (sender, receiver) = mpsc::channel();
        let thread = pump_stdout_lines(Cursor::new(b"one\ntwo\n"), sender);
        assert_eq!(
            receiver.recv().unwrap(),
            LineMessage::Line("one\n".to_owned())
        );
        assert_eq!(
            receiver.recv().unwrap(),
            LineMessage::Line("two\n".to_owned())
        );
        assert_eq!(receiver.recv().unwrap(), LineMessage::Eof);
        thread.join().unwrap();
    }

    #[test]
    fn stdout_pump_sends_eof_after_read_error() {
        struct Broken;
        impl Read for Broken {
            fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("broken"))
            }
        }
        let (sender, receiver) = mpsc::channel();
        pump_stdout_lines(Broken, sender).join().unwrap();
        assert_eq!(receiver.recv().unwrap(), LineMessage::Eof);
    }

    #[test]
    fn stderr_cap_does_not_prevent_full_drain() {
        let data = Cursor::new("12345\n67890\n");
        assert_eq!(drain_stderr(data, 7).join().unwrap(), "12345\n6");
    }
}
