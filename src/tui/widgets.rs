use super::theme;
use crossterm::cursor::{Hide, Show};
use crossterm::event::{Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, read};
use crossterm::execute;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode};
use ratatui::backend::CrosstermBackend;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Widget};
use ratatui::{Terminal, TerminalOptions, Viewport};
use std::collections::BTreeSet;
use std::io::{self, BufRead, IsTerminal, Stdout, Write};

pub const INLINE_HEIGHT: u16 = 14;
pub const SELECT_FOOTER: &str = "↑↓ move · enter select · q quit · esc/Ctrl-C quit";
pub const MULTI_HINT: &str = "space toggles, enter confirms";
pub const MULTI_FOOTER: &str = "↑↓ move · space toggle · enter confirm · q quit · esc/Ctrl-C quit";
pub const TEXT_FOOTER: &str = "enter confirms · esc/Ctrl-C quit";

#[derive(Debug)]
pub enum PromptError {
    Abort,
    Io(io::Error),
}

impl From<io::Error> for PromptError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl std::fmt::Display for PromptError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Abort => formatter.write_str("setup aborted"),
            Self::Io(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for PromptError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WidgetResult<T> {
    Continue,
    Complete(T, String),
    Abort,
}

fn pressed(event: &KeyEvent) -> bool {
    event.kind == KeyEventKind::Press
}

fn ctrl_c(event: &KeyEvent) -> bool {
    event.code == KeyCode::Char('c') && event.modifiers.contains(KeyModifiers::CONTROL)
}

#[derive(Clone, Debug)]
pub struct SelectState {
    pub options: Vec<(String, String)>,
    pub index: usize,
    default_value: String,
}

impl SelectState {
    pub fn new(options: &[(&str, &str)], default_value: &str) -> Self {
        let index = options
            .iter()
            .position(|(value, _)| *value == default_value)
            .unwrap_or(0);
        Self {
            options: options
                .iter()
                .map(|(value, label)| ((*value).to_owned(), (*label).to_owned()))
                .collect(),
            index,
            default_value: default_value.to_owned(),
        }
    }

    pub fn update(&mut self, event: KeyEvent) -> WidgetResult<(String, bool)> {
        if !pressed(&event) {
            return WidgetResult::Continue;
        }
        if ctrl_c(&event)
            || event.code == KeyCode::Esc
            || (event.code == KeyCode::Char('q') && event.modifiers.is_empty())
        {
            return WidgetResult::Abort;
        }
        match event.code {
            KeyCode::Up | KeyCode::Char('k') => {
                self.index = self.index.checked_sub(1).unwrap_or(self.options.len() - 1);
            }
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Tab => {
                self.index = (self.index + 1) % self.options.len();
            }
            KeyCode::Char(digit) if digit.is_ascii_digit() => {
                let selected = digit.to_digit(10).unwrap_or(0) as usize;
                if selected != 0 && selected <= self.options.len() {
                    self.index = selected - 1;
                }
            }
            KeyCode::Enter => {
                let (value, label) = &self.options[self.index];
                let summary = label.split("  ").next().unwrap_or(label).trim().to_owned();
                return WidgetResult::Complete(
                    (value.clone(), value != &self.default_value),
                    summary,
                );
            }
            _ => {}
        }
        WidgetResult::Continue
    }
}

#[derive(Clone, Debug)]
pub struct MultiSelectState {
    pub options: Vec<(String, String)>,
    pub index: usize,
    pub selected: BTreeSet<String>,
    default_selected: BTreeSet<String>,
}

impl MultiSelectState {
    pub fn new(options: &[(&str, &str)], default_selected: &[String]) -> Self {
        let selected: BTreeSet<String> = default_selected.iter().cloned().collect();
        Self {
            options: options
                .iter()
                .map(|(value, label)| ((*value).to_owned(), (*label).to_owned()))
                .collect(),
            index: 0,
            default_selected: selected.clone(),
            selected,
        }
    }

    pub fn update(&mut self, event: KeyEvent) -> WidgetResult<(Vec<String>, bool)> {
        if !pressed(&event) {
            return WidgetResult::Continue;
        }
        if ctrl_c(&event)
            || event.code == KeyCode::Esc
            || (event.code == KeyCode::Char('q') && event.modifiers.is_empty())
        {
            return WidgetResult::Abort;
        }
        match event.code {
            KeyCode::Up | KeyCode::Char('k') => {
                self.index = self.index.checked_sub(1).unwrap_or(self.options.len() - 1);
            }
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Tab => {
                self.index = (self.index + 1) % self.options.len();
            }
            KeyCode::Char(' ') => {
                let value = self.options[self.index].0.clone();
                if !self.selected.remove(&value) {
                    self.selected.insert(value);
                }
            }
            KeyCode::Enter if !self.selected.is_empty() => {
                let ordered: Vec<_> = self
                    .options
                    .iter()
                    .filter(|(value, _)| self.selected.contains(value))
                    .map(|(value, _)| value.clone())
                    .collect();
                return WidgetResult::Complete(
                    (ordered.clone(), self.selected != self.default_selected),
                    ordered.join(", "),
                );
            }
            _ => {}
        }
        WidgetResult::Continue
    }
}

#[derive(Clone, Debug)]
pub struct LineState {
    pub entered: Vec<char>,
    default: String,
    shown_default: String,
    secret: bool,
}

impl LineState {
    pub fn new(default: &str, display_default: Option<&str>, secret: bool) -> Self {
        Self {
            entered: Vec::new(),
            default: default.to_owned(),
            shown_default: display_default.unwrap_or(default).to_owned(),
            secret,
        }
    }

    pub fn update(&mut self, event: KeyEvent) -> WidgetResult<(String, bool)> {
        if !pressed(&event) {
            return WidgetResult::Continue;
        }
        if ctrl_c(&event) || event.code == KeyCode::Esc {
            return WidgetResult::Abort;
        }
        match event.code {
            KeyCode::Backspace => {
                self.entered.pop();
            }
            KeyCode::Enter | KeyCode::Char('d')
                if event.code == KeyCode::Enter
                    || event.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                let raw = self.entered.iter().collect::<String>();
                let value = raw.trim();
                let unchanged = value.is_empty()
                    || (!self.secret
                        && self.shown_default != self.default
                        && value == self.shown_default);
                let result = if unchanged {
                    self.default.clone()
                } else {
                    value.to_owned()
                };
                let changed = !unchanged;
                let summary = if !changed {
                    if self.shown_default.is_empty() {
                        "(blank)".to_owned()
                    } else {
                        self.shown_default.clone()
                    }
                } else if self.secret || self.shown_default != self.default {
                    "(updated)".to_owned()
                } else if result.is_empty() {
                    "(blank)".to_owned()
                } else {
                    result.clone()
                };
                return WidgetResult::Complete((result, changed), summary);
            }
            KeyCode::Char(character) if !event.modifiers.contains(KeyModifiers::CONTROL) => {
                self.entered.push(character);
            }
            _ => {}
        }
        WidgetResult::Continue
    }

    fn visible(&self) -> String {
        if self.secret {
            "•".repeat(self.entered.len())
        } else {
            self.entered.iter().collect()
        }
    }
}

