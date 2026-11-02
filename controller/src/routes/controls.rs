//! Validated controls: volume, motor mode, wobble, sounds, and the expression
//! dataset list the dashboard reads.

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::daemon::{DaemonError, safe_dataset};
use crate::error::{ApiError, ApiResult};
use crate::routes::power::kinematics;
use crate::state::Shared;

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/volume/current", get(|s| passthrough_get(s, "volume/current")))
        .route("/api/volume/set", post(|s, b| set_volume(s, b, "volume/set")))
        .route("/api/volume/microphone/current", get(|s| passthrough_get(s, "volume/microphone/current")))
        .route("/api/volume/microphone/set", post(|s, b| set_volume(s, b, "volume/microphone/set")))
        .route("/api/volume/test-sound", post(|s| passthrough_post(s, "volume/test-sound")))
        .route("/api/motors/status", get(|s| passthrough_get(s, "motors/status")))
        .route("/api/motors/set_mode/{mode}", post(set_motor_mode))
        .route("/api/media/wobbling/enable", post(enable_wobbling))
        .route("/api/media/wobbling/disable", post(|s| passthrough_post(s, "media/wobbling/disable")))
        .route("/api/media/play_sound", post(play_sound))
        .route("/api/media/stop-sound", post(|s| passthrough_post(s, "media/stop_sound")))
        .route("/api/move/recorded-move-datasets/list/{*dataset}", get(list_dataset))
}

/// These controls answer 503 for any daemon failure, as the stopgap did.
fn or_unavailable(result: Result<Value, DaemonError>) -> ApiResult<Json<Value>> {
    result.map(Json).map_err(|_| ApiError::new(503, "Robot service down"))
}

async fn passthrough_get(State(state): State<Shared>, path: &'static str) -> ApiResult<Json<Value>> {
    or_unavailable(state.daemon.get(path).await)
}

async fn passthrough_post(State(state): State<Shared>, path: &'static str) -> ApiResult<Json<Value>> {
    or_unavailable(state.daemon.post(path, &[]).await)
}

async fn set_volume(State(state): State<Shared>, body: Bytes, path: &'static str) -> ApiResult<Json<Value>> {
    // Strict like the stopgap's model: an integer, not a float or string.
    let volume = serde_json::from_slice::<Value>(&body)
        .ok()
        .and_then(|v| v.get("volume").and_then(Value::as_i64))
        .ok_or_else(ApiError::bad_body)?;
    if !(0..=100).contains(&volume) {
        return Err(ApiError::new(400, "Volume must be between 0 and 100"));
    }
    or_unavailable(state.daemon.post_json(path, &json!({"volume": volume})).await)
}

async fn set_motor_mode(State(state): State<Shared>, Path(mode): Path<String>) -> ApiResult<Json<Value>> {
    if !matches!(mode.as_str(), "enabled" | "disabled" | "gravity_compensation") {
        return Err(ApiError::new(400, "Invalid motor mode"));
    }
    // Not during a power change: enabling starts a return home, which would
    // run under the sleep move (#177). Going limp is the safe direction and
    // never waits behind a slow app start.
    let _power = if mode == "disabled" { None } else { Some(state.power_lock.lock().await) };
    // daemon/stop returns before its sleep move ends, so the lock alone
    // doesn't cover a power-off still in progress.
    if mode == "enabled" {
        let daemon_state = state.daemon.status().await;
        if daemon_state.as_ref().and_then(|s| s.get("state")).and_then(Value::as_str) != Some("running") {
            return Err(ApiError::new(409, "Robot off"));
        }
    }
    if mode == "gravity_compensation" {
        // The motors ignore move targets in this mode, so an app's moves would
        // silently do nothing while its speech carries on (#150).
        if state.apps.refresh_active().await {
            return Err(ApiError::new(409, "Stop the app first"));
        }
        // The daemon refuses it under any other engine (409 since 1.13.0, a
        // bare 500 before); check first for a clearer message.
        let engines = kinematics(&state).await?;
        if engines.get("active").and_then(Value::as_str) != Some("Placo") {
            return Err(ApiError::new(409, "Gravity needs Placo"));
        }
        // Same for an expression: it would stop mid-move and skip its return home.
        if state.player.active() {
            return Err(ApiError::new(
                409,
                "Expression playing",
            ));
        }
    }
    let outcome = state.daemon.post(&format!("motors/set_mode/{mode}"), &[]).await;
    // #67: any torque change invalidates the trusted pose (a limp head droops).
    // Only a clear refusal means nothing changed: a timeout or a 5xx may have
    // applied the mode anyway.
    if !matches!(outcome, Err(DaemonError::Refused { status, .. }) if status < 500) {
        state.player.mode_changed();
        // A drive session ends here rather than holding the slot until its
        // socket notices, so the return home below isn't refused as busy.
        state.player.abort_drive();
    }
    let result = match outcome {
        Ok(result) => result,
        Err(DaemonError::Unavailable) => return Err(ApiError::new(503, "Robot service down")),
        Err(DaemonError::Refused { status, detail }) => {
            return Err(ApiError::new(
                if status >= 500 { 502 } else { status },
                detail.unwrap_or_else(|| format!("Motor mode refused (HTTP {status})")),
            ));
        }
    };
    // Enabling starts the return home; expressions refuse until it completes.
    // If a session is already active, return_home is a no-op and that
    // session's own goto clears the flag. Not while an app runs: it owns the
    // robot's moves (daemon-contract.md rule 7), and needs_home stays set.
    if mode == "enabled" && !state.apps.refresh_active().await {
        state.player.return_home().await;
    }
    Ok(Json(result))
}

async fn enable_wobbling(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let _ = state.daemon.post("media/acquire", &[]).await;
    or_unavailable(state.daemon.post("media/wobbling/enable", &[]).await)
}

async fn play_sound(State(state): State<Shared>, body: Bytes) -> ApiResult<Json<Value>> {
    let file = serde_json::from_slice::<Value>(&body)
        .ok()
        .and_then(|v| v.get("file").and_then(Value::as_str).map(str::to_string))
        .ok_or_else(ApiError::bad_body)?;
    or_unavailable(state.daemon.post_json("media/play_sound", &json!({"file": file})).await)
}

/// Move names in a recorded-move dataset, cached (see `datasets.rs`).
async fn list_dataset(State(state): State<Shared>, Path(dataset): Path<String>) -> ApiResult<Json<Value>> {
    let dataset = dataset.trim_end_matches('/');
    if !safe_dataset(dataset) {
        return Err(ApiError::new(404, "Unknown dataset"));
    }
    Ok(Json(state.datasets.list(&state, dataset).await?))
}
