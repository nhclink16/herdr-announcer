use super::theme;
use crate::config::{Config, load_config};
use crate::config_write::write_config_keys;
use crate::ipc::Client;
use crate::log::{LogEntry, read_log};
use crate::mute::{PaneMute, PaneMutes, list_pane_mutes, remove_pane_mute};
use crate::paths::PLUGIN_ID;
use crate::redact::redact_command;
use crate::snapshot::{self, CAPABILITY_NAMES, STATE_ORDER};
use crate::snooze::{
    SNOOZE_STEPS, format_snooze_remaining, next_snooze_step, read_snooze, set_snooze, snooze_label,
    snooze_message,
};
use crate::speech::capabilities;
use crossterm::event::{
    Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
    poll, read,
};
use jiff::{Timestamp, tz::TimeZone};
use ratatui::Frame;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::io::{self, IsTerminal, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const MIN_WIDTH: u16 = 60;
pub const MIN_HEIGHT: u16 = 16;
pub const MESSAGE_SECONDS: f64 = 5.0;
pub const LOG_LIMIT: usize = 500;
pub const USAGE: &str =
    "usage: herdr-announcer dashboard [snooze 5m|30m|2h|tomorrow|off | toggle-toast | open]";
pub const FOOTER_FULL: &str = "j/k or arrows move · space/enter toggle · s snooze · t test · w wizard · r reload · wheel scrolls log · q quit";
pub const FOOTER_COMPACT: &str =
    "j/k move · space toggle · s snooze · t test · w wizard · r reload · q quit";
pub const FOOTER_MINIMAL: &str = "j/k move · space toggle · s snooze · q quit";

const STATE_HELP: [(&str, &str); 5] = [
    ("done", "an agent finished work you weren't watching"),
    ("blocked", "an agent is waiting on your input"),
    ("idle", "an agent settled while you were watching"),
    ("working", "an agent started doing something (chatty)"),
    ("unknown", "unrecognized agent activity (chatty)"),
];

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum FocusId {
    Snooze,
    State(String),
    Toast,
    AgentMute(String),
    PaneMute(String),
    TestVoice,
    FullSetup,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Hit {
    Focusable(FocusId),
    FooterKey(char),
    LogArea,
}

#[derive(Clone, Debug)]
pub enum Input {
    Key(KeyEvent),
    Mouse(MouseEvent),
    Tick,
    Resize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UpdateResult {
    Continue,
    Quit,
    Wizard,
}

#[derive(Clone, Debug)]
enum MutedRow {
    Agent(String, bool),
    Pane(String, PaneMute),
}

#[derive(Clone, Copy, Debug)]
struct HeightPlan {
    config: bool,
    log_title: bool,
    log_entry_rows: u16,
    log_padding: u16,
    spacer_before_snooze: bool,
    spacer_after_snooze: bool,
    voice: bool,
    tools: bool,
    muted_height: u16,
}

impl HeightPlan {
    fn for_height(height: u16, full_muted_height: u16) -> Self {
        let mut plan = Self {
            config: true,
            log_title: true,
            log_entry_rows: 5,
            log_padding: 0,
            spacer_before_snooze: true,
            spacer_after_snooze: true,
            voice: true,
            tools: true,
            muted_height: full_muted_height,
        };
        let mut used = plan.used_height();
        if used < height {
            plan.log_entry_rows += height - used;
            return plan;
        }

        while used > height && plan.log_entry_rows > 1 {
            plan.log_entry_rows -= 1;
            used -= 1;
        }
        for spacer in [
            &mut plan.spacer_after_snooze,
            &mut plan.spacer_before_snooze,
        ] {
            if used > height {
                *spacer = false;
                used -= 1;
            }
        }
        for optional in [&mut plan.tools, &mut plan.voice, &mut plan.config] {
            if used > height {
                *optional = false;
                used -= 1;
            }
        }
        while used > height && plan.muted_height > 2 {
            plan.muted_height -= 1;
            used -= 1;
        }
        if used > height && plan.muted_height != 0 {
            used -= plan.muted_height;
            plan.muted_height = 0;
        }
        if used > height && plan.log_title {
            plan.log_title = false;
            used -= 1;
        }
        if used > height && plan.log_entry_rows != 0 {
            plan.log_entry_rows = 0;
            used -= 1;
        }

        // Removing a two-row Muted section can leave one spare row. Keep it as
        // empty rail inside Recent without reviving a dropped log entry.
        plan.log_padding = height.saturating_sub(used);
        plan
    }

    fn used_height(self) -> u16 {
        13 + u16::from(self.config)
            + u16::from(self.log_title)
            + self.log_entry_rows
            + u16::from(self.spacer_before_snooze)
            + u16::from(self.spacer_after_snooze)
            + u16::from(self.voice)
            + u16::from(self.tools)
            + self.muted_height
            + self.log_padding
    }

    fn recent_height(self) -> u16 {
        u16::from(self.log_title) + self.log_entry_rows + self.log_padding
    }

    fn controls_height(self) -> u16 {
        8 + u16::from(self.spacer_before_snooze)
            + u16::from(self.spacer_after_snooze)
            + u16::from(self.voice)
            + u16::from(self.tools)
    }
}

pub struct Dashboard {
    pub config_dir: PathBuf,
    pub state_dir: PathBuf,
    pub config: Config,
    pub entries: Vec<LogEntry>,
    pub snooze_until: f64,
    pub pane_mutes: PaneMutes,
    pub pane_agents: BTreeSet<String>,
    pub caps: BTreeMap<String, bool>,
    pub focus: FocusId,
    pub focus_ring: Vec<FocusId>,
    pub offset_from_bottom: usize,
    pub hit_regions: Vec<(Rect, Hit)>,
    pub message: String,
    pub now: f64,
    pub log_region_height: u16,
    log_visible_rows: u16,
    client: Option<Client>,
    message_at: f64,
    tick_count: u64,
    voice_test: Option<Child>,
}

fn wall_time() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

fn clip(value: impl AsRef<str>, width: usize) -> String {
    let plain = value
        .as_ref()
        .replace("\r\n", " ")
        .replace(['\n', '\r', '\t', '\u{0b}', '\u{0c}'], " ");
    if width == 0 {
        return String::new();
    }
    if plain.chars().count() <= width {
        return plain;
    }
    plain.chars().take(width - 1).collect::<String>() + theme::ELLIPSIS
}

fn pad(value: &str, width: usize) -> String {
    let mut value = clip(value, width);
    value.push_str(&" ".repeat(width.saturating_sub(value.chars().count())));
    value
}

fn format_timestamp(timestamp: &str) -> String {
    timestamp.split_once('T').map_or_else(
        || clip(timestamp, 8),
        |(_, clock)| clock.chars().take(8).collect(),
    )
}

fn announce_states(config: &Config) -> Vec<String> {
    let selected: BTreeSet<_> = config
        .get("announce")
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_lowercase)
        .collect();
    STATE_ORDER
        .into_iter()
        .filter(|state| selected.contains(*state))
        .map(ToOwned::to_owned)
        .collect()
}

fn configured_muted_agents(config: &Config) -> BTreeSet<String> {
    config
        .get("mute_agents")
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|name| !name.is_empty())
        .map(str::to_lowercase)
        .collect()
}

fn python_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        _ => value.to_string(),
    }
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&byte))
    {
        return value.to_owned();
    }
    if value.is_empty() {
        return "''".to_owned();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

pub fn voice_backend_label(config: &Config, caps: &BTreeMap<String, bool>) -> String {
    let command = config.get("speak_command");
    if !command.is_null() {
        let Some(command) = command.as_array() else {
            return "custom command  (invalid)".to_owned();
        };
        let command: Vec<_> = command.iter().map(python_string).collect();
        let rendered = redact_command(&command)
            .iter()
            .map(|argument| shell_quote(argument))
            .collect::<Vec<_>>()
            .join(" ");
        return format!("custom command  {}", clip(rendered, 48));
    }
    if config.is_truthy("elevenlabs_api_key") {
        return format!(
            "ElevenLabs  voice {}",
            python_string(config.get("elevenlabs_voice_id"))
        );
    }
    if cfg!(target_os = "macos") {
        let voice = config.string("voice").unwrap_or("");
        if voice.is_empty() {
            return "local say  system voice".to_owned();
        }
        return format!("local say  voice {voice}");
    }
    if cfg!(target_os = "linux") {
        let found = ["spd-say", "espeak-ng", "espeak"]
            .into_iter()
            .filter(|name| caps.get(*name).copied().unwrap_or(false))
            .collect::<Vec<_>>();
        if found.is_empty() {
            return "local  nothing detected!".to_owned();
        }
        return format!("local {}", found.join(" / "));
    }
    format!("unsupported platform: {}", env::consts::OS)
}

pub fn footer_text(width: u16) -> &'static str {
    [FOOTER_FULL, FOOTER_COMPACT, FOOTER_MINIMAL]
        .into_iter()
        .find(|text| text.chars().count() <= usize::from(width))
        .unwrap_or(FOOTER_MINIMAL)
}

