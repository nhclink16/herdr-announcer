use herdr_announcer::event::{event_payload, parse_event, payload_string};
use serde_json::Value;

fn captured_event(fixture: &str) -> String {
    let capture: Value = serde_json::from_str(fixture).unwrap();
    serde_json::to_string(&capture["event_json"]).unwrap()
}

#[test]
fn phase_zero_status_changed_envelopes_parse() {
    for (fixture, expected) in [
        (
            include_str!("fixtures/hook-pane-agent-status-changed.json"),
            ("w7:p1", "working"),
        ),
        (
            include_str!("fixtures/hook-pane-agent-status-changed-done.json"),
            ("w7:p1", "done"),
        ),
    ] {
        let raw = captured_event(fixture);
        assert_eq!(
            parse_event(&raw).unwrap(),
            (expected.0.to_owned(), expected.1.to_owned())
        );
    }
}

#[test]
fn phase_zero_detected_initial_and_release_are_distinct_after_unwrap() {
    let initial = event_payload(&captured_event(include_str!(
        "fixtures/hook-pane-agent-detected.json"
    )))
    .unwrap();
    assert_eq!(payload_string(&initial, "pane_id"), Some("w7:p1"));
    assert_eq!(payload_string(&initial, "agent"), Some("contract-probe"));
    assert!(initial.get("released").is_none());

    let released_raw = captured_event(include_str!(
        "fixtures/hook-pane-agent-detected-released.json"
    ));
    let released = event_payload(&released_raw).unwrap();
    assert_eq!(released["released"], true);
    assert_eq!(released["final_status"], "unknown");
    assert_eq!(
        parse_event(&released_raw).unwrap_err(),
        "event payload has no string agent_status"
    );
}

#[test]
fn phase_zero_closed_and_exited_envelopes_retain_flat_pane_ids() {
    for (fixture, pane_id) in [
        (include_str!("fixtures/hook-pane-closed.json"), "w7:p2"),
        (include_str!("fixtures/hook-pane-exited.json"), "w7:p1"),
    ] {
        let raw = captured_event(fixture);
        let payload = event_payload(&raw).unwrap();
        assert_eq!(payload_string(&payload, "pane_id"), Some(pane_id));
        assert_eq!(
            parse_event(&raw).unwrap_err(),
            "event payload has no string agent_status"
        );
    }
}
