//! HTTP client for the local Reachy daemon (`reachy-mini`, bound to loopback).
//!
//! Every daemon call the controller makes goes through [`Daemon::call`], which
//! turns transport failures into [`DaemonError::Unavailable`] and error
//! statuses into [`DaemonError::Refused`] with the daemon's `detail`.

use std::sync::{Arc, RwLock};
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

/// Default per-request timeout; slow calls (app start/stop, power off) pass
/// their own.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, PartialEq)]
pub enum DaemonError {
    /// No answer: connection refused, timeout, or a body that isn't JSON.
    Unavailable,
    /// The daemon answered with an error status.
    Refused { status: u16, detail: Option<String> },
}

/// Every clone shares one target, so [`Daemon::retarget`] moves the whole
/// controller (apps, expressions, streams) between the robot's daemon and the
/// sim daemon (#149) at once.
#[derive(Clone)]
pub struct Daemon {
    base: Arc<RwLock<String>>,
    /// Bumped on every retarget, so long-lived connections can drop and
    /// reconnect to the new daemon.
    switched: Arc<tokio::sync::watch::Sender<u64>>,
    http: reqwest::Client,
    timeout: Duration,
}

impl Daemon {
    pub fn new(base_url: &str, timeout: Duration) -> Self {
        Self {
            base: Arc::new(RwLock::new(base_url.trim_end_matches('/').to_string())),
            switched: Arc::new(tokio::sync::watch::channel(0).0),
            http: reqwest::Client::new(),
            timeout,
        }
    }

    pub fn base_url(&self) -> String {
        self.base.read().unwrap().clone()
    }

    /// Point every clone at another daemon.
    pub fn retarget(&self, base_url: &str) {
        *self.base.write().unwrap() = base_url.trim_end_matches('/').to_string();
        self.switched.send_modify(|epoch| *epoch += 1);
    }

    /// Changes on every [`Daemon::retarget`].
    pub fn switched(&self) -> tokio::sync::watch::Receiver<u64> {
        self.switched.subscribe()
    }

    fn url(&self, path: &str) -> String {
        format!("{}/api/{}", self.base_url(), path.trim_start_matches("/api/").trim_start_matches('/'))
    }

    /// Open a request with no read timeout, for long-lived streams.
    pub async fn open_stream(&self, path: &str, accept: &str) -> Result<reqwest::Response, DaemonError> {
        self.http.get(self.url(path)).header("accept", accept).send().await.map_err(|_| DaemonError::Unavailable)
    }

    /// One request. An empty success body reads as `Value::Null`.
    pub async fn call(
        &self,
        method: Method,
        path: &str,
        query: &[(&str, &str)],
        body: Option<&Value>,
        timeout: Option<Duration>,
    ) -> Result<Value, DaemonError> {
        let mut request =
            self.http.request(method, self.url(path)).query(query).timeout(timeout.unwrap_or(self.timeout));
        if let Some(body) = body {
            request = request.json(body);
        }
        let response = request.send().await.map_err(|_| DaemonError::Unavailable)?;
        let status = response.status();
        let bytes = response.bytes().await.map_err(|_| DaemonError::Unavailable)?;
        if status.as_u16() >= 400 {
            let detail = serde_json::from_slice::<Value>(&bytes)
                .ok()
                .and_then(|v| v.get("detail").and_then(Value::as_str).map(str::to_string));
            return Err(DaemonError::Refused { status: status.as_u16(), detail });
        }
        if bytes.iter().all(u8::is_ascii_whitespace) {
            return Ok(Value::Null);
        }
        serde_json::from_slice(&bytes).map_err(|_| DaemonError::Unavailable)
    }

    pub async fn get(&self, path: &str) -> Result<Value, DaemonError> {
        self.call(Method::GET, path, &[], None, None).await
    }

    pub async fn get_query(&self, path: &str, query: &[(&str, &str)]) -> Result<Value, DaemonError> {
        self.call(Method::GET, path, query, None, None).await
    }

    pub async fn post(&self, path: &str, query: &[(&str, &str)]) -> Result<Value, DaemonError> {
        self.call(Method::POST, path, query, None, None).await
    }

    pub async fn post_json(&self, path: &str, body: &Value) -> Result<Value, DaemonError> {
        self.call(Method::POST, path, &[], Some(body), None).await
    }

