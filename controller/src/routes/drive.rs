//! Manual drive (#152): one WebSocket session streams head, body and antenna
//! targets to the daemon's `move/ws/set_target`, clamped to the robot's
//! limits. The session holds the expression player's slot, so plays and app
//! starts are refused while it runs, and it ends with the silent return home.

use std::sync::Arc;
use std::time::Duration;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::Response;
use axum::routing::get;
use axum::{Extension, Router};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite;

use crate::auth::{AuthContext, Credential};
use crate::daemon::{backend_up, motor_mode};
use crate::error::{ApiError, ApiResult};
use crate::expressions::{ExpressionPlayer, PlayError};
use crate::state::Shared;

/// How often a session re-checks the motors and the running app.
const RECHECK: Duration = Duration::from_secs(1);
/// A socket that takes longer to accept a message ends the session, so a
/// stalled peer can't hold the robot.
const SEND_TIMEOUT: Duration = Duration::from_millis(500);

use std::f64::consts::PI;

const DEG: f64 = PI / 180.0;
const HEAD_TILT: f64 = 40.0 * DEG;
/// Head yaw relative to the body: the analytical IK's `max_relative_yaw`.
const HEAD_YAW: f64 = 65.0 * DEG;
/// The analytical IK's `max_body_yaw`.
const BODY_YAW: f64 = 160.0 * DEG;
const HEAD_XY: f64 = 0.02;
const HEAD_Z: (f64, f64) = (-0.03, 0.02);
/// Narrower than the daemon accepts until checked on hardware.
const ANTENNA: f64 = 90.0 * DEG;

pub fn router() -> Router<Shared> {
    Router::new().route("/api/drive/ws", get(upgrade))
}

pub fn limits() -> Value {
    json!({
        "head": {
            "roll": [-HEAD_TILT, HEAD_TILT],
            "pitch": [-HEAD_TILT, HEAD_TILT],
            "yaw_from_body": [-HEAD_YAW, HEAD_YAW],
            "x": [-HEAD_XY, HEAD_XY],
            "y": [-HEAD_XY, HEAD_XY],
            "z": [HEAD_Z.0, HEAD_Z.1],
        },
        "body_yaw": [-BODY_YAW, BODY_YAW],
        "antennas": [-ANTENNA, ANTENNA],
    })
}

fn number(value: Option<&Value>) -> Option<f64> {
    value.and_then(Value::as_f64).filter(|v| v.is_finite())
}

/// A client message as a daemon `FullBodyTarget`, every value clamped. The
/// head yaw is clamped around the body yaw: this message's, else the last one
/// sent. `None` for anything malformed, including a message with no target.
pub fn target(message: &Value, body_yaw: &mut f64) -> Option<Value> {
    let object = message.as_object()?;
    let mut out = serde_json::Map::new();
    if let Some(raw) = object.get("body_yaw") {
        let yaw = number(Some(raw))?.clamp(-BODY_YAW, BODY_YAW);
        *body_yaw = yaw;
        out.insert("target_body_yaw".into(), json!(yaw));
    }
    if let Some(head) = object.get("head") {
        let head = head.as_object()?;
        let field = |key: &str, low: f64, high: f64| -> Option<f64> {
            match head.get(key) {
                None => Some(0.0),
                raw => number(raw).map(|v| v.clamp(low, high)),
            }
        };
        out.insert(
            "target_head_pose".into(),
            json!({
                "x": field("x", -HEAD_XY, HEAD_XY)?,
                "y": field("y", -HEAD_XY, HEAD_XY)?,
                "z": field("z", HEAD_Z.0, HEAD_Z.1)?,
                "roll": field("roll", -HEAD_TILT, HEAD_TILT)?,
                "pitch": field("pitch", -HEAD_TILT, HEAD_TILT)?,
                "yaw": field("yaw", (*body_yaw - HEAD_YAW).max(-PI), (*body_yaw + HEAD_YAW).min(PI))?,
            }),
        );
    }
    if let Some(antennas) = object.get("antennas") {
        let [right, left] = antennas.as_array()?.as_slice() else { return None };
        let clamp = |v: &Value| number(Some(v)).map(|v| v.clamp(-ANTENNA, ANTENNA));
        out.insert("target_antennas".into(), json!([clamp(right)?, clamp(left)?]));
    }
    (!out.is_empty()).then_some(Value::Object(out))
}