fn local_hhmm(until: f64) -> String {
    if !until.is_finite() || until < i64::MIN as f64 || until > i64::MAX as f64 {
        return "--:--".to_owned();
    }
    Timestamp::from_second(until as i64).map_or_else(
        |_| "--:--".to_owned(),
        |stamp| {
            stamp
                .to_zoned(TimeZone::system())
                .strftime("%H:%M")
                .to_string()
        },
    )
}

impl Dashboard {
    pub fn new(config_dir: PathBuf, state_dir: PathBuf, client: Option<Client>) -> Self {
        let now = wall_time();
        let loaded = load_config(&config_dir, &mut Vec::new(), None);
        let unreadable = loaded.is_err();
        let mut dashboard = Self {
            config_dir,
            state_dir,
            config: loaded.unwrap_or_default(),
            entries: Vec::new(),
            snooze_until: 0.0,
            pane_mutes: BTreeMap::new(),
            pane_agents: BTreeSet::new(),
            caps: capabilities(),
            focus: FocusId::Snooze,
            focus_ring: Vec::new(),
            offset_from_bottom: 0,
            hit_regions: Vec::new(),
            message: String::new(),
            now,
            log_region_height: 0,
            log_visible_rows: 0,
            client,
            message_at: 0.0,
            tick_count: 0,
            voice_test: None,
        };
        if unreadable {
            dashboard.set_message("config unreadable - showing the last good values");
        }
        dashboard.refresh(false);
        dashboard.rebuild_focus_ring();
        dashboard
    }

    pub fn from_environment(config_dir: PathBuf, state_dir: PathBuf) -> Self {
        Self::new(config_dir, state_dir, Client::from_env().ok())
    }

    pub fn set_now(&mut self, now: f64) {
        self.now = now;
    }

    pub fn set_message(&mut self, message: impl Into<String>) {
        self.message = message.into();
        self.message_at = if self.message.is_empty() {
            0.0
        } else {
            self.now
        };
    }

