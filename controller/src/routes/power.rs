//! Robot power, backed by the daemon's own start/stop API.
//!
//! systemd owns the daemon process. "Power" switches the robot backend on and
//! off inside the always-running daemon: on enables the motors and plays the
//! wake-up move (ending at the home pose); off stops any app, plays the sleep
//! move and releases torque. Every action that powers, wakes or sleeps the
//! robot holds `power_lock` (#148), so two requests can't both act on the
//! same observed state.

use std::future::Future;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::State;
use axum::routing::{get, post};
use axum::{Json, Router};
use reqwest::Method;
use serde_json::{Value, json};

use crate::apps::APP_CONTROL_TIMEOUT;
use crate::daemon::{DaemonError, backend_up, motor_mode, robot_state};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

/// How long power off waits for stopped moves to leave the daemon.
const MOVES_STOP_TIMEOUT: Duration = Duration::from_secs(2);
/// The sleep move takes about 5 s, then the daemon tears the backend down.
pub const POWER_OFF_TIMEOUT: Duration = Duration::from_secs(40);
const WAKE_TIMEOUT: Duration = Duration::from_secs(20);

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/daemon/status", get(status))
        .route("/api/daemon/start", post(start))
        .route("/api/daemon/stop", post(stop))
        .route("/api/daemon/kinematics", get(get_kinematics).put(put_kinematics))
}

