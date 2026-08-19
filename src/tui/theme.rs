use ratatui::style::{Color, Modifier, Style};

pub const DIAMOND: &str = "◆";
pub const DIAMOND_EMPTY: &str = "◇";
pub const RAIL: &str = "│";
pub const RAIL_BOTTOM: &str = "└";
pub const CURSOR: &str = "❯";
pub const CHECKED: &str = "◼";
pub const UNCHECKED: &str = "◻";
pub const ACTION: &str = "▸";
pub const SELECTED: &str = "●";
pub const UNSELECTED: &str = "○";
pub const OK_MARK: &str = "✓";
pub const ERROR_MARK: &str = "✗";
pub const DOT: &str = "·";
pub const ELLIPSIS: &str = "…";

pub const ACCENT: Color = Color::Cyan;
pub const DIM: Color = Color::DarkGray;
pub const OK: Color = Color::Green;
pub const WARN: Color = Color::Yellow;
pub const ERR: Color = Color::Red;

pub fn focused(focused: bool) -> Style {
    if focused {
        Style::default().add_modifier(Modifier::BOLD)
    } else {
        Style::default()
    }
}