    pub fn badge(&self) -> (String, ratatui::style::Color) {
        if announce_states(&self.config).is_empty() {
            ("silent · no states selected".to_owned(), theme::ERR)
        } else if self.snooze_until > self.now {
            (
                format!(
                    "snoozed · {}",
                    format_snooze_remaining(self.snooze_until, self.now)
                ),
                theme::WARN,
            )
        } else {
            ("live".to_owned(), theme::OK)
        }
    }

    fn muted_rows(&self) -> Vec<MutedRow> {
        let muted = configured_muted_agents(&self.config);
        if muted.is_empty() && self.pane_mutes.is_empty() {
            return Vec::new();
        }
        let agents = muted.union(&self.pane_agents).cloned().map(|name| {
            let checked = muted.contains(&name);
            MutedRow::Agent(name, checked)
        });
        let panes = self
            .pane_mutes
            .iter()
            .map(|(pane, entry)| MutedRow::Pane(pane.clone(), entry.clone()));
        agents.chain(panes).collect()
    }

    pub fn rebuild_focus_ring(&mut self) {
        let old = self.focus.clone();
        let old_index = self
            .focus_ring
            .iter()
            .position(|item| item == &old)
            .unwrap_or(0);
        let mut ring = vec![FocusId::Snooze];
        ring.extend(
            STATE_ORDER
                .into_iter()
                .map(|state| FocusId::State(state.to_owned())),
        );
        ring.push(FocusId::Toast);
        for row in self.muted_rows() {
            ring.push(match row {
                MutedRow::Agent(name, _) => FocusId::AgentMute(name),
                MutedRow::Pane(pane, _) => FocusId::PaneMute(pane),
            });
        }
        ring.push(FocusId::TestVoice);
        ring.push(FocusId::FullSetup);
        self.focus = if ring.contains(&old) {
            old
        } else {
            ring[old_index.saturating_sub(1).min(ring.len() - 1)].clone()
        };
        self.focus_ring = ring;
    }

    fn move_focus(&mut self, delta: isize) {
        if self.focus_ring.is_empty() {
            return;
        }
        let current = self
            .focus_ring
            .iter()
            .position(|item| item == &self.focus)
            .unwrap_or(0) as isize;
        let len = self.focus_ring.len() as isize;
        let next = (current + delta).rem_euclid(len) as usize;
        self.focus.clone_from(&self.focus_ring[next]);
        self.set_message("");
    }

    fn save_config(&mut self, updates: &[(&str, Value)]) -> bool {
        match write_config_keys(&self.config_dir, updates) {
            Ok(config) => {
                self.config = config;
                self.rebuild_focus_ring();
                true
            }
            Err(error) => {
                self.set_message(format!(
                    "could not save config: {}",
                    clip(error.to_string(), 60)
                ));
                false
            }
        }
    }

    pub fn toggle_state(&mut self, state: &str) -> bool {
        let current = announce_states(&self.config);
        let adding = !current.iter().any(|item| item == state);
        let chosen: Vec<_> = STATE_ORDER
            .into_iter()
            .filter(|name| {
                if *name == state {
                    adding
                } else {
                    current.iter().any(|item| item == name)
                }
            })
            .collect();
        if !self.save_config(&[("announce", json!(chosen))]) {
            return !adding;
        }
        let states = announce_states(&self.config);
        if states.is_empty() {
            self.set_message("announce: (none) - nothing will speak");
        } else {
            self.set_message(format!("announce: {}", states.join(", ")));
        }
        adding
    }

    pub fn toggle_toast(&mut self) -> bool {
        let value = !self.config.is_truthy("toast");
        if !self.save_config(&[("toast", json!(value))]) {
            return !value;
        }
        self.set_message(if value { "toast on" } else { "toast off" });
        value
    }

    pub fn toggle_agent_mute(&mut self, name: &str) -> bool {
        let mut agents = configured_muted_agents(&self.config);
        let muted = if agents.remove(name) {
            false
        } else {
            agents.insert(name.to_owned());
            true
        };
        let ordered: Vec<_> = agents.into_iter().collect();
        if !self.save_config(&[("mute_agents", json!(ordered))]) {
            return !muted;
        }
        self.set_message(format!(
            "agent {name} {}",
            if muted { "muted" } else { "unmuted" }
        ));
        muted
    }

    pub fn unmute_pane(&mut self, pane_id: &str) -> bool {
        if !remove_pane_mute(&self.state_dir, pane_id) {
            return false;
        }
        if let Some(client) = &self.client {
            let _ = client.report_muted(pane_id, false);
        }
        self.pane_mutes.remove(pane_id);
        self.set_message(format!("pane {pane_id} unmuted"));
        self.rebuild_focus_ring();
        true
    }

    pub fn cycle_snooze(&mut self) -> io::Result<f64> {
        let step = next_snooze_step(self.snooze_until, self.now);
        let until = set_snooze(&self.state_dir, step, self.now)
            .ok_or_else(|| io::Error::other("invalid snooze step"))?;
        self.snooze_until = until;
        self.set_message(snooze_message(until, self.now));
        Ok(until)
    }

    pub fn start_voice_test(&mut self) {
        if self
            .voice_test
            .as_mut()
            .and_then(|child| child.try_wait().ok())
            .is_some_and(|status| status.is_none())
        {
            self.set_message("voice test already running");
            return;
        }
        let executable = match env::current_exe() {
            Ok(path) => path,
            Err(error) => {
                self.set_message(format!(
                    "voice test failed: {}",
                    clip(error.to_string(), 60)
                ));
                return;
            }
        };
        match Command::new(executable)
            .arg("--test")
            .env("HERDR_PLUGIN_CONFIG_DIR", &self.config_dir)
            .env("HERDR_PLUGIN_STATE_DIR", &self.state_dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(child) => {
                self.voice_test = Some(child);
                self.set_message("voice test running…");
            }
            Err(error) => self.set_message(format!(
                "voice test failed: {}",
                clip(error.to_string(), 60)
            )),
        }
    }