pub async fn wait_until<F, Fut>(timeout: Duration, mut check: F) -> bool
where
    F: FnMut() -> Fut,
    Fut: Future<Output = bool>,
{
    let deadline = tokio::time::Instant::now() + timeout;
    while tokio::time::Instant::now() < deadline {
        if check().await {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    false
}

/// Make sure the robot is on, the motors enabled and the wake-up move done.
///
/// The daemon doesn't wake the robot when it starts an app, and it puts the
/// robot to sleep when an app exits, so an app started from the controller
/// would otherwise run on a limp robot. Call with `power_lock` held.
pub async fn ensure_awake(state: &Shared) -> ApiResult<()> {
    let daemon = &state.daemon;
    let Some(status) = daemon.status().await else {
        return Ok(()); // unknown: let the daemon decide and report
    };
    let robot = robot_state(&status);
    if robot == "asleep" {
        daemon.post("motors/set_mode/enabled", &[]).await?;
        daemon.post("move/play/wake_up", &[]).await?;
    } else if robot == "running" && motor_mode(&status) == Some("gravity_compensation") {
        // Under gravity compensation the motors ignore every move target, so
        // an app would run on a robot that can't move (#150). Back to position
        // control; the head stays where it was guided.
        daemon.post("motors/set_mode/enabled", &[]).await?;
    } else if robot != "running" {
        // A power-on already in flight is still "starting": wait for it.
        if robot != "starting" {
            daemon.post("daemon/start", &[("wake_up", "true")]).await?;
        }
        let running =
            wait_until(WAKE_TIMEOUT, || async { daemon.status().await.is_some_and(|s| robot_state(&s) == "running") })
                .await;
        if !running {
            return Err(ApiError::new(504, "Robot did not wake up in time"));
        }
    }
    wait_until(WAKE_TIMEOUT, || async { daemon.running_moves().await.is_some_and(|m| m.is_empty()) }).await;
    state.player.set_needs_home(false);
    Ok(())
}

/// Power on; call with `power_lock` held. `wake = false` starts the backend
/// without the wake move, so the motors stay limp and the robot comes back
/// asleep.
pub async fn power_on_locked(state: &Shared, wake: bool) -> ApiResult<Value> {
    let daemon = &state.daemon;
    let robot = daemon.status().await.map(|s| robot_state(&s));
    if let Some(robot @ ("running" | "starting")) = robot.as_deref() {
        return Ok(json!({"status": robot}));
    }
    let result = if robot.as_deref() == Some("asleep") {
        daemon.post("motors/set_mode/enabled", &[]).await?;
        daemon.post("move/play/wake_up", &[]).await?
    } else {
        daemon.post("daemon/start", &[("wake_up", if wake { "true" } else { "false" })]).await?
    };
    // The wake-up move ends at the home pose; expressions are gated on the
    // daemon reporting running until then.
    state.player.set_needs_home(!wake);
    Ok(result)
}

/// Stop any app, play the sleep move and release torque; call with
/// `power_lock` held.
pub async fn power_off_locked(state: &Shared) -> ApiResult<Value> {
    let daemon = &state.daemon;
    // No drive targets during the sleep move.
    state.player.abort_drive();
    if state.apps.refresh_active().await {
        let _ = daemon.post_slow("apps/stop-current-app", APP_CONTROL_TIMEOUT).await;
    }
    // An expression (or its return home) still playing would run interleaved
    // with the sleep move.
    state.player.halt(MOVES_STOP_TIMEOUT).await;
    // Already asleep: skip the sleep move. It re-enables the motors and lifts
    // the head first, so the robot would seem to wake before sleeping.
    let asleep = daemon.status().await.is_some_and(|s| robot_state(&s) == "asleep");
    let result = daemon.post("daemon/stop", &[("goto_sleep", if asleep { "false" } else { "true" })]).await?;
    state.player.set_needs_home(true);
    // The stop job starts in the background; hold the lock until the daemon
    // stops reporting running, so a motor-mode change can't slip in first.
    wait_until(MOVES_STOP_TIMEOUT, || async {
        daemon.status().await.is_none_or(|s| s.get("state").and_then(Value::as_str) != Some("running"))
    })
    .await;
    Ok(result)
}

/// Robot state, in the shape the dashboard's power button expects.
async fn status(State(state): State<Shared>) -> Json<Value> {
    let Some(status) = state.daemon.status().await else {
        let target = state.sim.target();
        return Json(
            json!({"reachable": false, "running": false, "manageable": false, "status": "offline", "target": target}),
        );
    };
    let robot = robot_state(&status);
    Json(json!({
        "reachable": true,
        "running": robot == "running",
        "manageable": true,
        "status": robot,
        // Lets the dashboard tell the sleep move (motors on) from torque release.
        "motors": motor_mode(&status),
        "error": status.get("error").cloned().unwrap_or(Value::Null),
        // "robot" or "sim" (#149): which daemon the controller drives.
        "target": state.sim.target(),
    }))
}

/// Switch the robot on. A backend that is up but asleep is woken in place.
async fn start(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let _power = state.power_lock.lock().await;
    Ok(Json(power_on_locked(&state, true).await?))
}

async fn stop(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let _power = state.power_lock.lock().await;
    Ok(Json(power_off_locked(&state).await?))
}

pub async fn kinematics(state: &Shared) -> ApiResult<Value> {
    match state.daemon.get("bridge/kinematics").await {
        Ok(value) => Ok(value),
        Err(DaemonError::Unavailable) => Err(ApiError::unavailable()),
        Err(DaemonError::Refused { status: 404, .. }) => Err(ApiError::new(501, "Kinematics switching unavailable")),
        Err(DaemonError::Refused { .. }) => Err(ApiError::new(502, "Kinematics unavailable")),
    }
}

/// Engine for the next power-on, the running one, and what's installed.
async fn get_kinematics(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    Ok(Json(kinematics(&state).await?))
}

/// Switch the kinematics engine (#150).
///
/// The daemon only changes engines while the backend is down, so a robot that
/// is on (or asleep) is powered off, switched and powered back on as it was.
/// Refused while an app runs, so a switch never stops an app on its own.
async fn put_kinematics(State(state): State<Shared>, body: Bytes) -> ApiResult<Json<Value>> {
    let engine = serde_json::from_slice::<Value>(&body)
        .ok()
        .and_then(|v| v.get("engine").cloned())
        .ok_or_else(|| ApiError::new(422, r#"Body must be {"engine": ...}"#))?;
    let daemon = &state.daemon;
    let _power = state.power_lock.lock().await;
    let current = kinematics(&state).await?;
    let active = current.get("active").unwrap_or(&Value::Null);
    if current.get("engine") == Some(&engine) && (active.is_null() || *active == engine) {
        return Ok(Json(with_restarted(current, false)));
    }
    if state.apps.refresh_active().await {
        return Err(ApiError::new(409, "Stop the app before switching kinematics"));
    }
    let status = daemon.status().await;
    let robot = status.as_ref().map(robot_state);
    if let Some(robot @ ("starting" | "stopping")) = robot.as_deref() {
        return Err(ApiError::new(409, format!("Robot is {robot}; try again shortly")));
    }
    // A failed start can leave "error" with the backend still up; that is on.
    let was_on = matches!(robot.as_deref(), Some("running" | "asleep"))
        || (robot.as_deref() == Some("error") && status.as_ref().is_some_and(backend_up));
    // Come back as the user had it: asleep stays asleep (no wake move).
    let wake = robot.as_deref() != Some("asleep");
    if was_on {
        power_off_locked(&state).await?;
        // The daemon reports stopped (or error) slightly before it drops the
        // backend, and refuses the switch until it has.
        let off = wait_until(POWER_OFF_TIMEOUT, || async {
            daemon.status().await.is_some_and(|s| {
                !backend_up(&s) && matches!(robot_state(&s).as_str(), "stopped" | "not_initialized" | "error")
            })
        })
        .await;
        if !off {
            return Err(ApiError::new(504, "Robot did not power off in time"));
        }
    }
    let switched = daemon.call(Method::PUT, "bridge/kinematics", &[], Some(&json!({"engine": engine})), None).await;
    match switched {
        Ok(result) => {
            if was_on {
                power_on_locked(&state, wake).await?;
            }
            Ok(Json(with_restarted(result, was_on)))
        }
        Err(error) => {
            // Leave the robot as the user had it, on the old engine.
            if was_on {
                let restored = power_on_locked(&state, wake).await;
                if error != DaemonError::Unavailable {
                    restored?;
                }
            }
            Err(error.into())
        }
    }
}

fn with_restarted(mut value: Value, restarted: bool) -> Value {
    if let Some(object) = value.as_object_mut() {
        object.insert("restarted".into(), restarted.into());
    }
    value
}
