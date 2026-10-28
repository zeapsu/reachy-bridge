//! Sim mode (#149): try the controller on a simulated robot, with no hardware.
//!
//! An on-demand second daemon (`reachy-bridge-sim.service`: MuJoCo, no media,
//! no apps, its own loopback port) runs beside the robot's. Entering sim mode
//! starts it and points the controller's [`Daemon`] client at it; leaving
//! points the client back at the robot and stops it.
//!
//! Sim mode is only entered with the robot off, and nothing in the controller
//! can switch the robot on while it lasts (every action goes to the sim), so a
//! request that reaches the robot's daemon around a switch finds it off. The
//! dashboard sends the target it believes in (`X-Reachy-Target`), and an
//! action from a tab that missed a switch is refused rather than carried out
//! on the other robot.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::daemon::{backend_up, robot_state, Daemon};
use crate::error::{ApiError, ApiResult};
use crate::routes::power::wait_until;
use crate::state::Shared;

/// The header the dashboard puts its believed target in.
pub const TARGET_HEADER: &str = "x-reachy-target";
/// MuJoCo and the daemon take a while to import on the Jetson.
const SIM_START_TIMEOUT: Duration = Duration::from_secs(90);
/// The unit's own `TimeoutStopSec` bounds a stop; this bounds `systemctl`.
const SYSTEMCTL_TIMEOUT: Duration = Duration::from_secs(40);
/// How long leaving waits for sim moves to stop.
const MOVES_STOP_TIMEOUT: Duration = Duration::from_secs(2);
/// Routes that install, change or start apps: the sim daemon has none.
const APP_ACTIONS: [&str; 5] =
    ["/api/apps/start/", "/api/apps/install", "/api/apps/remove/", "/api/apps/update/", "/api/hf/token"];

pub struct SimMode {
    robot_url: String,
    sim_url: String,
    /// The systemd user unit to start and stop; `None` when something else
    /// runs the sim daemon (tests, `tools/sim`).
    unit: Option<String>,
    active: AtomicBool,
    /// The robot's return-home gate, kept while the sim uses the player.
    robot_needs_home: AtomicBool,
}

impl SimMode {
    pub fn new(robot_url: &str, sim_url: &str, unit: Option<String>) -> Self {
        Self {
            robot_url: robot_url.to_string(),
            sim_url: sim_url.to_string(),
            unit: unit.filter(|u| !u.is_empty()),
            active: AtomicBool::new(false),
            robot_needs_home: AtomicBool::new(false),
        }
    }

    /// `SIM_DAEMON_URL` (default `http://127.0.0.1:8001`) and `SIM_DAEMON_UNIT`
    /// (`reachy-bridge-sim.service`; empty leaves starting it to someone else).
    pub fn from_env(robot_url: &str) -> Self {
        let sim_url = std::env::var("SIM_DAEMON_URL").ok().filter(|v| !v.is_empty());
        let unit = std::env::var("SIM_DAEMON_UNIT").unwrap_or_else(|_| "reachy-bridge-sim.service".into());
        Self::new(robot_url, sim_url.as_deref().unwrap_or("http://127.0.0.1:8001"), Some(unit))
    }

    pub fn active(&self) -> bool {
        self.active.load(Ordering::SeqCst)
    }

    pub fn target(&self) -> &'static str {
        if self.active() { "sim" } else { "robot" }
    }

    pub fn sim_url(&self) -> &str {
        &self.sim_url
    }

    /// Stop a sim daemon left running by an earlier controller, which would
    /// otherwise use the CPU with nothing pointed at it.
    pub async fn stop_leftover(&self) {
        if let Some(unit) = &self.unit
            && let Err(error) = systemctl("stop", unit).await
        {
            tracing::debug!(%error, "sim daemon not stopped");
        }
    }
}