fn rail() -> Span<'static> {
    Span::styled(theme::RAIL, Style::default().fg(theme::DIM))
}

fn title_line(title: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(theme::DIAMOND, Style::default().fg(theme::ACCENT)),
        Span::raw(" "),
        Span::styled(
            title.to_owned(),
            Style::default().add_modifier(Modifier::BOLD),
        ),
    ])
}

fn footer_line(text: &'static str) -> Line<'static> {
    Line::from(vec![
        rail(),
        Span::raw("  "),
        Span::styled(text, Style::default().fg(theme::DIM)),
    ])
}

fn bottom_line() -> Line<'static> {
    Line::from(Span::styled(
        theme::RAIL_BOTTOM,
        Style::default().fg(theme::DIM),
    ))
}

pub fn select_lines(title: &str, hint: &str, state: &SelectState) -> Vec<Line<'static>> {
    let mut lines = vec![title_line(title)];
    if !hint.is_empty() {
        lines.push(Line::from(vec![
            rail(),
            Span::raw("  "),
            Span::styled(hint.to_owned(), Style::default().fg(theme::DIM)),
        ]));
    }
    for (index, (_, label)) in state.options.iter().enumerate() {
        if index == state.index {
            lines.push(Line::from(vec![
                rail(),
                Span::raw("  "),
                Span::styled(theme::SELECTED, Style::default().fg(theme::ACCENT)),
                Span::raw(" "),
                Span::styled(label.clone(), Style::default().add_modifier(Modifier::BOLD)),
            ]));
        } else {
            lines.push(Line::from(vec![
                rail(),
                Span::raw("  "),
                Span::styled(
                    format!("{} {label}", theme::UNSELECTED),
                    Style::default().fg(theme::DIM),
                ),
            ]));
        }
    }
    lines.push(footer_line(SELECT_FOOTER));
    lines.push(bottom_line());
    lines
}

