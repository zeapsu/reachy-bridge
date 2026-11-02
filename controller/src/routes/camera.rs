//! Camera availability and the daemon's MJPEG preview, proxied.
//!
//! The daemon owns the camera (daemon-contract.md); the controller never
//! opens a capture device. Each dashboard preview is one upstream request,
//! dropped with the browser's connection.

use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use futures_util::StreamExt;
use serde_json::{Value, json};

use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

const STARTUP_TIMEOUT: Duration = Duration::from_secs(8);

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/camera/availability", get(availability))
        .route("/api/camera/specs", get(specs))
        .route("/api/camera/stream", get(stream))
}

async fn availability(State(state): State<Shared>) -> Json<Value> {
    let media = state.daemon.get("media/status").await.ok();
    let unavailable = |reason: &str| Json(json!({"available": false, "reason": reason, "holder": null}));
    match &media {
        None if state.daemon.status().await.is_none() => return unavailable("daemon_down"),
        Some(media)
            if media.get("available") == Some(&Value::Bool(false))
                || media.get("released") == Some(&Value::Bool(true)) =>
        {
            return unavailable("media_unavailable");
        }
        _ => {}
    }
    Json(json!({"available": true, "reason": "available", "holder": null}))
}

async fn specs(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    state.daemon.get("camera/specs").await.map(Json).map_err(|_| ApiError::new(503, "Robot service down"))
}

fn upstream_failed() -> Response {
    ApiError::new(503, "Camera upstream failed while opening the stream").into_response()
}

async fn stream(State(state): State<Shared>) -> Response {
    let deadline = tokio::time::Instant::now() + STARTUP_TIMEOUT;
    let opened =
        tokio::time::timeout_at(deadline, state.daemon.open_stream("camera/stream.mjpg", "multipart/x-mixed-replace"))
            .await;
    let Ok(Ok(upstream)) = opened else { return upstream_failed() };
    let status = upstream.status().as_u16();
    let content_type = upstream.headers().get(header::CONTENT_TYPE).cloned();
    if status >= 400 {
        let Ok(Ok(body)) = tokio::time::timeout_at(deadline, upstream.bytes()).await else {
            return upstream_failed();
        };
        let status = if status >= 500 { 503 } else { status };
        let mut response = (StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY), body).into_response();
        if let Some(content_type) = content_type {
            response.headers_mut().insert(header::CONTENT_TYPE, content_type);
        }
        return response;
    }
    // Wait for the first frame bytes so a stalled pipeline reports 503
    // instead of an image that never loads.
    let mut chunks = upstream.bytes_stream();
    let first = match tokio::time::timeout_at(deadline, chunks.next()).await {
        Ok(Some(Ok(first))) => first,
        _ => return upstream_failed(),
    };
    let body = futures_util::stream::once(async move { Ok::<_, reqwest::Error>(first) }).chain(chunks);
    let mut response = Response::new(Body::from_stream(body));
    if let Some(content_type) = content_type {
        response.headers_mut().insert(header::CONTENT_TYPE, content_type);
    }
    response
}