    fn poll_voice_test(&mut self) {
        let Some(child) = self.voice_test.as_mut() else {
            return;
        };
        let Ok(Some(status)) = child.try_wait() else {
            return;
        };
        let mut stderr = String::new();
        if let Some(mut pipe) = child.stderr.take() {
            let _ = pipe.read_to_string(&mut stderr);
        }
        if status.success() {
            self.set_message("voice test ok");
        } else {
            let detail = stderr
                .lines()
                .rfind(|line| !line.trim().is_empty())
                .map(ToOwned::to_owned)
                .unwrap_or_else(|| format!("exit {}", status.code().unwrap_or(-1)));
            self.set_message(format!("voice test failed: {}", clip(detail, 60)));
        }
        self.voice_test = None;
    }

    fn refresh_panes(&mut self) {
        let Some(client) = &self.client else {
            return;
        };
        let Ok(panes) = client.pane_list(None) else {
            return;
        };
        self.pane_agents = panes
            .iter()
            .filter_map(|pane| {
                ["agent", "display_agent"].into_iter().find_map(|key| {
                    pane.get(key)
                        .and_then(Value::as_str)
                        .filter(|name| !name.is_empty())
                        .map(str::to_lowercase)
                })
            })
            .collect();
    }

    pub fn refresh(&mut self, refresh_caps: bool) {
        self.now = wall_time();
        match load_config(&self.config_dir, &mut Vec::new(), None) {
            Ok(config) => self.config = config,
            Err(_) => self.set_message("config unreadable - showing the last good values"),
        }
        self.entries = read_log(&self.state_dir.join("announcer.log"), LOG_LIMIT);
        self.snooze_until = read_snooze(&self.state_dir, self.now);
        self.pane_mutes = list_pane_mutes(&self.state_dir, self.now);
        if self.tick_count.is_multiple_of(5) {
            self.refresh_panes();
        }
        self.tick_count = self.tick_count.saturating_add(1);
        if refresh_caps {
            self.caps = capabilities();
        }
        self.poll_voice_test();
        if !self.message.is_empty() && self.now - self.message_at >= MESSAGE_SECONDS {
            self.set_message("");
        }
        self.rebuild_focus_ring();
        self.clamp_scroll();
    }

    fn max_scroll(&self) -> usize {
        let visible = usize::from(self.log_visible_rows);
        self.entries.len().saturating_sub(visible)
    }

    fn clamp_scroll(&mut self) {
        self.offset_from_bottom = self.offset_from_bottom.min(self.max_scroll());
    }

    pub fn scroll_log(&mut self, amount: isize) {
        if amount >= 0 {
            self.offset_from_bottom = self
                .offset_from_bottom
                .saturating_add(amount as usize)
                .min(self.max_scroll());
        } else {
            self.offset_from_bottom = self
                .offset_from_bottom
                .saturating_sub(amount.unsigned_abs());
        }
    }

    fn activate(&mut self) -> UpdateResult {
        match self.focus.clone() {
            FocusId::Snooze => {
                if let Err(error) = self.cycle_snooze() {
                    self.set_message(format!(
                        "could not save snooze: {}",
                        clip(error.to_string(), 60)
                    ));
                }
            }
            FocusId::State(state) => {
                self.toggle_state(&state);
            }
            FocusId::Toast => {
                self.toggle_toast();
            }
            FocusId::AgentMute(name) => {
                self.toggle_agent_mute(&name);
            }
            FocusId::PaneMute(pane) => {
                self.unmute_pane(&pane);
            }
            FocusId::TestVoice => self.start_voice_test(),
            FocusId::FullSetup => {
                self.set_message("opening the setup wizard…");
                return UpdateResult::Wizard;
            }
        }
        UpdateResult::Continue
    }

    fn key(&mut self, event: KeyEvent) -> UpdateResult {
        if event.kind != KeyEventKind::Press {
            return UpdateResult::Continue;
        }
        if event.modifiers.contains(KeyModifiers::CONTROL)
            && event.modifiers.contains(KeyModifiers::ALT)
        {
            return UpdateResult::Continue;
        }
        if event.code == KeyCode::Esc
            || matches!(
                event.code,
                KeyCode::Char('c' | 'd') if event.modifiers.contains(KeyModifiers::CONTROL)
            )
            || (event.code == KeyCode::Char('q') && event.modifiers.is_empty())
        {
            return UpdateResult::Quit;
        }
        match event.code {
            KeyCode::Char('j') | KeyCode::Down | KeyCode::Tab => self.move_focus(1),
            KeyCode::Char('k') | KeyCode::Up | KeyCode::BackTab => self.move_focus(-1),
            KeyCode::Char(' ') | KeyCode::Enter => return self.activate(),
            KeyCode::Char('s') => {
                if let Err(error) = self.cycle_snooze() {
                    self.set_message(format!(
                        "could not save snooze: {}",
                        clip(error.to_string(), 60)
                    ));
                }
            }
            KeyCode::Char('t') => self.start_voice_test(),
            KeyCode::Char('w') => {
                self.set_message("opening the setup wizard…");
                return UpdateResult::Wizard;
            }
            KeyCode::Char('r') => {
                self.refresh(true);
                self.set_message("reloaded");
            }
            KeyCode::PageUp => {
                self.scroll_log(isize::try_from(self.log_region_height.saturating_sub(1)).unwrap())
            }
            KeyCode::PageDown => {
                self.scroll_log(-isize::try_from(self.log_region_height.saturating_sub(1)).unwrap())
            }
            _ => {}
        }
        UpdateResult::Continue
    }