pub fn multiselect_lines(title: &str, hint: &str, state: &MultiSelectState) -> Vec<Line<'static>> {
    let mut lines = vec![title_line(title)];
    lines.push(Line::from(vec![
        rail(),
        Span::raw("  "),
        Span::styled(hint.to_owned(), Style::default().fg(theme::DIM)),
    ]));
    for (index, (value, label)) in state.options.iter().enumerate() {
        let focused = index == state.index;
        lines.push(Line::from(vec![
            rail(),
            Span::raw(" "),
            if focused {
                Span::styled(theme::CURSOR, Style::default().fg(theme::ACCENT))
            } else {
                Span::raw(" ")
            },
            Span::styled(
                if state.selected.contains(value) {
                    theme::CHECKED
                } else {
                    theme::UNCHECKED
                },
                Style::default().fg(if state.selected.contains(value) {
                    theme::ACCENT
                } else {
                    theme::DIM
                }),
            ),
            Span::raw(" "),
            Span::styled(
                label.clone(),
                if focused {
                    Style::default().add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme::DIM)
                },
            ),
        ]));
    }
    lines.push(footer_line(MULTI_FOOTER));
    lines.push(bottom_line());
    lines
}

pub fn text_lines(title: &str, state: &LineState) -> Vec<Line<'static>> {
    let default_hint = if state.shown_default.is_empty() {
        String::new()
    } else {
        format!("[{}] ", state.shown_default)
    };
    vec![
        title_line(title),
        Line::from(vec![
            rail(),
            Span::raw("  "),
            Span::styled(default_hint, Style::default().fg(theme::DIM)),
            Span::raw("> "),
            Span::raw(state.visible()),
        ]),
        footer_line(TEXT_FOOTER),
        bottom_line(),
    ]
}

pub fn collapse_line(title: &str, answer: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(theme::DIAMOND_EMPTY, Style::default().fg(theme::DIM)),
        Span::raw(" "),
        Span::styled(title.to_owned(), Style::default().fg(theme::DIM)),
        Span::styled(" · ", Style::default().fg(theme::DIM)),
        Span::styled(answer.to_owned(), Style::default().fg(theme::ACCENT)),
    ])
}

pub trait PromptUi {
    fn is_fancy(&self) -> bool;
    fn print_line(&mut self, line: &str) -> Result<(), PromptError>;

    fn print_title(&mut self, title: &str) -> Result<(), PromptError> {
        self.print_line(title)
    }

    fn print_blank(&mut self) -> Result<(), PromptError> {
        self.print_line("")
    }

    fn select(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default_value: &str,
        hint: &str,
    ) -> Result<(String, bool), PromptError>;

