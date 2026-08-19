use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use herdr_announcer::tui::widgets::{
    LinePrompter, LineState, MULTI_FOOTER, MultiSelectState, PromptError, PromptUi, SELECT_FOOTER,
    TEXT_FOOTER, WidgetResult, collapse_line, multiselect_lines, select_lines, text_lines,
};
use std::io::Cursor;

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn text_of(lines: &[ratatui::text::Line<'_>]) -> Vec<String> {
    lines.iter().map(ToString::to_string).collect()
}

#[test]
fn select_navigation_wraps_and_digits_jump() {
    let mut state = herdr_announcer::tui::widgets::SelectState::new(
        &[("one", "One"), ("two", "Two"), ("three", "Three")],
        "one",
    );
    state.update(key(KeyCode::Up));
    assert_eq!(state.index, 2);
    state.update(key(KeyCode::Down));
    assert_eq!(state.index, 0);
    state.update(key(KeyCode::Tab));
    assert_eq!(state.index, 1);
    state.update(key(KeyCode::Char('3')));
    assert_eq!(state.index, 2);
}

#[test]
fn select_enter_returns_value_changed_and_collapsed_label() {
    let mut state = herdr_announcer::tui::widgets::SelectState::new(
        &[("one", "One  detail"), ("two", "Two")],
        "one",
    );
    state.update(key(KeyCode::Down));
    assert_eq!(
        state.update(key(KeyCode::Enter)),
        WidgetResult::Complete(("two".to_owned(), true), "Two".to_owned())
    );
}

#[test]
fn choice_widgets_abort_on_q_escape_and_ctrl_c() {
    for event in [
        key(KeyCode::Char('q')),
        key(KeyCode::Esc),
        KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
    ] {
        let mut select = herdr_announcer::tui::widgets::SelectState::new(&[("one", "One")], "one");
        assert_eq!(select.update(event), WidgetResult::Abort);
        let mut multi = MultiSelectState::new(&[("one", "One")], &["one".to_owned()]);
        assert_eq!(multi.update(event), WidgetResult::Abort);
    }
}

#[test]
fn multiselect_toggles_refuses_empty_and_returns_option_order() {
    let mut state = MultiSelectState::new(
        &[("done", "Done"), ("blocked", "Blocked")],
        &["done".to_owned()],
    );
    state.update(key(KeyCode::Char(' ')));
    assert_eq!(state.update(key(KeyCode::Enter)), WidgetResult::Continue);
    state.update(key(KeyCode::Down));
    state.update(key(KeyCode::Char(' ')));
    state.update(key(KeyCode::Up));
    state.update(key(KeyCode::Char(' ')));
    assert_eq!(
        state.update(key(KeyCode::Enter)),
        WidgetResult::Complete(
            (vec!["done".to_owned(), "blocked".to_owned()], true),
            "done, blocked".to_owned()
        )
    );
}

#[test]
fn text_q_is_literal_and_unicode_backspace_is_character_based() {
    let mut state = LineState::new("default", None, false);
    for character in ['q', 'a', 'é'] {
        state.update(key(KeyCode::Char(character)));
    }
    state.update(key(KeyCode::Backspace));
    assert_eq!(
        state.update(key(KeyCode::Enter)),
        WidgetResult::Complete(("qa".to_owned(), true), "qa".to_owned())
    );
}

#[test]
fn ctrl_d_accepts_text_and_secret_defaults() {
    for secret in [false, true] {
        let mut state = LineState::new("default", Some("****ault"), secret);
        assert_eq!(
            state.update(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL)),
            WidgetResult::Complete(("default".to_owned(), false), "****ault".to_owned())
        );
    }
}

#[test]
fn line_widgets_abort_only_on_escape_and_ctrl_c() {
    for event in [
        key(KeyCode::Esc),
        KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
    ] {
        let mut state = LineState::new("", None, false);
        assert_eq!(state.update(event), WidgetResult::Abort);
    }
}

#[test]
fn select_visuals_and_footer_are_verbatim() {
    let state = herdr_announcer::tui::widgets::SelectState::new(
        &[("one", "Selected label"), ("two", "other label")],
        "one",
    );
    assert_eq!(
        text_of(&select_lines("Title", "hint", &state)),
        vec![
            "◆ Title",
            "│  hint",
            "│  ● Selected label",
            "│  ○ other label",
            &format!("│  {SELECT_FOOTER}"),
            "└",
        ]
    );
}

