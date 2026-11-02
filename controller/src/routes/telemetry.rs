//! Telemetry snapshots and the three dashboard SSE streams (robot state,
//! audio levels, host stats).

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::State;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use futures_util::{Stream, StreamExt};
use serde_json::{Value, json};
use tokio_stream::wrappers::WatchStream;

use crate::error::{ApiError, ApiResult};
use crate::host;
use crate::state::Shared;
use crate::streams::{self, Sample};

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/telemetry", get(snapshot))
        .route("/api/telemetry/state", get(full_state))
        .route("/api/telemetry/daemon-status", get(daemon_status))
        .route("/telemetry/state-stream", get(state_stream))
        .route("/api/system/stats", get(system_stats))
        .route("/api/system/stats-stream", get(stats_stream))
        .route("/api/system/platform", get(platform))
        .route("/api/audio/levels-stream", get(audio_stream))
}

/// One resilient snapshot; each part is `null` when it can't be read.
async fn snapshot(State(state): State<Shared>) -> Json<Value> {
    let d = &state.daemon;
    let (daemon, full, app, motors, speaker, mic) = tokio::join!(
        d.get("daemon/status"),
        d.get_query("state/full", &[("with_doa", "true")]),
        d.get("apps/current-app-status"),
        d.get("motors/status"),
        d.get("volume/current"),
        d.get("volume/microphone/current"),
    );
    let host = tokio::task::spawn_blocking(host::collect_host_metrics).await.unwrap_or(Value::Null);
    let field = |v: &Result<Value, _>, key: &str| v.as_ref().ok().and_then(|v| v.get(key)).cloned();
    Json(json!({
        "timestamp": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
        "daemon": {"online": daemon.is_ok(), "status": daemon.as_ref().ok()},
        "robot": {"state": full.as_ref().ok(), "motor_mode": field(&motors, "mode")},
        "app": app.as_ref().ok(),
        "audio": {"speaker_volume": field(&speaker, "volume"), "microphone_volume": field(&mic, "volume")},
        "host": host,
    }))
}

async fn full_state(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    state
        .daemon
        .get_query("state/full", &[("with_doa", "true")])
        .await
        .map(Json)
        .map_err(|_| ApiError::new(503, "Robot service down"))
}

async fn daemon_status(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    state.daemon.status().await.map(Json).ok_or_else(|| ApiError::new(503, "Robot service down"))
}

/// The board the runtime detected, its support tier, and the bridge features
/// startup applied or skipped (#140). Fixed for the daemon's lifetime.
async fn platform(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    state.daemon.get("bridge/platform").await.map(Json).map_err(|_| ApiError::new(503, "Robot service down"))
}

async fn system_stats() -> Json<Value> {
    Json(tokio::task::spawn_blocking(host::collect_stats).await.unwrap_or(Value::Null))
}

type EventStream = Response;

/// Map each sample to an event (or skip it), with a 15 s keepalive comment.
pub(crate) fn sse(rx: tokio::sync::watch::Receiver<Sample>, to_event: fn(&Value) -> Option<Event>) -> EventStream {
    let events = WatchStream::from_changes(rx)
        .filter_map(move |sample| async move { sample.as_deref().and_then(to_event).map(Ok) });
    let events: std::pin::Pin<Box<dyn Stream<Item = Result<Event, Infallible>> + Send>> = Box::pin(events);
    Sse::new(events).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)).text("keepalive")).into_response()
}

fn is_error(value: &Value) -> bool {
    value.get("error").is_some()
}

async fn state_stream(State(state): State<Shared>) -> EventStream {
    let daemon = state.daemon.clone();
    let rx = state.streams.state.subscribe(|tx| streams::run_state(daemon, tx));
    sse(rx, |value| {
        Some(if is_error(value) {
            Event::default().event("daemon-offline").data("{}")
        } else {
            Event::default().event("state-update").data(value.to_string())
        })
    })
}

async fn audio_stream(State(state): State<Shared>) -> EventStream {
    let daemon = state.daemon.clone();
    let rx = state.streams.audio.subscribe(|tx| streams::run_audio(daemon, tx));
    sse(rx, |value| {
        Some(if is_error(value) {
            Event::default().event("daemon-unavailable").data("{}")
        } else {
            Event::default().event("audio-level").data(value.to_string())
        })
    })
}

async fn stats_stream(State(state): State<Shared>) -> EventStream {
    let daemon = state.daemon.clone();
    let rx = state.streams.stats.subscribe(|tx| streams::run_stats(daemon, tx));
    sse(rx, |value| Some(Event::default().event("stats-update").data(value.to_string())))
}

pub struct Streams {
    pub state: Arc<streams::LazyStream>,
    pub audio: Arc<streams::LazyStream>,
    pub stats: Arc<streams::LazyStream>,
    /// The sim's sound events (#219).
    pub sounds: Arc<streams::LazyStream>,
}

impl Default for Streams {
    fn default() -> Self {
        Self {
            state: streams::LazyStream::new(false),
            audio: streams::LazyStream::new(true),
            stats: streams::LazyStream::new(false),
            sounds: streams::LazyStream::new(true),
        }
    }
}