    fn multiselect(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default_selected: &[String],
        hint: &str,
    ) -> Result<(Vec<String>, bool), PromptError>;

    fn confirm(&mut self, title: &str, default: bool) -> Result<(bool, bool), PromptError>;

    fn text(
        &mut self,
        title: &str,
        default: &str,
        display_default: Option<&str>,
    ) -> Result<(String, bool), PromptError>;

    fn secret(
        &mut self,
        title: &str,
        default: &str,
        display_default: Option<&str>,
    ) -> Result<(String, bool), PromptError>;
}

struct RawPromptGuard;

impl RawPromptGuard {
    fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        if let Err(error) = execute!(io::stdout(), Hide) {
            let _ = disable_raw_mode();
            return Err(error);
        }
        Ok(Self)
    }
}

struct RawInputGuard;

impl RawInputGuard {
    fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        Ok(Self)
    }
}

impl Drop for RawInputGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
    }
}

impl Drop for RawPromptGuard {
    fn drop(&mut self) {
        let _ = execute!(io::stdout(), Show);
        let _ = disable_raw_mode();
    }
}

pub struct TtyPrompter {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TtyPrompter {
    pub fn new() -> io::Result<Self> {
        let terminal = Terminal::with_options(
            CrosstermBackend::new(io::stdout()),
            TerminalOptions {
                viewport: Viewport::Inline(INLINE_HEIGHT),
            },
        )?;
        Ok(Self { terminal })
    }

    fn clear_viewport(&mut self) -> io::Result<()> {
        self.terminal.draw(|_| {}).map(|_| ())
    }

    fn collapse(&mut self, title: &str, answer: &str) -> io::Result<()> {
        self.terminal.insert_before(1, |buffer| {
            collapse_line(title, answer).render(buffer.area, buffer);
        })?;
        self.clear_viewport()
    }

    fn run_widget<T>(
        &mut self,
        mut render: impl FnMut() -> Vec<Line<'static>>,
        mut update: impl FnMut(KeyEvent) -> WidgetResult<T>,
        title: &str,
    ) -> Result<T, PromptError> {
        let guard = RawPromptGuard::enter()?;
        let result = loop {
            let lines = render();
            self.terminal
                .draw(|frame| frame.render_widget(Paragraph::new(lines), frame.area()))?;
            let Event::Key(event) = read()? else {
                continue;
            };
            match update(event) {
                WidgetResult::Continue => {}
                WidgetResult::Complete(value, answer) => {
                    self.collapse(title, &answer)?;
                    break Ok(value);
                }
                WidgetResult::Abort => {
                    self.clear_viewport()?;
                    break Err(PromptError::Abort);
                }
            }
        };
        drop(guard);
        result
    }
}

impl Drop for TtyPrompter {
    fn drop(&mut self) {
        let _ = self.terminal.draw(|frame| {
            let area = frame.area();
            frame.set_cursor_position((area.x, area.y));
        });
        let _ = execute!(io::stdout(), Show);
        let _ = disable_raw_mode();
        let _ = writeln!(io::stdout());
    }
}

impl PromptUi for TtyPrompter {
    fn is_fancy(&self) -> bool {
        true
    }

    fn print_line(&mut self, line: &str) -> Result<(), PromptError> {
        let height = u16::try_from(line.lines().count().max(1)).unwrap_or(u16::MAX);
        let text = line.to_owned();
        self.terminal.insert_before(height, move |buffer| {
            Paragraph::new(text).render(buffer.area, buffer);
        })?;
        self.clear_viewport()?;
        Ok(())
    }

    fn print_title(&mut self, title: &str) -> Result<(), PromptError> {
        let title = title.to_owned();
        self.terminal.insert_before(1, move |buffer| {
            Line::from(Span::styled(
                title,
                Style::default().add_modifier(Modifier::BOLD),
            ))
            .render(buffer.area, buffer);
        })?;
        self.clear_viewport()?;
        Ok(())
    }

