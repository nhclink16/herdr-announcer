use herdr_announcer::config::load_config;
use herdr_announcer::config_write::write_config_keys;
use herdr_announcer::summarize::ANNOUNCEMENT_PROMPT;
use herdr_announcer::tui::widgets::{LinePrompter, PromptError, PromptUi};
use herdr_announcer::tui::wizard::{
    claude_summary_command, config_lines, preview_line, run_with_line_io_opts, run_with_ui_opts,
};
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::io::Cursor;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

fn caps(codex: bool, claude: bool) -> BTreeMap<String, bool> {
    [
        ("codex", codex),
        ("claude", claude),
        ("say", false),
        ("spd-say", false),
        ("espeak-ng", false),
        ("espeak", false),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .collect()
}

fn run(root: &Path, input: &str, detected: &BTreeMap<String, bool>) -> (u8, String) {
    let (result, _, output) = run_with_line_io_opts(
        Cursor::new(input.as_bytes().to_vec()),
        Vec::new(),
        &root.join("config"),
        &root.join("state"),
        detected,
        false,
    );
    (
        result.unwrap(),
        String::from_utf8(output).expect("wizard output is UTF-8"),
    )
}

#[test]
fn non_tty_template_local_flow_writes_chosen_keys_and_exact_signoff() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(temp.path(), "\n2\n\n\n\n\nn\n", &caps(true, false));
    assert_eq!(code, 0);
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string("summary"), Some("template"));
    assert!(loaded.get("speak_command").is_null());
    assert_eq!(loaded.string("elevenlabs_api_key"), Some(""));
    assert!(output.starts_with("herdr-announcer setup\nConfig: "));
    assert!(output.contains("Enter keeps the value in [brackets]. q quits choices; Ctrl-C exits without writing anything."));
    assert!(
        output.ends_with("Done. Re-run this wizard anytime; the file is safe to hand-edit too.\n")
    );
    assert!(!output.contains("do not preserve comments"));
}

#[test]
fn decline_write_exits_zero_and_creates_no_config() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(temp.path(), "\n2\n\n\n\nn\n", &caps(true, false));
    assert_eq!(code, 0);
    assert!(output.ends_with("Nothing written.\n"));
    assert!(!temp.path().join("config/config.toml").exists());
}

#[test]
fn abort_before_write_exits_130_without_touching_disk() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(temp.path(), "q\n", &caps(true, false));
    assert_eq!(code, 130);
    assert!(output.ends_with("\nsetup aborted, nothing written\n"));
    assert!(!temp.path().join("config/config.toml").exists());
}

#[test]
fn invalid_states_reprompt_with_authoritative_sorted_message() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(
        temp.path(),
        "bogus\ndone,blocked\n2\n\n\n\nn\n",
        &caps(true, false),
    );
    assert_eq!(code, 0);
    assert!(output.contains("  Choose from: blocked, done, idle, unknown, working."));
}

#[test]
fn codex_model_effort_style_states_toast_and_debounce_are_chosen() {
    let temp = tempfile::tempdir().unwrap();
    let (code, _) = run(
        temp.path(),
        "done,idle\n1\ngpt-x\n2\n2\n1\ny\n9\ny\nn\n",
        &caps(true, false),
    );
    assert_eq!(code, 0);
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string_array("announce").unwrap(), ["done", "idle"]);
    assert_eq!(loaded.string("codex_model"), Some("gpt-x"));
    assert_eq!(loaded.string("codex_effort"), Some("medium"));
    assert_eq!(loaded.string("style"), Some("summary"));
    assert!(loaded.is_truthy("toast"));
    assert_eq!(loaded.get("debounce_seconds"), &json!(9));
}

#[test]
fn claude_option_installs_the_documented_command() {
    let temp = tempfile::tempdir().unwrap();
    let (code, _) = run(temp.path(), "\n1\n1\n1\n\n\n\n\nn\n", &caps(false, true));
    assert_eq!(code, 0);
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string("summary"), Some("command"));
    assert_eq!(
        loaded.string_array("summary_command").unwrap(),
        claude_summary_command()
    );
    assert_eq!(
        claude_summary_command().last().unwrap(),
        &format!("{ANNOUNCEMENT_PROMPT} The terminal output is provided on stdin.")
    );
}

#[test]
fn existing_summary_command_gets_keep_custom_option() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    fs::create_dir_all(&config_dir).unwrap();
    fs::write(
        config_dir.join("config.toml"),
        "summary = \"command\"\nsummary_command = [\"mine\"]\n",
    )
    .unwrap();
    let (code, output) = run(temp.path(), "\n1\n1\n1\n\n\n\nn\n", &caps(false, true));
    assert_eq!(code, 0);
    assert!(output.contains("Custom - keep your current summary command"));
}