    fn footer_key(&mut self, key: char) -> UpdateResult {
        if key == '↕' {
            self.scroll_log(3);
            return UpdateResult::Continue;
        }
        let event = KeyEvent::new(KeyCode::Char(key), KeyModifiers::NONE);
        self.key(event)
    }

    fn mouse(&mut self, event: MouseEvent) -> UpdateResult {
        match event.kind {
            MouseEventKind::ScrollUp => {
                self.scroll_log(3);
                return UpdateResult::Continue;
            }
            MouseEventKind::ScrollDown => {
                self.scroll_log(-3);
                return UpdateResult::Continue;
            }
            MouseEventKind::Down(MouseButton::Left) => {}
            _ => return UpdateResult::Continue,
        }
        let hit = self
            .hit_regions
            .iter()
            .rev()
            .find(|(rect, _)| rect.contains((event.column, event.row).into()))
            .map(|(_, hit)| hit.clone());
        match hit {
            Some(Hit::Focusable(focus)) => {
                self.focus = focus;
                self.activate()
            }
            Some(Hit::FooterKey('\0')) | Some(Hit::LogArea) | None => UpdateResult::Continue,
            Some(Hit::FooterKey(key)) => self.footer_key(key),
        }
    }

    pub fn update(&mut self, input: Input) -> UpdateResult {
        match input {
            Input::Key(event) => self.key(event),
            Input::Mouse(event) => self.mouse(event),
            Input::Tick => {
                self.refresh(false);
                UpdateResult::Continue
            }
            Input::Resize => UpdateResult::Continue,
        }
    }

    fn render_line(frame: &mut Frame<'_>, area: Rect, line: Line<'static>) {
        if area.height != 0 && area.width != 0 {
            frame.render_widget(Paragraph::new(line), area);
        }
    }