    fn select(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default_value: &str,
        hint: &str,
    ) -> Result<(String, bool), PromptError> {
        let state = std::cell::RefCell::new(SelectState::new(options, default_value));
        self.run_widget(
            || select_lines(title, hint, &state.borrow()),
            |event| state.borrow_mut().update(event),
            title,
        )
    }

    fn multiselect(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default_selected: &[String],
        hint: &str,
    ) -> Result<(Vec<String>, bool), PromptError> {
        let state = std::cell::RefCell::new(MultiSelectState::new(options, default_selected));
        self.run_widget(
            || multiselect_lines(title, hint, &state.borrow()),
            |event| state.borrow_mut().update(event),
            title,
        )
    }

    fn confirm(&mut self, title: &str, default: bool) -> Result<(bool, bool), PromptError> {
        let default_value = if default { "yes" } else { "no" };
        let (value, _) = self.select(title, &[("yes", "Yes"), ("no", "No")], default_value, "")?;
        let result = value == "yes";
        Ok((result, result != default))
    }

    fn text(
        &mut self,
        title: &str,
        default: &str,
        display_default: Option<&str>,
    ) -> Result<(String, bool), PromptError> {
        let state = std::cell::RefCell::new(LineState::new(default, display_default, false));
        self.run_widget(
            || text_lines(title, &state.borrow()),
            |event| state.borrow_mut().update(event),
            title,
        )
    }

    fn secret(
        &mut self,
        title: &str,
        default: &str,
        display_default: Option<&str>,
    ) -> Result<(String, bool), PromptError> {
        let state = std::cell::RefCell::new(LineState::new(default, display_default, true));
        self.run_widget(
            || text_lines(title, &state.borrow()),
            |event| state.borrow_mut().update(event),
            title,
        )
    }
}

pub struct LinePrompter<R, W> {
    input: R,
    output: W,
    secure_secret: bool,
}

impl<R, W> LinePrompter<R, W> {
    pub fn new(input: R, output: W, secure_secret: bool) -> Self {
        Self {
            input,
            output,
            secure_secret,
        }
    }

    pub fn into_parts(self) -> (R, W) {
        (self.input, self.output)
    }
}

impl<R: BufRead, W: Write> LinePrompter<R, W> {
    fn prompt(&mut self, label: &str, default: &str) -> Result<(String, bool), PromptError> {
        write!(self.output, "{label} [{default}]: ")?;
        self.output.flush()?;
        let mut value = String::new();
        if self.input.read_line(&mut value)? == 0 {
            return Ok((default.to_owned(), false));
        }
        if value.contains('\x03') {
            return Err(PromptError::Abort);
        }
        let value = value.trim();
        if value.is_empty() {
            Ok((default.to_owned(), false))
        } else {
            Ok((value.to_owned(), true))
        }
    }

    fn secure_line(&mut self) -> Result<Option<String>, PromptError> {
        if !self.secure_secret || !io::stdin().is_terminal() {
            return Ok(None);
        }
        let guard = RawInputGuard::enter()?;
        let mut entered = Vec::new();
        let result = loop {
            let Event::Key(event) = read()? else {
                continue;
            };
            if !pressed(&event) {
                continue;
            }
            if ctrl_c(&event) {
                break Err(PromptError::Abort);
            }
            match event.code {
                KeyCode::Enter => break Ok(Some(entered.iter().collect())),
                KeyCode::Backspace => {
                    entered.pop();
                }
                KeyCode::Char('d') if event.modifiers.contains(KeyModifiers::CONTROL) => {
                    break Ok(Some(entered.iter().collect()));
                }
                KeyCode::Char(character) if !event.modifiers.contains(KeyModifiers::CONTROL) => {
                    entered.push(character);
                }
                _ => {}
            }
        };
        drop(guard);
        result
    }
}

impl<R: BufRead, W: Write> PromptUi for LinePrompter<R, W> {
    fn is_fancy(&self) -> bool {
        false
    }

