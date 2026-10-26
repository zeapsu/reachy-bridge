//! Manual drive (#152) over a real socket, against a fake daemon that records
//! the targets it is streamed and the gotos it is sent.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::ws::{Message, WebSocketUpgrade};
use axum::extract::State;
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::{AppState, Shared};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite;

#[derive(Default)]
struct Fake {
    motors: Option<String>,
    app: Option<Value>,
    targets: Vec<Value>,
    gotos: usize,
}

type FakeState = Arc<Mutex<Fake>>;

async fn fake_daemon(fake: FakeState) -> String {
    async fn set_target(State(fake): State<FakeState>, ws: WebSocketUpgrade) -> axum::response::Response {
        ws.on_upgrade(move |mut socket| async move {
            while let Some(Ok(Message::Text(text))) = socket.recv().await {
                fake.lock().unwrap().targets.push(serde_json::from_str(&text).unwrap());
            }
        })
    }
    let app = Router::new()
        .route(
            "/api/daemon/status",
            get(|State(f): State<FakeState>| async move {
                let f = f.lock().unwrap();
                let backend = f.motors.as_ref().map(|m| json!({"motor_control_mode": m}));
                Json(json!({"state": "running", "backend_status": backend}))
            }),
        )
        .route(
            "/api/apps/current-app-status",
            get(|State(f): State<FakeState>| async move { Json(f.lock().unwrap().app.clone().unwrap_or(Value::Null)) }),
        )
        .route("/api/move/running", get(|| async { Json(json!([])) }))
        .route(
            "/api/move/goto",
            post(|State(f): State<FakeState>| async move {
                f.lock().unwrap().gotos += 1;
                Json(json!({"uuid": "home"}))
            }),
        )
        .route("/api/move/ws/set_target", get(set_target))
        .with_state(fake);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

struct Harness {
    fake: FakeState,
    state: Shared,
    controller: String,
}

async fn harness(motors: Option<&str>) -> Harness {
    let fake = Arc::new(Mutex::new(Fake { motors: motors.map(str::to_string), ..Default::default() }));
    let url = fake_daemon(fake.clone()).await;
    let dir = std::env::temp_dir().join(format!("rc-drive-{}", std::process::id()));
    let state = AppState::new(Daemon::new(&url, Duration::from_secs(2)), LogBroker::new(), dir, Duration::from_millis(10));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let controller = listener.local_addr().unwrap().to_string();
    let app = reachy_controller::app(state.clone());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    Harness { fake, state, controller }
}

type Client = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

impl Harness {
    async fn connect(&self) -> Result<Client, u16> {
        match tokio_tungstenite::connect_async(format!("ws://{}/api/drive/ws", self.controller)).await {
            Ok((client, _)) => Ok(client),
            Err(tungstenite::Error::Http(response)) => Err(response.status().as_u16()),
            Err(error) => panic!("{error}"),
        }
    }

    async fn wait(&self, what: &str, check: impl Fn(&Fake) -> bool) {
        for _ in 0..200 {
            if check(&self.fake.lock().unwrap()) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("timed out waiting for {what}");
    }

    async fn wait_idle(&self) {
        for _ in 0..200 {
            if !self.state.player.active() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("drive session never ended");
    }
}

async fn next_json(client: &mut Client) -> Value {
    loop {
        match client.next().await.expect("socket closed").unwrap() {
            tungstenite::Message::Text(text) => return serde_json::from_str(&text).unwrap(),
            _ => continue,
        }
    }
}

#[tokio::test]
async fn drives_clamped_then_returns_home() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    let ready = next_json(&mut client).await;
    assert_eq!(ready["type"], "ready");
    assert!(ready["limits"]["head"]["pitch"].is_array());

    client.send(tungstenite::Message::text(r#"{"head": {"pitch": 2.0}, "body_yaw": 0.5}"#)).await.unwrap();
    h.wait("a target", |f| !f.targets.is_empty()).await;
    let target = h.fake.lock().unwrap().targets[0].clone();
    assert!((target["target_head_pose"]["pitch"].as_f64().unwrap() - 40f64.to_radians()).abs() < 1e-9);
    assert_eq!(target["target_body_yaw"], json!(0.5));

    // The session holds the robot: plays and a second session are refused.
    assert_eq!(h.state.player.current()["phase"], "driving");
    assert_eq!(h.connect().await.unwrap_err(), 409);

    client.close(None).await.unwrap();
    h.wait("the return home", |f| f.gotos == 1).await;
    h.wait_idle().await;
}

#[tokio::test]
async fn refused_unless_motors_enabled() {
    for motors in [Some("disabled"), Some("gravity_compensation")] {
        let h = harness(motors).await;
        assert_eq!(h.connect().await.unwrap_err(), 409, "{motors:?}");
        assert!(!h.state.player.active());
    }
    let h = harness(None).await;
    assert_eq!(h.connect().await.unwrap_err(), 503);
}

#[tokio::test]
async fn refused_while_an_app_runs() {
    let h = harness(Some("enabled")).await;
    h.fake.lock().unwrap().app = Some(json!({"info": {"name": "radio"}, "state": "running"}));
    assert_eq!(h.connect().await.unwrap_err(), 409);
    assert!(!h.state.player.active());
}

#[tokio::test]
async fn refused_until_homed() {
    let h = harness(Some("enabled")).await;
    h.state.player.set_needs_home(true);
    assert_eq!(h.connect().await.unwrap_err(), 409);
}

#[tokio::test]
async fn invalid_target_ends_the_session() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    client.send(tungstenite::Message::text(r#"{"head": {"pitch": "up"}}"#)).await.unwrap();
    let error = next_json(&mut client).await;
    assert_eq!(error, json!({"type": "error", "detail": "Invalid drive target"}));
    h.wait("the return home", |f| f.gotos == 1).await;
    h.wait_idle().await;
    assert!(h.fake.lock().unwrap().targets.is_empty());
}

#[tokio::test]
async fn a_motor_mode_change_ends_the_session() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    h.state.player.mode_changed();
    client.send(tungstenite::Message::text(r#"{"antennas": [0.1, -0.1]}"#)).await.unwrap();
    let error = next_json(&mut client).await;
    assert_eq!(error["type"], "error");
    h.wait_idle().await;
    assert!(h.fake.lock().unwrap().targets.is_empty());
    // The pose stays untrusted: a new session waits for a verified return home.
    assert!(h.state.player.needs_home());
}

#[tokio::test]
async fn an_app_start_ends_the_session() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    h.fake.lock().unwrap().app = Some(json!({"info": {"name": "radio"}, "state": "running"}));
    let error = next_json(&mut client).await;
    assert_eq!(error["type"], "error");
    h.wait_idle().await;
}