    fn body_prefix() -> Vec<Span<'static>> {
        vec![
            Span::styled(theme::RAIL, Style::default().fg(theme::DIM)),
            Span::raw(" "),
        ]
    }

    fn cursor(&self, focus: &FocusId) -> Span<'static> {
        if &self.focus == focus {
            Span::styled(theme::CURSOR, Style::default().fg(theme::ACCENT))
        } else {
            Span::raw(" ")
        }
    }

    fn focus_row(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        focus: FocusId,
        mut spans: Vec<Span<'static>>,
    ) {
        let mut line = Self::body_prefix();
        line.push(self.cursor(&focus));
        line.append(&mut spans);
        Self::render_line(frame, area, Line::from(line));
        self.hit_regions.push((area, Hit::Focusable(focus)));
    }

    fn render_recent(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        show_title: bool,
        entry_rows: u16,
    ) {
        self.log_region_height = area.height;
        self.log_visible_rows = entry_rows;
        self.clamp_scroll();
        self.hit_regions.push((area, Hit::LogArea));
        if area.height == 0 {
            return;
        }
        for row in 0..area.height {
            Self::render_line(
                frame,
                Rect::new(area.x, area.y + row, area.width, 1),
                Line::from(Self::body_prefix()),
            );
        }
        if show_title {
            let mut title = Self::body_prefix();
            title.push(Span::styled(
                " Recent",
                Style::default().add_modifier(Modifier::BOLD),
            ));
            Self::render_line(
                frame,
                Rect::new(area.x, area.y, area.width, 1),
                Line::from(title),
            );
        }
        let rows = usize::from(entry_rows);
        let shown = rows.min(self.entries.len());
        if show_title && self.offset_from_bottom > 0 {
            let indicator = format!("({shown}/{})", self.entries.len());
            frame.render_widget(
                Paragraph::new(Span::styled(indicator, Style::default().fg(theme::DIM)))
                    .alignment(Alignment::Right),
                Rect::new(area.x, area.y, area.width.saturating_sub(1), 1),
            );
        }
        let body = Rect::new(
            area.x,
            area.bottom().saturating_sub(entry_rows),
            area.width,
            entry_rows,
        );
        if self.entries.is_empty() {
            let mut spans = Self::body_prefix();
            spans.push(Span::styled(
                "   no announcements logged yet",
                Style::default().fg(theme::DIM),
            ));
            Self::render_line(
                frame,
                Rect::new(body.x, body.y, body.width, 1),
                Line::from(spans),
            );
            return;
        }
        let end = self.entries.len().saturating_sub(self.offset_from_bottom);
        let start = end.saturating_sub(shown);
        let top_padding = rows.saturating_sub(shown);
        for (position, entry) in self.entries[start..end].iter().enumerate() {
            let y = body.y + u16::try_from(top_padding + position).unwrap_or(u16::MAX);
            if y >= body.bottom() {
                break;
            }
            let prefix = format!(
                "   {}  {}  {}  ",
                pad(&format_timestamp(&entry.timestamp), 8),
                pad(&entry.status, 8),
                pad(&entry.pane_id, 12)
            );
            let mut action = entry.action.clone();
            if !entry.reasons.is_empty() {
                action.push_str("  (");
                action.push_str(&entry.reasons.join(";"));
                action.push(')');
            }
            let available = usize::from(area.width)
                .saturating_sub(2)
                .saturating_sub(prefix.chars().count());
            let action = clip(action, available);
            let action_color = match entry.action.as_str() {
                "error" => theme::ERR,
                "snoozed" => theme::WARN,
                _ => theme::DIM,
            };
            let mut spans = Self::body_prefix();
            spans.push(Span::styled(prefix, Style::default().fg(theme::DIM)));
            spans.push(Span::styled(action, Style::default().fg(action_color)));
            Self::render_line(
                frame,
                Rect::new(area.x, y, area.width, 1),
                Line::from(spans),
            );
        }
        if self.entries.len() > rows && rows > 0 {
            let mut scrollbar = ScrollbarState::new(self.entries.len())
                .position(
                    self.entries
                        .len()
                        .saturating_sub(self.offset_from_bottom + shown),
                )
                .viewport_content_length(rows);
            frame.render_stateful_widget(
                Scrollbar::new(ScrollbarOrientation::VerticalRight)
                    .begin_symbol(None)
                    .end_symbol(None),
                area,
                &mut scrollbar,
            );
        }
    }

    fn render_controls(&mut self, frame: &mut Frame<'_>, area: Rect, plan: HeightPlan) {
        let mut y = area.y;
        let mut next_row = || {
            let row = Rect::new(area.x, y, area.width, 1);
            y += 1;
            row
        };
        if plan.spacer_before_snooze {
            Self::render_line(frame, next_row(), Line::from(Self::body_prefix()));
        }
        let snooze = FocusId::Snooze;
        let snooze_text = format!(
            "{}{}",
            pad("Snooze", 9),
            snooze_label(self.snooze_until, self.now)
        );
        self.focus_row(
            frame,
            next_row(),
            snooze.clone(),
            vec![
                Span::raw(" "),
                Span::styled(snooze_text, theme::focused(self.focus == snooze)),
                Span::raw("  "),
                Span::styled(
                    format!("s cycles {}", SNOOZE_STEPS.join(" / ")),
                    Style::default().fg(theme::DIM),
                ),
            ],
        );
        if plan.spacer_after_snooze {
            Self::render_line(frame, next_row(), Line::from(Self::body_prefix()));
        }
        let mut announce = Self::body_prefix();
        announce.push(Span::styled(
            " Announce on",
            Style::default().add_modifier(Modifier::BOLD),
        ));
        Self::render_line(frame, next_row(), Line::from(announce));
        let selected = announce_states(&self.config);
        for (state, help) in STATE_HELP {
            let focus = FocusId::State(state.to_owned());
            let checked = selected.iter().any(|item| item == state);
            let glyph = if checked {
                theme::CHECKED
            } else {
                theme::UNCHECKED
            };
            let color = if checked { theme::ACCENT } else { theme::DIM };
            self.focus_row(
                frame,
                next_row(),
                focus.clone(),
                vec![
                    Span::styled(glyph, Style::default().fg(color)),
                    Span::raw(" "),
                    Span::styled(
                        format!("{}{help}", pad(state, 9)),
                        theme::focused(self.focus == focus),
                    ),
                ],
            );
        }
        let toast = FocusId::Toast;
        let checked = self.config.is_truthy("toast");
        self.focus_row(
            frame,
            next_row(),
            toast.clone(),
            vec![
                Span::styled(
                    if checked {
                        theme::CHECKED
                    } else {
                        theme::UNCHECKED
                    },
                    Style::default().fg(if checked { theme::ACCENT } else { theme::DIM }),
                ),
                Span::raw(" "),
                Span::styled(
                    format!(
                        "{}mirror each announcement as a Herdr notification",
                        pad("toast", 9)
                    ),
                    theme::focused(self.focus == toast),
                ),
            ],
        );
        if plan.voice {
            let mut voice = Self::body_prefix();
            voice.push(Span::raw(format!(" {}", pad("voice", 9))));
            voice.push(Span::styled(
                voice_backend_label(&self.config, &self.caps),
                Style::default().fg(theme::DIM),
            ));
            Self::render_line(frame, next_row(), Line::from(voice));
        }
        if plan.tools {
            let mut tools = Self::body_prefix();
            tools.push(Span::raw(format!(" {}", pad("tools", 9))));
            let mut used = 0usize;
            for name in CAPABILITY_NAMES {
                let cost = name.chars().count() + 2 + usize::from(used != 0) * 2;
                if used + cost > usize::from(area.width).saturating_sub(12) {
                    break;
                }
                if used != 0 {
                    tools.push(Span::raw("  "));
                }
                tools.push(Span::raw(format!("{name} ")));
                let found = self.caps.get(name).copied().unwrap_or(false);
                tools.push(Span::styled(
                    if found {
                        theme::OK_MARK
                    } else {
                        theme::ERROR_MARK
                    },
                    Style::default().fg(if found { theme::OK } else { theme::DIM }),
                ));
                used += cost;
            }
            Self::render_line(frame, next_row(), Line::from(tools));
        }
        debug_assert_eq!(y, area.bottom());
    }

    fn render_muted(&mut self, frame: &mut Frame<'_>, area: Rect) {
        if area.height == 0 {
            return;
        }
        let all = self.muted_rows();
        let mut title = Self::body_prefix();
        title.push(Span::styled(
            " Muted",
            Style::default().add_modifier(Modifier::BOLD),
        ));
        Self::render_line(
            frame,
            Rect::new(area.x, area.y, area.width, 1),
            Line::from(title),
        );
        let row_capacity = usize::from(area.height.saturating_sub(1));
        let show_rows = row_capacity.min(4).min(all.len());
        for (index, row) in all.iter().take(show_rows).enumerate() {
            let area = Rect::new(area.x, area.y + 1 + index as u16, area.width, 1);
            match row {
                MutedRow::Agent(name, checked) => {
                    let focus = FocusId::AgentMute(name.clone());
                    self.focus_row(
                        frame,
                        area,
                        focus.clone(),
                        vec![
                            Span::styled(
                                if *checked {
                                    theme::CHECKED
                                } else {
                                    theme::UNCHECKED
                                },
                                Style::default().fg(if *checked {
                                    theme::ACCENT
                                } else {
                                    theme::DIM
                                }),
                            ),
                            Span::raw(" "),
                            Span::styled(
                                format!("{}never announce this agent type", pad(name, 9)),
                                theme::focused(self.focus == focus),
                            ),
                        ],
                    );
                }
                MutedRow::Pane(pane, entry) => {
                    let focus = FocusId::PaneMute(pane.clone());
                    let until = if entry.until == 0.0 {
                        "until closed".to_owned()
                    } else {
                        format!("until {}", local_hhmm(entry.until))
                    };
                    self.focus_row(
                        frame,
                        area,
                        focus.clone(),
                        vec![
                            Span::styled(theme::ERROR_MARK, Style::default().fg(theme::ERR)),
                            Span::raw(" "),
                            Span::styled(
                                format!("{}{}{}", pad(pane, 14), pad(&entry.agent, 9), until),
                                theme::focused(self.focus == focus),
                            ),
                        ],
                    );
                }
            }
        }
        if all.len() > 4 && row_capacity > 4 {
            let mut spans = Self::body_prefix();
            spans.push(Span::styled(
                format!("   {} and {} more", theme::ELLIPSIS, all.len() - 4),
                Style::default().fg(theme::DIM),
            ));
            Self::render_line(
                frame,
                Rect::new(area.x, area.y + 5, area.width, 1),
                Line::from(spans),
            );
        }
    }

    fn render_actions(&mut self, frame: &mut Frame<'_>, area: Rect) {
        for (index, (focus, label, help)) in [
            (
                FocusId::TestVoice,
                "Test voice",
                "speak a sample announcement now",
            ),
            (
                FocusId::FullSetup,
                "Full setup",
                "open the setup wizard (replaces this screen)",
            ),
        ]
        .into_iter()
        .enumerate()
        {
            self.focus_row(
                frame,
                Rect::new(area.x, area.y + index as u16, area.width, 1),
                focus.clone(),
                vec![
                    Span::styled(theme::ACTION, Style::default().fg(theme::ACCENT)),
                    Span::raw(" "),
                    Span::styled(
                        format!("{}{help}", pad(label, 15)),
                        theme::focused(self.focus == focus),
                    ),
                ],
            );
        }
    }

    fn footer_segment_key(segment: &str) -> char {
        if segment.starts_with("j/k") {
            'j'
        } else if segment.starts_with("space") {
            ' '
        } else if segment.starts_with("s ") {
            's'
        } else if segment.starts_with("t ") {
            't'
        } else if segment.starts_with("w ") {
            'w'
        } else if segment.starts_with("r ") {
            'r'
        } else if segment.starts_with("q ") {
            'q'
        } else if segment.starts_with("wheel ") {
            '↕'
        } else {
            '\0'
        }
    }

    fn render_footer(&mut self, frame: &mut Frame<'_>, area: Rect) {
        let footer = footer_text(area.width);
        let footer = clip(footer, usize::from(area.width));
        Self::render_line(
            frame,
            area,
            Line::from(Span::styled(
                footer.clone(),
                Style::default().fg(theme::DIM),
            )),
        );
        let mut x = area.x;
        for segment in footer.split(" · ") {
            let width = u16::try_from(segment.chars().count()).unwrap_or(u16::MAX);
            self.hit_regions.push((
                Rect::new(x, area.y, width.min(area.right().saturating_sub(x)), 1),
                Hit::FooterKey(Self::footer_segment_key(segment)),
            ));
            x = x.saturating_add(width).saturating_add(3);
        }
    }

    pub fn view(&mut self, frame: &mut Frame<'_>) {
        self.hit_regions.clear();
        let area = frame.area();
        if area.width < MIN_WIDTH || area.height < MIN_HEIGHT {
            self.log_region_height = 0;
            self.log_visible_rows = 0;
            Self::render_line(
                frame,
                Rect::new(area.x, area.y, area.width, 1),
                Line::from(format!(
                    "announcer dashboard: terminal too small ({}x{}, need 60x16)",
                    area.width, area.height
                )),
            );
            return;
        }
        let muted_rows = self.muted_rows().len();
        let muted_height = if muted_rows == 0 {
            0
        } else {
            1 + muted_rows.min(4) as u16 + u16::from(muted_rows > 4)
        };
        let plan = HeightPlan::for_height(area.height, muted_height);
        debug_assert_eq!(plan.used_height(), area.height);
        let mut y = area.y;
        let header = Rect::new(area.x, y, area.width, 1);
        y += 1;
        let config = Rect::new(area.x, y, area.width, u16::from(plan.config));
        y += config.height;
        let recent = Rect::new(area.x, y, area.width, plan.recent_height());
        y = y.saturating_add(recent.height);
        let controls = Rect::new(area.x, y, area.width, plan.controls_height());
        y = y.saturating_add(controls.height);
        let muted = Rect::new(area.x, y, area.width, plan.muted_height);
        y = y.saturating_add(muted.height);
        let actions = Rect::new(area.x, y, area.width, 2);
        y = y.saturating_add(actions.height);
        let footer = Rect::new(area.x, y, area.width, 1);
        y = y.saturating_add(footer.height);
        let message = Rect::new(area.x, y, area.width, 1);
        debug_assert_eq!(message.bottom(), area.bottom());

        let (badge, color) = self.badge();
        Self::render_line(
            frame,
            header,
            Line::from(vec![
                Span::styled(theme::DIAMOND, Style::default().fg(theme::ACCENT)),
                Span::raw(" "),
                Span::styled("Announcer", Style::default().add_modifier(Modifier::BOLD)),
                Span::raw("  "),
                Span::styled(badge, Style::default().fg(color)),
            ]),
        );
        let mut config_line = Self::body_prefix();
        config_line.push(Span::styled(
            format!(" config {}", self.config_dir.join("config.toml").display()),
            Style::default().fg(theme::DIM),
        ));
        Self::render_line(frame, config, Line::from(config_line));
        self.render_recent(frame, recent, plan.log_title, plan.log_entry_rows);
        self.render_controls(frame, controls, plan);
        self.render_muted(frame, muted);
        self.render_actions(frame, actions);
        self.render_footer(frame, footer);
        if !self.message.is_empty() {
            Self::render_line(
                frame,
                message,
                Line::from(Span::styled(
                    clip(&self.message, usize::from(message.width)),
                    Style::default().fg(theme::ACCENT),
                )),
            );
        }
    }
}

