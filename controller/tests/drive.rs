//! Manual drive (#152) over a real socket, against a fake daemon that records
//! the targets it is streamed and the gotos it is sent.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::ws::{Message, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::{AppState, Shared};
use serde_json::{Value, json};
use tokio_tungstenite::tungstenite;

#[derive(Default)]
struct Fake {
    motors: Option<String>,
    app: Option<Value>,
    targets: Vec<Value>,
    gotos: usize,
    running: Vec<String>,
    stops: usize,
    /// Drive sockets the controller opened to the daemon.
    upstreams: usize,
}

type FakeState = Arc<Mutex<Fake>>;

async fn fake_daemon(fake: FakeState) -> String {
    async fn set_target(State(fake): State<FakeState>, ws: WebSocketUpgrade) -> axum::response::Response {
        ws.on_upgrade(move |mut socket| async move {
            fake.lock().unwrap().upstreams += 1;
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
        .route(
            "/api/move/running",
            get(|State(f): State<FakeState>| async move {
                let running = f.lock().unwrap().running.clone();
                Json(Value::Array(running.into_iter().map(|uuid| json!({"uuid": uuid})).collect()))
            }),
        )
        .route(
            "/api/move/play/recorded-move-dataset/{*target}",
            post(|State(f): State<FakeState>| async move {
                f.lock().unwrap().running.push("expr".into());
                Json(json!({"uuid": "expr"}))
            }),
        )
        .route(
            "/api/motors/set_mode/{mode}",
            post(|State(f): State<FakeState>, Path(mode): Path<String>| async move {
                f.lock().unwrap().motors = Some(mode.clone());
                Json(json!({"status": "ok"}))
            }),
        )
        .route(
            "/api/daemon/stop",
            post(|State(f): State<FakeState>| async move {
                let mut f = f.lock().unwrap();
                f.stops += 1;
                f.motors = None;
                Json(json!({"job_id": "stop"}))
            }),
        )
        .route("/api/apps/list-available/installed", get(|| async { Json(json!([{"name": "radio", "extra": {}}])) }))
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
    harness_in(motors, std::env::temp_dir().join(format!("rc-drive-{}", std::process::id()))).await
}

async fn harness_in(motors: Option<&str>, dir: std::path::PathBuf) -> Harness {
    let fake = Arc::new(Mutex::new(Fake { motors: motors.map(str::to_string), ..Default::default() }));
    let url = fake_daemon(fake.clone()).await;
    let state =
        AppState::new(Daemon::new(&url, Duration::from_secs(2)), LogBroker::new(), dir, Duration::from_millis(10));
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

async fn call(controller: &str, path: &str) -> (u16, Value) {
    let response = reqwest::Client::new().post(format!("http://{controller}{path}")).send().await.unwrap();
    (response.status().as_u16(), response.json().await.unwrap_or(Value::Null))
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
async fn refused_from_another_site() {
    use tungstenite::client::IntoClientRequest;
    let h = harness(Some("enabled")).await;
    // A page on another origin (an app UI on another port) must not drive the robot.
    let mut request = format!("ws://{}/api/drive/ws", h.controller).into_client_request().unwrap();
    request.headers_mut().insert("origin", "http://127.0.0.1:8042".parse().unwrap());
    match tokio_tungstenite::connect_async(request).await {
        Err(tungstenite::Error::Http(response)) => assert_eq!(response.status().as_u16(), 403),
        other => panic!("expected 403, got {:?}", other.map(|_| ())),
    }
    assert!(!h.state.player.active());
    assert!(h.fake.lock().unwrap().targets.is_empty());
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
async fn a_silent_browser_ends_the_session() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    // Targets now and then keep the session going.
    for _ in 0..3 {
        tokio::time::sleep(Duration::from_millis(1500)).await;
        client.send(tungstenite::Message::text(r#"{"head": {"pitch": 0.1}}"#)).await.unwrap();
    }
    assert_eq!(h.state.player.current()["phase"], "driving");
    // Then nothing, as from a lost connection: the robot goes home.
    let error = tokio::time::timeout(Duration::from_secs(5), next_json(&mut client)).await.expect("session didn't end");
    assert_eq!(error, json!({"type": "error", "detail": "Connection lost"}));
    h.wait("the return home", |f| f.gotos == 1).await;
    h.wait_idle().await;
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
async fn plays_and_app_starts_are_refused_while_driving() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    let (status, _) = call(&h.controller, "/api/expressions/play/pollen-robotics/emotions/fear1").await;
    assert_eq!(status, 409);
    let (_, body) = call(&h.controller, "/api/apps/start/radio").await;
    assert_eq!(body["status"], "refused");
    assert!(h.fake.lock().unwrap().running.is_empty());
}

#[tokio::test]
async fn motors_off_and_on_while_driving_still_returns_home() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    assert_eq!(call(&h.controller, "/api/motors/set_mode/disabled").await.0, 200);
    // The session ends at once, so re-enabling isn't refused as busy and runs
    // its own return home.
    assert!(!h.state.player.driving(0));
    assert_eq!(call(&h.controller, "/api/motors/set_mode/enabled").await.0, 200);
    h.wait("the return home", |f| f.gotos == 1).await;
    h.wait_idle().await;
    assert!(!h.state.player.needs_home());
    assert_eq!(next_json(&mut client).await["type"], "error");
    // Closing the old socket doesn't send a second goto.
    drop(client);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(h.fake.lock().unwrap().gotos, 1);
}

#[tokio::test]
async fn power_off_ends_the_session_without_a_goto() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    let _ = call(&h.controller, "/api/daemon/stop").await;
    h.wait("the stop", |f| f.stops == 1).await;
    assert!(!h.state.player.active());
    drop(client);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(h.fake.lock().unwrap().gotos, 0);
    assert!(h.state.player.needs_home());
}

#[tokio::test]
async fn an_app_taking_over_skips_the_return_home() {
    let h = harness(Some("enabled")).await;
    let mut client = h.connect().await.unwrap();
    next_json(&mut client).await;
    h.fake.lock().unwrap().app = Some(json!({"info": {"name": "radio"}, "state": "running"}));
    assert_eq!(next_json(&mut client).await["type"], "error");
    h.wait_idle().await;
    assert_eq!(h.fake.lock().unwrap().gotos, 0);
    assert!(h.state.player.needs_home());
}

#[tokio::test]
async fn a_mode_change_mid_expression_keeps_the_pose_untrusted() {
    let h = harness(Some("enabled")).await;
    assert_eq!(call(&h.controller, "/api/expressions/play/pollen-robotics/emotions/fear1").await.0, 200);
    // Motors off mid-play: the move still ends on schedule, then the watcher's
    // goto "completes" without torque. That must not clear needs_home.
    h.state.player.mode_changed();
    h.fake.lock().unwrap().running.clear();
    h.wait("the return home", |f| f.gotos == 1).await;
    h.wait_idle().await;
    assert!(h.state.player.needs_home());
}

#[tokio::test]
async fn a_drive_whose_browser_lost_its_trust_while_waiting_never_starts() {
    // #341: the credential is checked again at the claim, so a revoke (here:
    // admin mode starting, which ends this loopback browser's trust) while
    // the drive waits either sees it open or keeps it from starting at all.
    let dir = std::env::temp_dir().join(format!("rc-drive-{}-trust", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let h = harness_in(Some("enabled"), dir).await;
    let power = h.state.power_lock.lock().await;
    let controller = h.controller.clone();
    let drive = tokio::spawn(async move {
        match tokio_tungstenite::connect_async(format!("ws://{controller}/api/drive/ws")).await {
            Ok(_) => 101,
            Err(tungstenite::Error::Http(response)) => response.status().as_u16(),
            Err(error) => panic!("{error}"),
        }
    });
    // It connected to the daemon: next it waits for the power lock.
    h.wait("the drive's daemon socket", |f| f.upstreams == 1).await;
    // The first admin pairs: loopback browsers aren't trusted any more.
    let http = reqwest::Client::new();
    let r = http.post(format!("http://{}/api/auth/pair", h.controller)).send().await.unwrap();
    let cookie = r.headers()["set-cookie"].to_str().unwrap().split(';').next().unwrap().to_string();
    let pair: Value = r.json().await.unwrap();
    let approve = reachy_controller::auth::ApproveAs {
        name: Some("laptop"),
        person: Some("Andry"),
        role: reachy_controller::auth::Role::Admin,
        shared: false,
        approver: None,
    };
    h.state.auth.approve_code(pair["code"].as_str().unwrap(), approve, "test", true).unwrap();
    let id = pair["id"].as_str().unwrap();
    let r =
        http.get(format!("http://{}/api/auth/pair/{id}", h.controller)).header("cookie", cookie).send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert!(h.state.auth.admin_mode());
    drop(power);
    assert_eq!(drive.await.unwrap(), 401);
    // No session, no return home, and the pose is still trusted.
    assert!(!h.state.player.active());
    assert_eq!(h.fake.lock().unwrap().gotos, 0);
    assert!(!h.state.player.needs_home());
}

#[tokio::test]
async fn a_cancelled_drive_leaves_no_session_and_no_return_home() {
    // A drive whose browser lost its trust between the claim and the check
    // after it is dropped before anything was sent (#341).
    let h = harness(Some("enabled")).await;
    let epoch = h.state.player.start_drive().await.expect("a drive starts");
    // Another epoch's cancel leaves this drive alone.
    h.state.player.cancel_drive(epoch + 1);
    assert!(h.state.player.driving(epoch));
    h.state.player.cancel_drive(epoch);
    assert!(!h.state.player.active());
    assert!(!h.state.player.needs_home());
    assert_eq!(h.fake.lock().unwrap().gotos, 0);
}
