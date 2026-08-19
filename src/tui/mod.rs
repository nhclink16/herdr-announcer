pub mod dashboard;
pub mod theme;
pub mod widgets;
pub mod wizard;

use crossterm::cursor::{Hide, Show};
use crossterm::event::{DisableMouseCapture, EnableMouseCapture, poll, read};
use crossterm::execute;
use crossterm::style::force_color_output;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use std::io::{self, stdout};
use std::panic;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

static TERMINAL_ACTIVE: AtomicBool = AtomicBool::new(false);
type PanicHook = Box<dyn Fn(&panic::PanicHookInfo<'_>) + Send + Sync + 'static>;

pub fn restore_terminal() {
    if TERMINAL_ACTIVE.swap(false, Ordering::SeqCst) {
        let _ = execute!(stdout(), LeaveAlternateScreen);
        let _ = execute!(stdout(), DisableMouseCapture);
        let _ = execute!(stdout(), Show);
        let _ = disable_raw_mode();
    }
}

/// Discard input that was already queued for the dashboard before raw mode is
/// released. Without this, rapid navigation keys immediately followed by quit
/// can be delivered to the shell that regains the terminal.
pub fn drain_pending_events() {
    for _ in 0..256 {
        match poll(Duration::ZERO) {
            Ok(true) => {
                if read().is_err() {
                    break;
                }
            }
            Ok(false) | Err(_) => break,
        }
    }
}

pub struct TerminalGuard {
    previous_hook: Option<PanicHook>,
}

impl TerminalGuard {
    pub fn enter() -> io::Result<Self> {
        force_color_output(true);
        execute!(stdout(), EnterAlternateScreen)?;
        if let Err(error) = enable_raw_mode() {
            let _ = execute!(stdout(), LeaveAlternateScreen);
            return Err(error);
        }
        if let Err(error) = execute!(stdout(), EnableMouseCapture, Hide) {
            let _ = disable_raw_mode();
            let _ = execute!(stdout(), LeaveAlternateScreen);
            return Err(error);
        }
        TERMINAL_ACTIVE.store(true, Ordering::SeqCst);
        let previous = panic::take_hook();
        panic::set_hook(Box::new(|information| {
            restore_terminal();
            eprintln!("{information}");
        }));
        Ok(Self {
            previous_hook: Some(previous),
        })
    }

    pub fn restore(&mut self) {
        restore_terminal();
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        restore_terminal();
        if !std::thread::panicking()
            && let Some(previous) = self.previous_hook.take()
        {
            let _ = panic::take_hook();
            panic::set_hook(previous);
        }
    }
}