    pub async fn post_slow(&self, path: &str, timeout: Duration) -> Result<Value, DaemonError> {
        self.call(Method::POST, path, &[], None, Some(timeout)).await
    }

    /// `GET daemon/status`, or `None` when the daemon can't be read.
    pub async fn status(&self) -> Option<Value> {
        self.get("daemon/status").await.ok()
    }

    /// UUIDs of the moves the daemon is running, or `None` when unreadable.
    pub async fn running_moves(&self) -> Option<Vec<String>> {
        let moves = self.get("move/running").await.ok()?;
        let moves = moves.as_array()?;
        Some(moves.iter().filter_map(|m| m.get("uuid").and_then(Value::as_str).map(str::to_string)).collect())
    }

    /// Whether the daemon's idle reset (the sleep after an app exits on its
    /// own or a remote session drops) is waiting out its grace or moving, from
    /// the runtime's `GET /api/bridge/idle-reset` (#292). `None` when the
    /// daemon can't be read. A daemon without the route has nothing to report
    /// (`false`); an error answer, or one without `pending`, reads as pending.
    pub async fn idle_reset_pending(&self) -> Option<bool> {
        match self.get("bridge/idle-reset").await {
            Ok(body) => Some(body.get("pending").and_then(Value::as_bool).unwrap_or(true)),
            Err(DaemonError::Refused { status: 404, .. }) => Some(false),
            Err(DaemonError::Refused { .. }) => Some(true),
            Err(DaemonError::Unavailable) => None,
        }
    }

    pub fn ws_url(&self, path: &str) -> String {
        let base = self.base_url().replacen("http://", "ws://", 1).replacen("https://", "wss://", 1);
        format!("{base}/api/{}", path.trim_start_matches('/'))
    }
}

/// One daemon path segment taken from a client URL: letters, digits, `.`,
/// `_` and `-`, and never `.` or `..`. Axum percent-decodes path parameters,
/// so without this check `..%2F` in a dataset or move name would reach any
/// daemon endpoint.
pub fn safe_segment(segment: &str) -> bool {
    !segment.is_empty()
        && segment != "."
        && segment != ".."
        && segment.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// A recorded-move dataset: `name` or `owner/name`, each a [`safe_segment`].
pub fn safe_dataset(dataset: &str) -> bool {
    let parts: Vec<&str> = dataset.split('/').collect();
    parts.len() <= 2 && parts.iter().all(|p| safe_segment(p))
}

/// Robot state from `daemon/status`: the daemon state, refined by motor mode.
///
/// The daemon stays `running` after it puts the robot to sleep on its own (the
/// idle reset after an app exits), so a running backend with the motors
/// disabled is reported as `asleep` rather than on (daemon-contract.md).
pub fn robot_state(status: &Value) -> String {
    let state = status.get("state").and_then(Value::as_str).unwrap_or("unknown");
    if state != "running" {
        return state.to_string();
    }
    if motor_mode(status) == Some("disabled") {
        return "asleep".to_string();
    }
    "running".to_string()
}

pub fn motor_mode(status: &Value) -> Option<&str> {
    status.get("backend_status").and_then(|b| b.get("motor_control_mode")).and_then(Value::as_str)
}

pub fn backend_up(status: &Value) -> bool {
    status.get("backend_status").is_some_and(|b| !b.is_null())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn running_with_motors_disabled_is_asleep() {
        let status = json!({"state": "running", "backend_status": {"motor_control_mode": "disabled"}});
        assert_eq!(robot_state(&status), "asleep");
    }

    #[test]
    fn running_with_gravity_compensation_is_on() {
        let status = json!({"state": "running", "backend_status": {"motor_control_mode": "gravity_compensation"}});
        assert_eq!(robot_state(&status), "running");
    }

    #[test]
    fn path_segments_reject_traversal_and_query() {
        assert!(safe_dataset("pollen-robotics/reachy-mini-emotions-library"));
        assert!(safe_segment("cheerful1"));
        assert!(!safe_dataset("../../daemon"));
        assert!(!safe_dataset("a/b/c"));
        assert!(!safe_segment("start?wake_up=true"));
        assert!(!safe_segment(".."));
        assert!(!safe_segment(""));
    }

    #[test]
    fn other_states_pass_through() {
        assert_eq!(robot_state(&json!({"state": "stopping", "backend_status": null})), "stopping");
        assert_eq!(robot_state(&json!({})), "unknown");
    }
}