/// Why the robot can't be driven now. A running app is its own case: it owns
/// the robot's moves, so a session it ends skips the return home.
enum Refusal {
    App(ApiError),
    Other(ApiError),
}

impl Refusal {
    fn error(self) -> ApiError {
        match self {
            Refusal::App(error) | Refusal::Other(error) => error,
        }
    }
}

async fn refusal(state: &Shared) -> Option<Refusal> {
    let Some(status) = state.daemon.status().await.filter(backend_up) else {
        return Some(Refusal::Other(ApiError::new(503, "Robot is off")));
    };
    if motor_mode(&status) != Some("enabled") {
        return Some(Refusal::Other(ApiError::new(409, "Set Motors to Enabled to drive")));
    }
    if state.apps.refresh_active().await {
        return Some(Refusal::App(ApiError::new(409, "Stop the app first: it is driving the robot")));
    }
    None
}

/// Ends the drive session however the socket goes away, including an upgrade
/// that never completes. `home` is cleared when an app took over.
struct Session {
    player: Arc<ExpressionPlayer>,
    epoch: u64,
    home: bool,
}

impl Drop for Session {
    fn drop(&mut self) {
        self.player.end_drive(self.epoch, self.home);
    }
}

async fn upgrade(
    State(state): State<Shared>,
    ctx: Option<Extension<AuthContext>>,
    ws: WebSocketUpgrade,
) -> ApiResult<Response> {
    // Requests built without the auth layer (unit tests) count as trusted.
    let credential = ctx.and_then(|Extension(ctx)| ctx.credential).unwrap_or(Credential::Trusted);
    // Connect before claiming the robot, so a failure leaves nothing to undo.
    let url = state.daemon.ws_url("move/ws/set_target");
    // Bounded: the upgrade holds actions and sim switches apart (sim.rs).
    let connect = tokio::time::timeout(Duration::from_secs(5), tokio_tungstenite::connect_async(url.as_str()));
    let Ok(Ok((upstream, _))) = connect.await else {
        return Err(ApiError::unavailable());
    };
    // Under the power lock, so an app start or a power action can't be
    // halfway through while the session is claimed.
    let epoch = {
        let _power = state.power_lock.lock().await;
        if let Some(refusal) = refusal(&state).await {
            return Err(refusal.error());
        }
        match state.player.start_drive().await {
            Ok(epoch) => epoch,
            Err(PlayError::Busy(detail)) => return Err(ApiError::new(409, detail)),
            Err(_) => return Err(ApiError::unavailable()),
        }
    };
    let session = Session { player: state.player.clone(), epoch, home: true };
    Ok(ws.on_upgrade(move |socket| drive(state, socket, upstream, session, credential)))
}

type Upstream = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Send within [`SEND_TIMEOUT`]; false if the peer is gone or stalled.
async fn sent<E>(send: impl std::future::Future<Output = Result<(), E>>) -> bool {
    matches!(tokio::time::timeout(SEND_TIMEOUT, send).await, Ok(Ok(())))
}

const MODE_CHANGED: &str = "Motor mode changed: drive again once the robot is home";