async fn systemctl(action: &str, unit: &str) -> Result<(), String> {
    let run = tokio::process::Command::new("systemctl")
        .args(["--user", action, unit])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .output();
    match tokio::time::timeout(SYSTEMCTL_TIMEOUT, run).await {
        Ok(Ok(output)) if output.status.success() => Ok(()),
        Ok(Ok(output)) => Err(String::from_utf8_lossy(&output.stderr).trim().chars().take(300).collect()),
        Ok(Err(error)) => Err(error.to_string()),
        Err(_) => Err(format!("systemctl {action} {unit} timed out")),
    }
}

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/sim", get(status))
        .route("/api/sim/enter", post(enter))
        .route("/api/sim/exit", post(exit))
}

async fn status(State(state): State<Shared>) -> Json<Value> {
    Json(json!({"active": state.sim.active(), "target": state.sim.target()}))
}

/// Start the sim daemon and switch the controller to it. Refused unless the
/// robot is off; a daemon that can't be reached has no robot on.
async fn enter(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let _power = state.power_lock.lock().await;
    let sim = &state.sim;
    if sim.active() {
        return Ok(Json(json!({"active": true, "target": "sim"})));
    }
    if let Some(status) = state.daemon.status().await {
        let robot = robot_state(&status);
        if backend_up(&status) || matches!(robot.as_str(), "running" | "asleep" | "starting" | "stopping") {
            return Err(ApiError::new(409, "Switch the robot off before starting the simulation"));
        }
        if state.apps.refresh_active().await {
            return Err(ApiError::new(409, "Stop the app before starting the simulation"));
        }
    }
    if let Some(unit) = &sim.unit {
        systemctl("start", unit)
            .await
            .map_err(|error| ApiError::new(502, format!("Couldn't start the simulation: {error}")))?;
    }
    let probe = Daemon::new(&sim.sim_url, Duration::from_secs(2));
    if !wait_until(SIM_START_TIMEOUT, || async { probe.status().await.is_some() }).await {
        if let Some(unit) = &sim.unit {
            let _ = systemctl("stop", unit).await;
        }
        return Err(ApiError::new(504, "The simulation didn't start in time"));
    }
    state.player.abort_drive();
    sim.robot_needs_home.store(state.player.needs_home(), Ordering::SeqCst);
    state.daemon.retarget(&sim.sim_url);
    sim.active.store(true, Ordering::SeqCst);
    state.apps.forget_installed().await;
    tracing::info!("sim mode on: the controller drives the sim daemon at {}", sim.sim_url);
    Ok(Json(json!({"active": true, "target": "sim"})))
}

/// Switch the controller back to the robot (still off) and stop the sim.
async fn exit(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let _power = state.power_lock.lock().await;
    let sim = &state.sim;
    if !sim.active() {
        return Ok(Json(json!({"active": false, "target": "robot"})));
    }
    // Nothing started on the sim may carry on against the robot.
    state.player.abort_drive();
    state.player.halt(MOVES_STOP_TIMEOUT).await;
    state.daemon.retarget(&sim.robot_url);
    sim.active.store(false, Ordering::SeqCst);
    state.player.set_needs_home(sim.robot_needs_home.load(Ordering::SeqCst));
    state.apps.forget_installed().await;
    tracing::info!("sim mode off: the controller drives the robot's daemon again");
    if let Some(unit) = &sim.unit
        && let Err(error) = systemctl("stop", unit).await
    {
        tracing::warn!(%error, "sim daemon not stopped");
    }
    Ok(Json(json!({"active": false, "target": "robot"})))
}

/// Refuse app installs and runs in sim mode, and any action from a tab that
/// believes in the other target.
pub async fn guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if !request.method().is_safe() && path.starts_with("/api/") && !path.starts_with("/api/sim") && !path.starts_with("/api/auth/") {
        let believed = request.headers().get(TARGET_HEADER).and_then(|v| v.to_str().ok());
        let target = state.sim.target();
        if believed.is_some_and(|b| b != target) {
            let detail = if target == "sim" {
                "The controller switched to the simulation; reload the page"
            } else {
                "The controller switched back to the robot; reload the page"
            };
            return ApiError::new(409, detail).into_response();
        }
        if state.sim.active() && APP_ACTIONS.iter().any(|p| path.starts_with(p)) {
            return ApiError::new(409, "Apps are off in sim mode").into_response();
        }
    }
    next.run(request).await
}
