//! Log sources and their SSE stream.
//!
//! Events: one `log-reset` (`{status, lines}`) on connect and after the client
//! fell behind, `log-lines` batches (up to 32 lines or 250 ms), and
//! `source-status` changes.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::response::IntoResponse;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::routing::get;
use axum::{Json, Router};
use futures_util::Stream;
use serde::Deserialize;
use serde_json::json;
use tokio::sync::broadcast::Receiver;
use tokio::sync::broadcast::error::RecvError;

use crate::error::ApiError;
use crate::logs::{LogBroker, LogEvent, SOURCES};
use crate::state::Shared;

const BATCH_WINDOW: Duration = Duration::from_millis(250);
const BATCH_LINES: usize = 32;

pub fn router() -> Router<Shared> {
    Router::new().route("/api/logs/sources", get(sources)).route("/api/logs/stream", get(stream))
}

async fn sources(State(state): State<Shared>) -> Json<serde_json::Value> {
    let sources: Vec<_> = SOURCES.iter().map(|id| json!({"id": id, "status": state.logs.status(id)})).collect();
    Json(json!({"sources": sources}))
}

#[derive(Deserialize)]
struct StreamQuery {
    source: String,
}

fn reset_event(status: &str, lines: &[String]) -> Event {
    Event::default().event("log-reset").data(json!({"status": status, "lines": lines}).to_string())
}

fn status_event(status: &str) -> Event {
    Event::default().event("source-status").data(json!({"status": status}).to_string())
}

fn lines_event(lines: &[String]) -> Event {
    Event::default().event("log-lines").data(json!({"lines": lines}).to_string())
}

struct Follow {
    broker: Arc<LogBroker>,
    source: String,
    rx: Receiver<LogEvent>,
    /// Events ready to send, oldest first.
    queued: std::collections::VecDeque<Event>,
}

impl Follow {
    /// Start over from the ring after falling behind the broadcast channel.
    fn resync(&mut self) {
        let snapshot = self.broker.subscribe(&self.source);
        self.rx = snapshot.rx;
        self.queued.push_back(reset_event(&snapshot.status, &snapshot.lines));
    }

    async fn next_event(&mut self) -> Option<Event> {
        loop {
            if let Some(event) = self.queued.pop_front() {
                return Some(event);
            }
            match self.rx.recv().await {
                Ok(LogEvent::Status(status)) => return Some(status_event(&status)),
                Ok(LogEvent::Line(line)) => self.batch(line).await,
                Err(RecvError::Lagged(_)) => self.resync(),
                Err(RecvError::Closed) => return None,
            }
        }
    }

    /// Collect lines for one batch; a status change ends it and follows it.
    async fn batch(&mut self, first: String) {
        let mut lines = vec![first];
        let deadline = tokio::time::Instant::now() + BATCH_WINDOW;
        let mut after = None;
        let mut lagged = false;
        while lines.len() < BATCH_LINES {
            match tokio::time::timeout_at(deadline, self.rx.recv()).await {
                Ok(Ok(LogEvent::Line(line))) => lines.push(line),
                Ok(Ok(LogEvent::Status(status))) => {
                    after = Some(status_event(&status));
                    break;
                }
                Ok(Err(RecvError::Lagged(_))) => {
                    lagged = true;
                    break;
                }
                Ok(Err(RecvError::Closed)) | Err(_) => break,
            }
        }
        self.queued.push_back(lines_event(&lines));
        if let Some(event) = after {
            self.queued.push_back(event);
        }
        if lagged {
            self.resync();
        }
    }
}

async fn stream(State(state): State<Shared>, Query(query): Query<StreamQuery>) -> impl IntoResponse {
    if !SOURCES.contains(&query.source.as_str()) {
        return Err(ApiError::new(404, "Unknown log source"));
    }
    let snapshot = state.logs.subscribe(&query.source);
    let follow = Follow {
        broker: Arc::clone(&state.logs),
        source: query.source,
        rx: snapshot.rx,
        queued: [reset_event(&snapshot.status, &snapshot.lines)].into(),
    };
    let events = futures_util::stream::unfold(follow, |mut follow| async move {
        let event = follow.next_event().await?;
        Some((Ok::<_, Infallible>(event), follow))
    });
    let events: std::pin::Pin<Box<dyn Stream<Item = Result<Event, Infallible>> + Send>> = Box::pin(events);
    Ok((
        [("cache-control", "no-cache"), ("x-accel-buffering", "no")],
        Sse::new(events).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)).text("keepalive")),
    ))
}