async fn drive(state: Shared, mut socket: WebSocket, mut upstream: Upstream, mut session: Session, credential: Credential) {
    let epoch = session.epoch;
    let ready = json!({"type": "ready", "limits": limits()});
    if !sent(socket.send(Message::Text(ready.to_string().into()))).await {
        return;
    }
    let mut body_yaw = 0.0;
    let mut recheck = tokio::time::interval(RECHECK);
    recheck.tick().await;
    // Ends the session when this browser is revoked or loses its trust (#221).
    let ended = state.auth.ended(credential);
    tokio::pin!(ended);
    let detail = loop {
        tokio::select! {
            () = &mut ended => break "Logged out: log in again to drive".to_string(),
            message = socket.recv() => match message {
                Some(Ok(Message::Text(text))) => {
                    if !state.player.driving(epoch) {
                        break MODE_CHANGED.to_string();
                    }
                    let parsed = serde_json::from_str::<Value>(&text).ok();
                    let Some(target) = parsed.as_ref().and_then(|m| target(m, &mut body_yaw)) else {
                        break "Invalid drive target".to_string();
                    };
                    if !sent(upstream.send(tungstenite::Message::text(target.to_string()))).await {
                        break "Daemon unavailable".to_string();
                    }
                }
                Some(Ok(Message::Close(_))) | None | Some(Err(_)) => return,
                Some(Ok(_)) => {}
            },
            reply = upstream.next() => match reply {
                // The daemon only answers to report a target it couldn't parse.
                Some(Ok(tungstenite::Message::Text(text))) => tracing::warn!("daemon refused a drive target: {text}"),
                Some(Ok(_)) => {}
                None | Some(Err(_)) => break "Daemon unavailable".to_string(),
            },
            _ = recheck.tick() => {
                if !state.player.driving(epoch) {
                    break MODE_CHANGED.to_string();
                }
                match refusal(&state).await {
                    None => {}
                    Some(Refusal::App(error)) => {
                        session.home = false;
                        break error.detail;
                    }
                    Some(Refusal::Other(error)) => break error.detail,
                }
            }
        }
    };
    let error = json!({"type": "error", "detail": detail});
    if sent(socket.send(Message::Text(error.to_string().into()))).await {
        let _ = sent(socket.send(Message::Close(None))).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamps_every_value() {
        let mut body = 0.0;
        let out = target(
            &json!({"head": {"pitch": 2.0, "roll": -2.0, "z": 0.5, "x": -1.0}, "antennas": [3.0, -3.0]}),
            &mut body,
        )
        .unwrap();
        let head = &out["target_head_pose"];
        assert_eq!(head["pitch"], json!(HEAD_TILT));
        assert_eq!(head["roll"], json!(-HEAD_TILT));
        assert_eq!(head["z"], json!(HEAD_Z.1));
        assert_eq!(head["x"], json!(-HEAD_XY));
        assert_eq!(head["y"], json!(0.0));
        assert_eq!(out["target_antennas"], json!([ANTENNA, -ANTENNA]));
        assert!(out.get("target_body_yaw").is_none());
    }

    #[test]
    fn head_yaw_follows_the_body() {
        let mut body = 0.0;
        let out = target(&json!({"body_yaw": 1.0, "head": {"yaw": 3.0}}), &mut body).unwrap();
        assert_eq!(out["target_body_yaw"], json!(1.0));
        assert_eq!(out["target_head_pose"]["yaw"], json!(1.0 + HEAD_YAW));
        // The body yaw carries over to a head-only message.
        let out = target(&json!({"head": {"yaw": -3.0}}), &mut body).unwrap();
        assert_eq!(out["target_head_pose"]["yaw"], json!(1.0 - HEAD_YAW));
        let out = target(&json!({"body_yaw": 9.0}), &mut body).unwrap();
        assert_eq!(out["target_body_yaw"], json!(BODY_YAW));
    }

    #[test]
    fn rejects_malformed_targets() {
        let mut body = 0.0;
        for message in [
            json!({}),
            json!([1, 2]),
            json!({"head": 1}),
            json!({"head": {"pitch": "up"}}),
            json!({"body_yaw": null}),
            json!({"antennas": [1.0]}),
            json!({"antennas": [1.0, "x"]}),
        ] {
            assert!(target(&message, &mut body).is_none(), "{message}");
        }
        assert_eq!(body, 0.0);
    }
}