#[test]
fn multiselect_visuals_and_footer_are_verbatim() {
    let state = MultiSelectState::new(
        &[("one", "Selected label"), ("two", "other label")],
        &["one".to_owned()],
    );
    assert_eq!(
        text_of(&multiselect_lines(
            "Title",
            "space toggles, enter confirms",
            &state
        )),
        vec![
            "◆ Title",
            "│  space toggles, enter confirms",
            "│ ❯◼ Selected label",
            "│  ◻ other label",
            &format!("│  {MULTI_FOOTER}"),
            "└",
        ]
    );
}

#[test]
fn text_secret_and_collapse_visuals_are_verbatim() {
    let mut state = LineState::new("secret", Some("****cret"), true);
    state.update(key(KeyCode::Char('x')));
    assert_eq!(
        text_of(&text_lines("Secret", &state)),
        vec![
            "◆ Secret",
            "│  [****cret] > •",
            &format!("│  {TEXT_FOOTER}"),
            "└",
        ]
    );
    assert_eq!(
        collapse_line("Secret", "(updated)").to_string(),
        "◇ Secret · (updated)"
    );
}

#[test]
fn non_tty_select_reprompts_with_exact_numbered_strings() {
    let input = Cursor::new(b"nope\n2\n".to_vec());
    let mut ui = LinePrompter::new(input, Vec::new(), false);
    let answer = ui
        .select("Pick", &[("one", "One"), ("two", "Two")], "one", "")
        .unwrap();
    let (_, output) = ui.into_parts();
    assert_eq!(answer, ("two".to_owned(), true));
    assert_eq!(
        String::from_utf8(output).unwrap(),
        "Pick\n  1) One\n  2) Two\n  choice [1]: Please choose 1, 2.\n  choice [1]: "
    );
}

#[test]
fn non_tty_multiselect_reprompts_with_sorted_exact_choices() {
    let input = Cursor::new(b"bogus\ndone,blocked\n".to_vec());
    let mut ui = LinePrompter::new(input, Vec::new(), false);
    let answer = ui
        .multiselect(
            "When should it speak?",
            &[
                ("done", "Done"),
                ("blocked", "Blocked"),
                ("idle", "Idle"),
                ("working", "Working"),
                ("unknown", "Unknown"),
            ],
            &["done".to_owned()],
            "",
        )
        .unwrap();
    let (_, output) = ui.into_parts();
    assert_eq!(answer.0, vec!["done", "blocked"]);
    assert!(
        String::from_utf8(output)
            .unwrap()
            .contains("  Choose from: blocked, done, idle, unknown, working.")
    );
}

#[test]
fn non_tty_confirm_reprompts_and_q_aborts() {
    let mut ui = LinePrompter::new(Cursor::new(b"maybe\ny\n"), Vec::new(), false);
    assert_eq!(ui.confirm("Continue?", false).unwrap(), (true, true));
    let (_, output) = ui.into_parts();
    assert_eq!(
        String::from_utf8(output).unwrap(),
        "Continue? [y/N]: Please enter yes or no.\nContinue? [y/N]: "
    );
    let mut ui = LinePrompter::new(Cursor::new(b"q\n"), Vec::new(), false);
    assert!(matches!(
        ui.confirm("Continue?", true),
        Err(PromptError::Abort)
    ));
}

#[test]
fn non_tty_q_is_text_and_secret_but_literal_ctrl_c_aborts_all() {
    let mut text_ui = LinePrompter::new(Cursor::new(b"q\n"), Vec::new(), false);
    assert_eq!(
        text_ui.text("Name", "default", None).unwrap(),
        ("q".to_owned(), true)
    );
    let mut secret_ui = LinePrompter::new(Cursor::new(b"q\n"), Vec::new(), false);
    assert_eq!(
        secret_ui.secret("Secret", "", None).unwrap(),
        ("q".to_owned(), true)
    );

    for prompt in 0..4 {
        let mut ui = LinePrompter::new(Cursor::new(b"typed\x03\n"), Vec::new(), false);
        let result = match prompt {
            0 => ui.select("Pick", &[("one", "One")], "one", "").map(|_| ()),
            1 => ui
                .multiselect("Pick", &[("one", "One")], &["one".to_owned()], "")
                .map(|_| ()),
            2 => ui.text("Name", "default", None).map(|_| ()),
            _ => ui.secret("Secret", "", None).map(|_| ()),
        };
        assert!(matches!(result, Err(PromptError::Abort)));
    }
}
