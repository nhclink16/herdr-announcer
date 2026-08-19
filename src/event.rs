use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::Value;
use std::fmt;

pub const VALID_STATUSES: [&str; 5] = ["idle", "working", "blocked", "done", "unknown"];

#[derive(Clone, Debug, PartialEq)]
enum OrderedValue {
    Object(Vec<(String, OrderedValue)>),
    Array(Vec<OrderedValue>),
    String(String),
    Other,
}

struct OrderedVisitor;

impl<'de> Visitor<'de> for OrderedVisitor {
    type Value = OrderedValue;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a JSON value")
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some((key, value)) = map.next_entry()? {
            values.push((key, value));
        }
        Ok(OrderedValue::Object(values))
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some(value) = sequence.next_element()? {
            values.push(value);
        }
        Ok(OrderedValue::Array(values))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E> {
        Ok(OrderedValue::String(value.to_owned()))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(OrderedValue::String(value))
    }

    fn visit_bool<E>(self, _value: bool) -> Result<Self::Value, E> {
        Ok(OrderedValue::Other)
    }

    fn visit_i64<E>(self, _value: i64) -> Result<Self::Value, E> {
        Ok(OrderedValue::Other)
    }

    fn visit_u64<E>(self, _value: u64) -> Result<Self::Value, E> {
        Ok(OrderedValue::Other)
    }

    fn visit_f64<E>(self, _value: f64) -> Result<Self::Value, E> {
        Ok(OrderedValue::Other)
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(OrderedValue::Other)
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(OrderedValue::Other)
    }
}

impl<'de> Deserialize<'de> for OrderedValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_any(OrderedVisitor)
    }
}

fn direct_string<'a>(value: &'a OrderedValue, key: &str) -> Option<&'a str> {
    let OrderedValue::Object(values) = value else {
        return None;
    };
    values.iter().find_map(|(candidate, value)| {
        (candidate == key)
            .then_some(value)
            .and_then(|value| match value {
                OrderedValue::String(value) => Some(value.as_str()),
                _ => None,
            })
    })
}

fn direct_value<'a>(value: &'a OrderedValue, key: &str) -> Option<&'a OrderedValue> {
    let OrderedValue::Object(values) = value else {
        return None;
    };
    values
        .iter()
        .find_map(|(candidate, value)| (candidate == key).then_some(value))
}

fn find_string_for_key<'a>(value: &'a OrderedValue, key: &str) -> Option<&'a str> {
    match value {
        OrderedValue::Object(values) => direct_string(value, key).or_else(|| {
            values
                .iter()
                .find_map(|(_, child)| find_string_for_key(child, key))
        }),
        OrderedValue::Array(values) => values
            .iter()
            .find_map(|child| find_string_for_key(child, key)),
        OrderedValue::String(_) | OrderedValue::Other => None,
    }
}

fn ordered_payload(value: &OrderedValue) -> &OrderedValue {
    direct_value(value, "data").unwrap_or(value)
}

pub fn event_payload(raw_event: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_str(raw_event).map_err(|error| error.to_string())?;
    Ok(value
        .as_object()
        .and_then(|object| object.get("data"))
        .cloned()
        .unwrap_or(value))
}

pub fn parse_event(raw_event: &str) -> Result<(String, String), String> {
    let envelope: OrderedValue =
        serde_json::from_str(raw_event).map_err(|error| error.to_string())?;
    let payload = ordered_payload(&envelope);
    let pane_id = direct_string(payload, "pane_id")
        .or_else(|| find_string_for_key(payload, "pane_id"))
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "event payload has no string pane_id".to_owned())?;
    let status = direct_string(payload, "agent_status")
        .or_else(|| find_string_for_key(payload, "agent_status"))
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "event payload has no string agent_status".to_owned())?
        .to_lowercase();
    if !VALID_STATUSES.contains(&status.as_str()) {
        return Err("event payload has invalid agent_status".to_owned());
    }
    Ok((pane_id.to_owned(), status))
}

pub fn payload_string<'a>(payload: &'a Value, key: &str) -> Option<&'a str> {
    payload
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn top_level_values_win_and_missing_fields_fall_back_independently() {
        let raw = r#"{"pane_id":"top","nested":{"pane_id":"nested","agent_status":"IDLE"}}"#;
        assert_eq!(
            parse_event(raw).unwrap(),
            ("top".to_owned(), "idle".to_owned())
        );
        let raw = r#"{"pane_id":9,"agent_status":false,"first":{"pane_id":"p1","agent_status":"working"},"second":{"pane_id":"p2","agent_status":"done"}}"#;
        assert_eq!(
            parse_event(raw).unwrap(),
            ("p1".to_owned(), "working".to_owned())
        );
    }

    #[test]
    fn envelope_data_is_unwrapped_before_lookup() {
        let raw = r#"{"pane_id":"wrong","agent_status":"blocked","data":{"pane_id":"right","agent_status":"DONE"}}"#;
        assert_eq!(
            parse_event(raw).unwrap(),
            ("right".to_owned(), "done".to_owned())
        );
        assert_eq!(
            payload_string(&event_payload(raw).unwrap(), "pane_id"),
            Some("right")
        );
    }

    #[test]
    fn exact_validation_errors_match_python() {
        assert_eq!(
            parse_event("{}").unwrap_err(),
            "event payload has no string pane_id"
        );
        assert_eq!(
            parse_event(r#"{"pane_id":"p"}"#).unwrap_err(),
            "event payload has no string agent_status"
        );
        assert_eq!(
            parse_event(r#"{"pane_id":"p","agent_status":"sleeping"}"#).unwrap_err(),
            "event payload has invalid agent_status"
        );
    }
}
