use herdr_announcer::actions::process_action;
use herdr_announcer::cli;
use herdr_announcer::config::load_config;
use herdr_announcer::hook::{LogContext, persist_last_error, process_cleanup, process_invocation};
use herdr_announcer::log::log_invocation;
use herdr_announcer::paths::resolve_dirs;
use herdr_announcer::tui::dashboard;
use herdr_announcer::tui::wizard;
use std::backtrace::Backtrace;
use std::fs;
use std::io::{IsTerminal, Read};
use std::process::ExitCode;
use std::time::Instant;

const USAGE: &str = "usage: herdr-announcer [--test | status | setup | dashboard [snooze <spec>|toggle-toast|open] | action <id> | cleanup]";

fn short_error(error: &str) -> String {
    let detail = error.trim();
    if detail.is_empty() {
        "error".to_owned()
    } else {
        detail.chars().take(120).collect()
    }
}

fn raw_event_from_environment() -> Result<String, String> {
    if let Ok(value) = std::env::var("HERDR_PLUGIN_EVENT_JSON") {
        return Ok(value);
    }
    let mut value = String::new();
    std::io::stdin()
        .read_to_string(&mut value)
        .map_err(|error| error.to_string())?;
    Ok(value)
}

fn finish_result(
    config_dir: &std::path::Path,
    state_dir: &std::path::Path,
    context: LogContext,
    mut reasons: Vec<String>,
    result: Result<Option<String>, String>,
    started: Instant,
) -> u8 {
    let redaction_config = load_config(config_dir, &mut Vec::new(), None).ok();
    if let Some(config) = &redaction_config {
        for reason in &mut reasons {
            *reason = cli::redact_configured_text(reason, config);
        }
    }
    match result {
        Ok(None) => 0,
        Ok(Some(action)) => {
            if let Err(error) = persist_last_error(state_dir, &reasons).and_then(|()| {
                log_invocation(
                    state_dir,
                    &context.pane_id,
                    &context.status,
                    &action,
                    started.elapsed().as_secs_f64(),
                    "",
                    &reasons,
                )
            }) {
                eprintln!("announcer error: {error}");
                return 1;
            }
            0
        }
        Err(error) => {
            let error = redaction_config.as_ref().map_or(error.clone(), |config| {
                cli::redact_configured_text(&error, config)
            });
            reasons.push(format!("error: {}", short_error(&error)));
            let trace = format!(
                "Error: {error}\nBacktrace:\n{}\n",
                Backtrace::force_capture()
            );
            let _ = persist_last_error(state_dir, &reasons);
            let _ = log_invocation(
                state_dir,
                &context.pane_id,
                &context.status,
                "error",
                started.elapsed().as_secs_f64(),
                &trace,
                &reasons,
            );
            eprintln!("announcer error: {error}");
            1
        }
    }
}

fn run_pipeline(test_mode: bool, started: Instant) -> u8 {
    let (config_dir, state_dir) = resolve_dirs();
    let mut context = if test_mode {
        LogContext::test()
    } else {
        LogContext::hook()
    };
    let mut reasons = Vec::new();
    let raw_event = if test_mode {
        None
    } else {
        match raw_event_from_environment() {
            Ok(value) => Some(value),
            Err(error) => {
                eprintln!("announcer error: {error}");
                return 1;
            }
        }
    };
    if let Err(error) = fs::create_dir_all(&state_dir) {
        eprintln!("announcer error: {error}");
        return 1;
    }
    let result = process_invocation(
        &config_dir,
        &state_dir,
        test_mode,
        raw_event.as_deref(),
        &mut context,
        &mut reasons,
    )
    .map(|action| (!action.is_empty()).then_some(action));
    finish_result(&config_dir, &state_dir, context, reasons, result, started)
}

fn run_action(action_id: &str, started: Instant) -> u8 {
    let (config_dir, state_dir) = resolve_dirs();
    if let Err(error) = fs::create_dir_all(&state_dir) {
        eprintln!("announcer error: {error}");
        return 1;
    }
    let mut context = LogContext::hook();
    let mut reasons = Vec::new();
    let raw_context = std::env::var("HERDR_PLUGIN_CONTEXT_JSON").ok();
    let result = process_action(
        &config_dir,
        &state_dir,
        action_id,
        raw_context.as_deref(),
        &mut context,
        &mut reasons,
    )
    .map(Some);
    finish_result(&config_dir, &state_dir, context, reasons, result, started)
}

fn run_cleanup(started: Instant) -> u8 {
    let (config_dir, state_dir) = resolve_dirs();
    let raw_event = match raw_event_from_environment() {
        Ok(value) => value,
        Err(error) => {
            eprintln!("announcer error: {error}");
            return 1;
        }
    };
    let mut context = LogContext::hook();
    let mut reasons = Vec::new();
    let result = process_cleanup(&state_dir, &raw_event, &mut context, &mut reasons);
    finish_result(&config_dir, &state_dir, context, reasons, result, started)
}

fn run_dashboard(args: &[String]) -> u8 {
    let (config_dir, state_dir) = resolve_dirs();
    if args.is_empty() {
        if dashboard::stdout_is_tty() {
            match crossterm::terminal::size() {
                Ok((columns, rows))
                    if columns >= dashboard::MIN_WIDTH && rows >= dashboard::MIN_HEIGHT =>
                {
                    return match dashboard::run(config_dir, state_dir) {
                        Ok(code) => code as u8,
                        Err(error) => {
                            eprintln!("dashboard error: {error}");
                            1
                        }
                    };
                }
                _ => eprintln!(
                    "dashboard: this terminal is smaller than 60x16; showing a static snapshot"
                ),
            }
        }
        print!("{}", dashboard::snapshot_text(&config_dir, &state_dir));
        return 0;
    }
    let result = dashboard::subcommand(args, &config_dir, &state_dir);
    print!("{}", result.stdout);
    eprint!("{}", result.stderr);
    result.code
}

fn run() -> u8 {
    let started = Instant::now();
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [command] if command == "status" => {
            let (config_dir, state_dir) = resolve_dirs();
            match cli::status(&config_dir, &state_dir) {
                Ok(output) => {
                    print!("{output}");
                    0
                }
                Err(error) => {
                    eprintln!("announcer error: {error}");
                    1
                }
            }
        }
        [flag] if flag == "--help" || flag == "-h" => {
            println!("{USAGE}");
            0
        }
        [] if std::io::stdin().is_terminal() => {
            eprintln!("{USAGE}");
            2
        }
        [] => run_pipeline(false, started),
        [flag] if flag == "--test" => run_pipeline(true, started),
        [command] if command == "setup" => {
            let (config_dir, state_dir) = resolve_dirs();
            match wizard::run_setup(&config_dir, &state_dir) {
                Ok(code) => code,
                Err(error) => {
                    eprintln!("announcer error: {error}");
                    1
                }
            }
        }
        [command] if command == "cleanup" => run_cleanup(started),
        [command] if command == "dashboard" => run_dashboard(&[]),
        [command, rest @ ..] if command == "dashboard" => run_dashboard(rest),
        [command, action]
            if command == "action"
                && matches!(
                    action.as_str(),
                    "mute-pane" | "snooze-pane" | "announce-now"
                ) =>
        {
            run_action(action, started)
        }
        _ => {
            eprintln!("{USAGE}");
            2
        }
    }
}

fn main() -> ExitCode {
    ExitCode::from(run())
}
