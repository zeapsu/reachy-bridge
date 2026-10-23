//! Expression routes over [`crate::expressions::ExpressionPlayer`].

use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::Value;

use crate::daemon::{safe_dataset, safe_segment};
use crate::error::{ApiError, ApiResult};
use crate::expressions::PlayError;
use crate::state::Shared;

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/expressions/play/{*target}", post(play))
        .route("/api/expressions/stop", post(stop))
        .route("/api/expressions/current", get(current))
}

/// `play/{dataset}/{move}`, where the dataset itself contains a slash
/// (`pollen-robotics/reachy-mini-emotions-library`).
async fn play(State(state): State<Shared>, Path(target): Path<String>) -> ApiResult<Json<Value>> {
    let (dataset, name) = target
        .rsplit_once('/')
        .filter(|(dataset, name)| safe_dataset(dataset) && safe_segment(name))
        .ok_or_else(|| ApiError::new(404, "Unknown move or dataset"))?;
    // Check-then-act across app and expression state isn't atomic; single-user UI.
    if state.apps.refresh_active().await {
        return Err(ApiError::new(409, "An app is controlling the robot — stop it first."));
    }
    match state.player.play(dataset, name).await {
        Ok(result) => Ok(Json(result)),
        Err(PlayError::Busy(detail)) => Err(ApiError::new(409, detail)),
        Err(PlayError::Unavailable) => Err(ApiError::unavailable()),
        Err(PlayError::NotFound) => Err(ApiError::new(404, "Unknown move or dataset")),
    }
}

async fn stop(State(state): State<Shared>) -> Json<Value> {
    Json(state.player.stop().await)
}

async fn current(State(state): State<Shared>) -> Json<Value> {
    Json(state.player.current())
}