pub fn snapshot_text(config_dir: &Path, state_dir: &Path) -> String {
    let config = load_config(config_dir, &mut Vec::new(), None).unwrap_or_default();
    let entries = read_log(&state_dir.join("announcer.log"), LOG_LIMIT);
    let now = wall_time();
    snapshot::render(
        &config_dir.join("config.toml"),
        &config,
        &entries,
        read_snooze(state_dir, now),
        &capabilities(),
        now,
    )
}

pub fn run(config_dir: PathBuf, state_dir: PathBuf) -> io::Result<i32> {
    let mut guard = super::TerminalGuard::enter()?;
    let backend = CrosstermBackend::new(io::stdout());
    let mut terminal = ratatui::Terminal::new(backend)?;
    let mut dashboard = Dashboard::from_environment(config_dir.clone(), state_dir.clone());
    let result = loop {
        terminal.draw(|frame| dashboard.view(frame))?;
        let update = if poll(Duration::from_secs(1))? {
            match read()? {
                Event::Key(event) => dashboard.update(Input::Key(event)),
                Event::Mouse(event) => dashboard.update(Input::Mouse(event)),
                Event::Resize(_, _) => dashboard.update(Input::Resize),
                _ => UpdateResult::Continue,
            }
        } else {
            dashboard.update(Input::Tick)
        };
        match update {
            UpdateResult::Continue => {}
            UpdateResult::Quit => break 0,
            UpdateResult::Wizard => break 1,
        }
    };
    super::drain_pending_events();
    if result == 1 {
        guard.restore();
        use std::os::unix::process::CommandExt;
        let error = Command::new(env::current_exe()?)
            .arg("setup")
            .env("HERDR_PLUGIN_CONFIG_DIR", config_dir)
            .env("HERDR_PLUGIN_STATE_DIR", state_dir)
            .exec();
        eprintln!("dashboard error: {error}");
        return Ok(1);
    }
    Ok(result)
}

