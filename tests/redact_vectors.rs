use herdr_announcer::redact::{mask_secret, redact_command, redact_command_text};
use serde::Deserialize;

#[derive(Deserialize)]
struct MaskVector {
    input: String,
    expected: String,
}

#[derive(Deserialize)]
struct CommandVector {
    input: Vec<String>,
    expected: Vec<String>,
}

#[derive(Deserialize)]
struct TextVector {
    command: Vec<String>,
    input: String,
    expected: String,
}

#[derive(Deserialize)]
struct Vectors {
    mask: Vec<MaskVector>,
    commands: Vec<CommandVector>,
    texts: Vec<TextVector>,
}

fn expand(value: &str) -> String {
    value.replace("<REPO>", env!("CARGO_MANIFEST_DIR"))
}

#[test]
fn shared_redaction_vectors_match_python() {
    let vectors: Vectors =
        serde_json::from_str(include_str!("fixtures/redact-vectors.json")).unwrap();

    for vector in vectors.mask {
        assert_eq!(mask_secret(&vector.input), vector.expected);
    }
    for vector in vectors.commands {
        let input: Vec<_> = vector.input.iter().map(|value| expand(value)).collect();
        let expected: Vec<_> = vector.expected.iter().map(|value| expand(value)).collect();
        assert_eq!(redact_command(&input), expected, "input: {input:?}");
    }
    for vector in vectors.texts {
        assert_eq!(
            redact_command_text(&vector.input, &vector.command),
            vector.expected,
            "input: {}",
            vector.input
        );
    }
}