    fn print_line(&mut self, line: &str) -> Result<(), PromptError> {
        writeln!(self.output, "{line}")?;
        self.output.flush()?;
        Ok(())
    }

    fn select(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default_value: &str,
        _hint: &str,
    ) -> Result<(String, bool), PromptError> {
        self.print_line(title)?;
        let default_number = options
            .iter()
            .position(|(value, _)| *value == default_value)
            .map_or(1, |index| index + 1);
        for (index, (_, label)) in options.iter().enumerate() {
            self.print_line(&format!("  {}) {label}", index + 1))?;
        }
        loop {
            let (value, explicit) = self.prompt("  choice", &default_number.to_string())?;
            if value.eq_ignore_ascii_case("q") {
                return Err(PromptError::Abort);
            }
            if let Ok(index) = value.parse::<usize>()
                && let Some((selected, _)) = options.get(index.saturating_sub(1))
                && index != 0
            {
                return Ok(((*selected).to_owned(), explicit));
            }
            self.print_line(&format!(
                "Please choose {}.",
                (1..=options.len())
                    .map(|value| value.to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            ))?;
        }
    }

    fn multiselect(
        &mut self,
        title: &str,
        options: &[(&str, &str)],
        default_selected: &[String],
        _hint: &str,
    ) -> Result<(Vec<String>, bool), PromptError> {
        let valid: BTreeSet<_> = options.iter().map(|(value, _)| *value).collect();
        loop {
            let (value, explicit) = self.prompt(
                &format!("{title} (comma list)"),
                &default_selected.join(","),
            )?;
            if value.eq_ignore_ascii_case("q") {
                return Err(PromptError::Abort);
            }
            let picked: Vec<_> = value
                .split(',')
                .map(|value| value.trim().to_lowercase())
                .filter(|value| !value.is_empty())
                .collect();
            if !picked.is_empty() && picked.iter().all(|value| valid.contains(value.as_str())) {
                return Ok((picked, explicit));
            }
            self.print_line(&format!(
                "  Choose from: {}.",
                valid.iter().copied().collect::<Vec<_>>().join(", ")
            ))?;
        }
    }

    fn confirm(&mut self, title: &str, default: bool) -> Result<(bool, bool), PromptError> {
        loop {
            let (value, explicit) = self.prompt(title, if default { "Y/n" } else { "y/N" })?;
            if !explicit {
                return Ok((default, false));
            }
            if value.eq_ignore_ascii_case("q") {
                return Err(PromptError::Abort);
            }
            if matches!(value.to_lowercase().as_str(), "y" | "yes") {
                return Ok((true, true));
            }
            if matches!(value.to_lowercase().as_str(), "n" | "no") {
                return Ok((false, true));
            }
            self.print_line("Please enter yes or no.")?;
        }
    }

    fn text(
        &mut self,
        title: &str,
        default: &str,
        display_default: Option<&str>,
    ) -> Result<(String, bool), PromptError> {
        let shown = display_default.unwrap_or(default);
        let (value, explicit) = self.prompt(title, shown)?;
        if display_default.is_some() && value == shown {
            Ok((default.to_owned(), false))
        } else {
            Ok((value, explicit))
        }
    }

    fn secret(
        &mut self,
        title: &str,
        default: &str,
        display_default: Option<&str>,
    ) -> Result<(String, bool), PromptError> {
        let shown = display_default.unwrap_or("");
        if shown.is_empty() {
            write!(self.output, "{title}: ")?;
        } else {
            write!(self.output, "{title} [{shown}]: ")?;
        }
        self.output.flush()?;
        let raw = if let Some(value) = self.secure_line()? {
            writeln!(self.output)?;
            value
        } else {
            let mut value = String::new();
            self.input.read_line(&mut value)?;
            value
        };
        if raw.contains('\x03') {
            return Err(PromptError::Abort);
        }
        let value = raw.trim();
        if value.is_empty() {
            Ok((default.to_owned(), false))
        } else {
            Ok((value.to_owned(), true))
        }
    }
}