pub struct SubcommandResult {
    pub code: u8,
    pub stdout: String,
    pub stderr: String,
}

impl SubcommandResult {
    fn ok(stdout: impl Into<String>) -> Self {
        Self {
            code: 0,
            stdout: stdout.into(),
            stderr: String::new(),
        }
    }

    fn error(code: u8, stderr: impl Into<String>) -> Self {
        Self {
            code,
            stdout: String::new(),
            stderr: stderr.into(),
        }
    }
}

pub fn subcommand(args: &[String], config_dir: &Path, state_dir: &Path) -> SubcommandResult {
    if let Err(error) = fs::create_dir_all(state_dir) {
        return SubcommandResult::error(1, format!("dashboard error: {error}\n"));
    }
    match args {
        [command, spec] if command == "snooze" => {
            let allowed = ["5m", "30m", "2h", "tomorrow", "off"];
            if !allowed.contains(&spec.as_str()) {
                return SubcommandResult::error(2, format!("{USAGE}\n"));
            }
            match set_snooze(state_dir, spec, wall_time()) {
                Some(0.0) => SubcommandResult::ok("snooze off\n"),
                Some(until) => {
                    SubcommandResult::ok(format!("snoozed until {} ({spec})\n", local_hhmm(until)))
                }
                None => SubcommandResult::error(1, "dashboard error: could not save snooze\n"),
            }
        }
        [command] if command == "toggle-toast" => {
            let config = match load_config(config_dir, &mut Vec::new(), None) {
                Ok(config) => config,
                Err(error) => {
                    return SubcommandResult::error(1, format!("dashboard error: {error}\n"));
                }
            };
            let value = !config.is_truthy("toast");
            match write_config_keys(config_dir, &[("toast", json!(value))]) {
                Ok(_) => SubcommandResult::ok(if value { "toast on\n" } else { "toast off\n" }),
                Err(error) => SubcommandResult::error(1, format!("dashboard error: {error}\n")),
            }
        }
        [command] if command == "open" => {
            let client = match Client::from_env() {
                Ok(client) => client,
                Err(error) => {
                    return SubcommandResult::error(1, format!("dashboard error: {error}\n"));
                }
            };
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let target = env::var("HERDR_PANE_ID").ok();
            match client.plugin_pane_open(PLUGIN_ID, "dashboard", target.as_deref(), &cwd, true) {
                Ok(_) => SubcommandResult::ok(""),
                Err(error) => SubcommandResult::error(1, format!("dashboard error: {error}\n")),
            }
        }
        _ => SubcommandResult::error(2, format!("{USAGE}\n")),
    }
}

pub fn stdout_is_tty() -> bool {
    io::stdout().is_terminal()
}

pub fn exit_code(value: u8) -> ExitCode {
    ExitCode::from(value)
}
