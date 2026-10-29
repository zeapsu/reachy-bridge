//! The sim's sounds, played in the browser (#219).
//!
//! The sim daemon has no media, so nothing it plays reaches a speaker. Its
//! runtime records each sound instead (`features/sim_sounds.py`): the
//! controller relays those events to the dashboard over SSE and serves the
//! files, by the token the runtime gave each one. Both routes answer only in
//! sim mode, so the robot's own sounds stay on the robot.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{header, StatusCode};
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use serde_json::{json, Value};
use tokio::sync::watch;

use crate::error::ApiError;
use crate::routes::telemetry::sse;
use crate::state::Shared;
use crate::streams::Sample;

/// Poll cadence while sim mode is on: a sound starts at most this late.
const POLL: Duration = Duration::from_millis(150);
/// Poll cadence otherwise, or when the sim daemon doesn't answer.
const IDLE: Duration = Duration::from_secs(1);
const FILE_TIMEOUT: Duration = Duration::from_secs(10);

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/sim/sounds-stream", get(stream))
        .route("/api/sim/sounds/{token}", get(file))
}

fn token_ok(token: &str) -> bool {
    token.len() == 32 && token.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Keep a well-formed `bridge/sounds` answer, reduced to the fields the
/// dashboard reads.
pub fn valid_sounds(value: &Value) -> Option<Value> {
    let seq = value.get("seq")?.as_u64()?;
    let events = value.get("events")?.as_array()?;
    let events: Vec<Value> = events
        .iter()
        .filter_map(|event| {
            let event_seq = event.get("seq")?.as_u64()?;
            match event.get("kind")?.as_str()? {
                "stop" => Some(json!({"seq": event_seq, "kind": "stop"})),
                "play" => {
                    let token = event.get("token")?.as_str().filter(|t| token_ok(t))?;
                    let name: String = event.get("name")?.as_str()?.chars().take(120).collect();
                    Some(json!({"seq": event_seq, "kind": "play", "token": token, "name": name}))
                }
                _ => None,
            }
        })
        .collect();
    Some(json!({"seq": seq, "events": events}))
}

/// The sim daemon's sound log while sim mode is on; a new sample only when
/// something played or stopped. The dashboard plays what is newer than the
/// first sample it saw, so a returning tab doesn't replay old sounds.
pub async fn run_sounds(state: Shared, tx: watch::Sender<Sample>) {
    let mut last: Option<Value> = None;
    loop {
        let sounds = if state.sim.active() {
            state.daemon.get("bridge/sounds").await.ok().and_then(|v| valid_sounds(&v))
        } else {
            None
        };
        let delay = if sounds.is_some() { POLL } else { IDLE };
        if let Some(sounds) = sounds {
            if last.as_ref() != Some(&sounds) {
                tx.send_replace(Some(Arc::new(sounds.clone())));
                last = Some(sounds);
            }
        } else if !state.sim.active() && last.take().is_some() {
            // Out of sim mode: forget the old sim daemon's log, so a tab
            // still listening when sim mode starts again takes the new
            // daemon's first sample as its starting point.
            tx.send_replace(None);
        }
        tokio::time::sleep(delay).await;
    }
}

async fn stream(State(state): State<Shared>) -> Response {
    if !state.sim.active() {
        return ApiError::new(409, "Sim sounds play only in sim mode").into_response();
    }
    let rx = state.streams.sounds.subscribe(|tx| run_sounds(state.clone(), tx));
    sse(rx, |value| Some(Event::default().event("sim-sounds").data(value.to_string())))
}

async fn file(State(state): State<Shared>, Path(token): Path<String>) -> Response {
    if !state.sim.active() {
        return ApiError::new(409, "Sim sounds play only in sim mode").into_response();
    }
    if !token_ok(&token) {
        return ApiError::new(404, "No such sound").into_response();
    }
    let path = format!("bridge/sounds/{token}");
    let Ok(Ok(upstream)) = tokio::time::timeout(FILE_TIMEOUT, state.daemon.open_stream(&path, "audio/*")).await
    else {
        return ApiError::unavailable().into_response();
    };
    if upstream.status() == StatusCode::NOT_FOUND {
        return ApiError::new(404, "No such sound").into_response();
    }
    if !upstream.status().is_success() {
        return ApiError::unavailable().into_response();
    }
    // Only audio passes: the runtime serves sound files and nothing else.
    let content_type = upstream.headers().get(header::CONTENT_TYPE).cloned();
    if !content_type.as_ref().and_then(|t| t.to_str().ok()).is_some_and(|t| t.starts_with("audio/")) {
        return ApiError::unavailable().into_response();
    }
    let length = upstream.headers().get(header::CONTENT_LENGTH).cloned();
    let mut response = Response::new(Body::from_stream(upstream.bytes_stream()));
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, content_type.expect("checked above"));
    if let Some(length) = length {
        headers.insert(header::CONTENT_LENGTH, length);
    }
    headers.insert(header::CACHE_CONTROL, header::HeaderValue::from_static("private, max-age=300"));
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sounds_keep_known_events_only() {
        let token = "a".repeat(32);
        let kept = valid_sounds(&json!({
            "seq": 3,
            "events": [
                {"seq": 1, "kind": "play", "token": token, "name": "wake_up.wav", "path": "/secret"},
                {"seq": 2, "kind": "play", "token": "../etc", "name": "x.wav"},
                {"seq": 3, "kind": "stop"},
                {"seq": 4, "kind": "volume"},
            ],
        }))
        .unwrap();
        assert_eq!(
            kept,
            json!({"seq": 3, "events": [
                {"seq": 1, "kind": "play", "token": token, "name": "wake_up.wav"},
                {"seq": 3, "kind": "stop"},
            ]})
        );
    }

    #[test]
    fn sounds_reject_a_malformed_answer() {
        assert!(valid_sounds(&json!({"events": []})).is_none());
        assert!(valid_sounds(&json!({"seq": 1})).is_none());
    }

    #[test]
    fn tokens_are_32_hex_digits() {
        assert!(token_ok(&"0f".repeat(16)));
        assert!(!token_ok(&"g".repeat(32)));
        assert!(!token_ok("abc"));
    }
}