#[test]
fn blank_custom_prompt_falls_back_to_announcement_style() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(
        temp.path(),
        "\n1\n\n\n3\n\n1\n\n\n\n\nn\n",
        &caps(true, false),
    );
    assert_eq!(code, 0);
    assert!(output.contains("No template given - keeping announcement style."));
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string("style"), Some("announcement"));
}

#[test]
fn fresh_voice_choices_omit_keep_but_existing_choices_include_it() {
    let fresh = tempfile::tempdir().unwrap();
    let (_, fresh_output) = run(fresh.path(), "\n2\n1\n\n\n\nn\n", &caps(true, false));
    assert!(!fresh_output.contains("Keep current voice settings"));

    let existing = tempfile::tempdir().unwrap();
    fs::create_dir_all(existing.path().join("config")).unwrap();
    fs::write(
        existing.path().join("config/config.toml"),
        "summary = \"template\"\n",
    )
    .unwrap();
    let (_, existing_output) = run(existing.path(), "\n\n\n\n\nn\n", &caps(true, false));
    assert!(existing_output.contains("Keep current voice settings"));
}

#[test]
fn elevenlabs_flow_masks_preview_but_writes_the_real_secret() {
    let temp = tempfile::tempdir().unwrap();
    let secret = "super-secret-1234";
    let input = format!("\n2\n2\n{secret}\nvoice-x\nmodel-x\n\n\n\n\nn\n");
    let (code, output) = run(temp.path(), &input, &caps(true, false));
    assert_eq!(code, 0);
    assert!(!output.contains(secret));
    assert!(output.contains("****1234"));
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string("elevenlabs_api_key"), Some(secret));
    assert_eq!(loaded.string("elevenlabs_voice_id"), Some("voice-x"));
    assert_eq!(loaded.string("elevenlabs_model"), Some("model-x"));
}

#[test]
fn custom_command_reprompts_after_shell_words_error_and_preserves_argv() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(
        temp.path(),
        "\n2\n3\n'unterminated\nhelper --flag 'two words'\n\n\n\n\nn\n",
        &caps(true, false),
    );
    assert_eq!(code, 0);
    assert!(output.contains("Invalid command: "));
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(
        loaded.string_array("speak_command").unwrap(),
        ["helper", "--flag", "two words"]
    );
}

#[test]
fn debounce_reprompts_until_a_nonnegative_integer() {
    let temp = tempfile::tempdir().unwrap();
    let (code, output) = run(
        temp.path(),
        "\n2\n\n\n-1\nnope\n7\n\nn\n",
        &caps(true, false),
    );
    assert_eq!(code, 0);
    assert_eq!(
        output
            .matches("Please enter a non-negative integer.")
            .count(),
        2
    );
    let loaded = load_config(&temp.path().join("config"), &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.get("debounce_seconds"), &json!(7));
}

#[test]
fn empty_preview_and_backup_notice_are_exact_for_existing_defaults() {
    let temp = tempfile::tempdir().unwrap();
    fs::create_dir_all(temp.path().join("config")).unwrap();
    fs::write(temp.path().join("config/config.toml"), "").unwrap();
    let (code, output) = run(temp.path(), "\n\n\n\n\n\n\n\nn\n", &caps(true, false));
    assert_eq!(code, 0);
    assert!(
        output.contains("  (empty file - everything matches the defaults)"),
        "{output}"
    );
    assert!(output.contains("Your current file will be kept as config.toml.bak."));
}

#[test]
fn preview_helpers_use_default_order_and_mask_only_the_api_key() {
    let temp = tempfile::tempdir().unwrap();
    let config = load_config(temp.path(), &mut Vec::new(), None).unwrap();
    assert_eq!(config_lines(&config, &[]).unwrap(), Vec::<String>::new());
    assert_eq!(
        preview_line("elevenlabs_api_key = \"super-secret-1234\""),
        "elevenlabs_api_key = \"****1234\""
    );
    assert_eq!(preview_line("voice = \"Alex\""), "voice = \"Alex\"");
}

#[test]
fn comments_unknown_keys_tables_and_backup_survive_wizard_write() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    fs::create_dir_all(&config_dir).unwrap();
    let original =
        "# hand comment\nsummary = \"template\"\nfuture_key = 1\n[future]\nname = \"kept\"\n";
    fs::write(config_dir.join("config.toml"), original).unwrap();
    let (code, _) = run(temp.path(), "done\n\n\n\n\n\n\nn\n", &caps(true, false));
    assert_eq!(code, 0);
    let text = fs::read_to_string(config_dir.join("config.toml")).unwrap();
    assert!(text.contains("# hand comment"));
    assert!(text.contains("future_key = 1"));
    assert!(text.contains("[future]\nname = \"kept\""));
    assert_eq!(
        fs::read_to_string(config_dir.join("config.toml.bak")).unwrap(),
        original
    );
}

