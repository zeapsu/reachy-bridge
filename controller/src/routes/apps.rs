//! App routes over the daemon's Hugging Face app manager: list, start/stop
//! with the controller's guards, the app UI probe, and up to three favorites
//! for the dashboard panel.

use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Path as UrlPath, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::apps::APP_CONTROL_TIMEOUT;
use crate::error::{ApiError, ApiResult};
use crate::routes::power::ensure_awake;
use crate::state::Shared;

pub const MAX_FAVORITES: usize = 3;

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/apps/status", get(status))
        .route("/api/apps/favorites", get(get_favorites).put(put_favorites))
        .route("/api/apps/start/{name}", post(start))
        .route("/api/apps/stop/{name}", post(stop))
        .route("/api/apps/{name}/ui", get(ui))
}

async fn status(State(state): State<Shared>) -> ApiResult<Json<Vec<Value>>> {
    state.apps.statuses().await.map(Json).map_err(|_| ApiError::unavailable())
}

async fn installed_names(state: &Shared) -> ApiResult<Vec<String>> {
    state.apps.installed_names().await.map_err(|_| ApiError::unavailable())
}

fn favorites_path(dir: &Path) -> PathBuf {
    dir.join("favorites.json")
}

fn load_favorites(dir: &Path) -> Option<Vec<String>> {
    let text = std::fs::read_to_string(favorites_path(dir)).ok()?;
    let payload: Value = serde_json::from_str(&text).ok()?;
    let mut favorites: Vec<String> = serde_json::from_value(payload.get("favorites")?.clone()).ok()?;
    favorites.truncate(MAX_FAVORITES);
    Some(favorites)
}

/// Write atomically: a temp file in the same directory, fsync, rename.
fn save_favorites(dir: &Path, favorites: &[String]) -> std::io::Result<()> {
    use std::io::Write;
    std::fs::create_dir_all(dir)?;
    let tmp = dir.join(format!(".favorites.{}.tmp", std::process::id()));
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(json!({"favorites": favorites}).to_string().as_bytes())?;
    file.sync_all()?;
    std::fs::rename(&tmp, favorites_path(dir))
}

async fn get_favorites(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let installed = installed_names(&state).await?;
    let favorites = load_favorites(&state.state_dir)
        .unwrap_or_else(|| installed.iter().take(MAX_FAVORITES).cloned().collect());
    let favorites: Vec<String> = favorites.into_iter().filter(|name| installed.contains(name)).collect();
    Ok(Json(json!({"favorites": favorites})))
}

async fn put_favorites(State(state): State<Shared>, body: Bytes) -> ApiResult<Json<Value>> {
    let invalid = || ApiError::new(422, "Invalid favorites payload");
    let payload: Value = serde_json::from_slice(&body).map_err(|_| invalid())?;
    let favorites: Vec<String> =
        serde_json::from_value(payload.get("favorites").cloned().ok_or_else(invalid)?).map_err(|_| invalid())?;
    let mut seen = std::collections::HashSet::new();
    if !favorites.iter().all(|name| seen.insert(name)) {
        return Err(ApiError::new(422, "Duplicate favorite app"));
    }
    if favorites.len() > MAX_FAVORITES {
        return Err(ApiError::new(422, "Too many favorites (maximum 3)"));
    }
    let installed = installed_names(&state).await?;
    if let Some(unknown) = favorites.iter().find(|name| !installed.contains(name)) {
        return Err(ApiError::new(422, format!("Unknown favorite app: {unknown}")));
    }
    save_favorites(&state.state_dir, &favorites).map_err(|error| {
        tracing::error!(%error, "could not save favorites");
        ApiError::new(500, "Could not save favorites")
    })?;
    Ok(Json(json!({"favorites": favorites})))
}

fn refused(name: &str, error: impl Into<String>, status: &str) -> Json<Value> {
    Json(json!({"error": error.into(), "name": name, "status": status}))
}

/// Start an installed app: clear a crashed app, wake the robot, then start.
async fn start(State(state): State<Shared>, UrlPath(name): UrlPath<String>) -> ApiResult<Json<Value>> {
    // Check-then-act across app and expression state isn't atomic; single-user UI.
    if state.player.active() {
        return Ok(refused(&name, "Robot is playing an expression — wait for it to finish.", "refused"));
    }
    if !installed_names(&state).await?.contains(&name) {
        return Err(ApiError::new(404, "Unknown app"));
    }
    let daemon = &state.daemon;
    let started = {
        let _power = state.power_lock.lock().await;
        // A drive session claims the robot under this lock, so this check
        // can't race one (#152).
        if state.player.active() {
            return Ok(refused(&name, "Robot is busy with another move.", "refused"));
        }
        // The daemon keeps a crashed app in its slot (state "error") and
        // refuses every start until it is cleared.
        let current = state.apps.current().await.ok().flatten();
        if current.as_ref().and_then(|c| c.get("state")).and_then(Value::as_str) == Some("error") {
            let _ = daemon.post_slow("apps/stop-current-app", APP_CONTROL_TIMEOUT).await;
        }
        ensure_awake(&state).await?;
        daemon.post_slow(&format!("apps/start-app/{name}"), APP_CONTROL_TIMEOUT).await
    };
    match started {
        Ok(_) => {}
        Err(crate::daemon::DaemonError::Unavailable) => return Err(ApiError::unavailable()),
        Err(crate::daemon::DaemonError::Refused { detail, .. }) => {
            return Ok(refused(&name, detail.unwrap_or_else(|| "Daemon refused the app".into()), "refused"));
        }
    }
    state.apps.refresh_active().await;
    let port = state.apps.ui_port(&name).await.ok().flatten();
    Ok(Json(json!({"name": name, "status": "loading", "ui_port": port})))
}

/// Stop the daemon's current app (it runs one at a time).
async fn stop(State(state): State<Shared>, UrlPath(name): UrlPath<String>) -> ApiResult<Json<Value>> {
    match state.daemon.post_slow("apps/stop-current-app", APP_CONTROL_TIMEOUT).await {
        Ok(_) => {}
        Err(crate::daemon::DaemonError::Unavailable) => return Err(ApiError::unavailable()),
        Err(crate::daemon::DaemonError::Refused { .. }) => {
            return Ok(refused(&name, "No app is running", "stopped"));
        }
    }
    state.apps.refresh_active().await;
    Ok(Json(json!({"name": name, "status": "stopping"})))
}

/// Port of the app's own web UI and whether it answers yet. Apps start their
/// web server a few seconds after the process; the dashboard waits for
/// `ready` so it never lands on a connection-refused page.
async fn ui(State(state): State<Shared>, UrlPath(name): UrlPath<String>) -> ApiResult<Json<Value>> {
    let port = state.apps.ui_port(&name).await.map_err(|_| ApiError::unavailable())?;
    let ready = match port {
        Some(port) => state
            .probe
            .get(format!("http://127.0.0.1:{port}/"))
            .timeout(Duration::from_secs(1))
            .send()
            .await
            .is_ok(),
        None => false,
    };
    Ok(Json(json!({"name": name, "port": port, "ready": ready})))
}