#[test]
fn abort_after_write_restores_config_backup_and_modes() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    fs::create_dir_all(&config_dir).unwrap();
    let config_path = config_dir.join("config.toml");
    let backup_path = config_dir.join("config.toml.bak");
    let original = b"# exact original\nsummary = \"template\"\n";
    let backup = b"# prior backup\nsummary = \"command\"\n";
    fs::write(&config_path, original).unwrap();
    fs::write(&backup_path, backup).unwrap();
    fs::set_permissions(&config_path, fs::Permissions::from_mode(0o640)).unwrap();
    fs::set_permissions(&backup_path, fs::Permissions::from_mode(0o600)).unwrap();
    let (code, output) = run(temp.path(), "done\n\n\n\n\n\nq\n", &caps(true, false));
    assert_eq!(code, 130);
    assert!(output.contains("setup aborted, nothing written"));
    assert_eq!(fs::read(&config_path).unwrap(), original);
    assert_eq!(fs::read(&backup_path).unwrap(), backup);
    assert_eq!(
        fs::metadata(&config_path).unwrap().permissions().mode() & 0o7777,
        0o640
    );
    assert_eq!(
        fs::metadata(&backup_path).unwrap().permissions().mode() & 0o7777,
        0o600
    );
}

#[test]
fn fresh_install_abort_after_write_leaves_no_config_or_backup() {
    let temp = tempfile::tempdir().unwrap();
    let (code, _) = run(temp.path(), "\n2\n\n\n\n\nq\n", &caps(true, false));
    assert_eq!(code, 130);
    assert!(!temp.path().join("config/config.toml").exists());
    assert!(!temp.path().join("config/config.toml.bak").exists());
}

struct ConcurrentChangeUi {
    inner: LinePrompter<Cursor<Vec<u8>>, Vec<u8>>,
    config_dir: PathBuf,
}

impl PromptUi for ConcurrentChangeUi {
    fn is_fancy(&self) -> bool {
        self.inner.is_fancy()
    }
    fn print_line(&mut self, line: &str) -> Result<(), PromptError> {
        self.inner.print_line(line)
    }
    fn select(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default: &str,
        hint: &str,
    ) -> Result<(String, bool), PromptError> {
        self.inner.select(title, options, default, hint)
    }
    fn multiselect(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default: &[String],
        hint: &str,
    ) -> Result<(Vec<String>, bool), PromptError> {
        self.inner.multiselect(title, options, default, hint)
    }
    fn confirm(&mut self, title: &str, default: bool) -> Result<(bool, bool), PromptError> {
        if title == "Test the voice now?" {
            write_config_keys(&self.config_dir, &[("voice", json!("Alex"))]).unwrap();
            return Err(PromptError::Abort);
        }
        self.inner.confirm(title, default)
    }
    fn text(
        &mut self,
        title: &str,
        default: &str,
        display: Option<&str>,
    ) -> Result<(String, bool), PromptError> {
        self.inner.text(title, default, display)
    }
    fn secret(
        &mut self,
        title: &str,
        default: &str,
        display: Option<&str>,
    ) -> Result<(String, bool), PromptError> {
        self.inner.secret(title, default, display)
    }
}

#[test]
fn abort_keeps_a_newer_concurrent_config_change() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    fs::create_dir_all(&config_dir).unwrap();
    fs::write(config_dir.join("config.toml"), "summary = \"template\"\n").unwrap();
    let mut ui = ConcurrentChangeUi {
        inner: LinePrompter::new(Cursor::new(b"\n\n\n\n\n\n".to_vec()), Vec::new(), false),
        config_dir: config_dir.clone(),
    };
    let code = run_with_ui_opts(
        &mut ui,
        &config_dir,
        &temp.path().join("state"),
        &caps(true, false),
        false,
    )
    .unwrap();
    assert_eq!(code, 130);
    let loaded = load_config(&config_dir, &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string("summary"), Some("template"));
    assert_eq!(loaded.string("voice"), Some("Alex"));
    let (_, output) = ui.inner.into_parts();
    assert!(
        String::from_utf8(output)
            .unwrap()
            .contains("config changed concurrently and was kept")
    );
}

#[test]
fn stale_disk_change_before_write_is_rebased_not_overwritten() {
    let temp = tempfile::tempdir().unwrap();
    let config_dir = temp.path().join("config");
    fs::create_dir_all(&config_dir).unwrap();
    fs::write(
        config_dir.join("config.toml"),
        "summary = \"template\"\ntoast = false\n",
    )
    .unwrap();
    write_config_keys(&config_dir, &[("voice", json!("Alex"))]).unwrap();
    let (code, _) = run(temp.path(), "done\n\n\n\n\n\n\nn\n", &caps(true, false));
    assert_eq!(code, 0);
    let loaded = load_config(&config_dir, &mut Vec::new(), None).unwrap();
    assert_eq!(loaded.string("voice"), Some("Alex"));
    assert_eq!(loaded.string_array("announce").unwrap(), ["done"]);
}
