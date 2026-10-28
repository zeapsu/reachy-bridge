//! The scripted fake daemon and the test harness shared by the controller's
//! integration tests (one file per area, #164).
#![allow(dead_code)]


pub use std::sync::atomic::{AtomicU64, Ordering};
pub use std::sync::{Arc, Mutex};
pub use std::time::Duration;

pub use axum::body::Body;
pub use axum::extract::{Path, Query, State};
pub use axum::http::{Request, StatusCode};
pub use axum::routing::{get, post};
pub use axum::{Json, Router};
pub use reachy_controller::daemon::Daemon;
pub use reachy_controller::logs::LogBroker;
pub use reachy_controller::state::{AppState, Shared};
pub use serde_json::{json, Value};
pub use tower::ServiceExt;

#[derive(Default)]
pub struct Fake {
    /// Daemon state; the backend is up when `motors` is `Some`.
    pub state: String,
    pub motors: Option<String>,
    pub engine: String,
    pub active_engine: Option<String>,
    pub app: Option<Value>,
    pub running_moves: Vec<String>,
    /// How long `move/goto` takes to answer (a slow daemon).
    pub goto_delay: Duration,
    pub kinematics_put_status: u16,
    pub set_mode_status: u16,
    pub calls: Vec<String>,
    /// The Hugging Face user the fake daemon is logged in as.
    pub hf_user: Option<String>,
}

pub type FakeState = Arc<Mutex<Fake>>;

pub fn record(fake: &FakeState, call: impl Into<String>) {
    fake.lock().unwrap().calls.push(call.into());
}

pub fn status_body(f: &Fake) -> Value {
    json!({
        "state": f.state,
        "backend_status": f.motors.as_ref().map(|m| json!({"motor_control_mode": m})),
        "error": null,
    })
}

/// A listing entry the way the daemon builds one from a Space.
pub fn catalog_entry(id: &str, private: bool) -> Value {
    let name = id.rsplit('/').next().unwrap();
    json!({
        "name": name,
        "source_kind": "hf_space",
        "description": format!("About {name}"),
        "url": format!("https://huggingface.co/spaces/{id}"),
        "extra": {"id": id, "private": private, "likes": 3, "cardData": {"title": name}},
    })
}

pub async fn fake_daemon(fake: FakeState) -> String {
    async fn start(State(fake): State<FakeState>, Query(q): Query<Vec<(String, String)>>) -> Json<Value> {
        let wake = q.iter().any(|(k, v)| k == "wake_up" && v == "true");
        record(&fake, format!("start wake={wake}"));
        let mut f = fake.lock().unwrap();
        f.state = "running".into();
        f.motors = Some(if wake { "enabled" } else { "disabled" }.into());
        f.active_engine = Some(f.engine.clone());
        Json(json!({"job_id": "start"}))
    }
    async fn stop(State(fake): State<FakeState>, Query(q): Query<Vec<(String, String)>>) -> Json<Value> {
        let sleep = q.iter().any(|(k, v)| k == "goto_sleep" && v == "true");
        record(&fake, format!("stop sleep={sleep}"));
        let mut f = fake.lock().unwrap();
        f.state = "stopped".into();
        f.motors = None;
        f.active_engine = None;
        Json(json!({"job_id": "stop"}))
    }
    async fn set_mode(State(fake): State<FakeState>, Path(mode): Path<String>) -> (StatusCode, Json<Value>) {
        record(&fake, format!("set_mode {mode}"));
        let mut f = fake.lock().unwrap();
        if f.set_mode_status >= 400 {
            return (StatusCode::from_u16(f.set_mode_status).unwrap(), Json(json!({"detail": "nope"})));
        }
        f.motors = Some(mode.clone());
        (StatusCode::OK, Json(json!({"status": format!("motors changed to {mode} mode")})))
    }
    async fn put_kinematics(State(fake): State<FakeState>, Json(body): Json<Value>) -> (StatusCode, Json<Value>) {
        record(&fake, format!("kinematics {}", body["engine"].as_str().unwrap_or("?")));
        let mut f = fake.lock().unwrap();
        if f.motors.is_some() {
            return (StatusCode::CONFLICT, Json(json!({"detail": "backend running"})));
        }
        if f.kinematics_put_status >= 400 {
            return (StatusCode::from_u16(f.kinematics_put_status).unwrap(), Json(json!({"detail": "bad engine"})));
        }
        f.engine = body["engine"].as_str().unwrap().to_string();
        (StatusCode::OK, Json(json!({"engine": f.engine, "active": null, "available": ["AnalyticalKinematics", "Placo"]})))
    }

    let app = Router::new()
        .route("/api/daemon/status", get(|State(f): State<FakeState>| async move { Json(status_body(&f.lock().unwrap())) }))
        .route(
            "/api/bridge/platform",
            get(|| async {
                Json(json!({"name": "jetson", "tier": "tested", "model": "NVIDIA Jetson", "features": ["camera"], "skipped": [], "reachy_mini": "1.12.0"}))
            }),
        )
        .route(
            "/api/state/ws/full",
            get(|State(f): State<FakeState>| async move {
                record(&f, "state-ws");
                StatusCode::SERVICE_UNAVAILABLE
            }),
        )
        .route("/api/daemon/start", post(start))
        .route("/api/daemon/stop", post(stop))
        .route("/api/motors/set_mode/{mode}", post(set_mode))
        .route(
            "/api/move/play/wake_up",
            post(|State(f): State<FakeState>| async move {
                record(&f, "wake_up");
                Json(json!({"uuid": "wake"}))
            }),
        )
        .route(
            "/api/move/running",
            get(|State(f): State<FakeState>| async move {
                let moves = f.lock().unwrap().running_moves.clone();
                Json(Value::Array(moves.into_iter().map(|uuid| json!({"uuid": uuid})).collect()))
            }),
        )
        .route(
            "/api/move/play/recorded-move-dataset/{*target}",
            post(|State(f): State<FakeState>, Path(target): Path<String>| async move {
                record(&f, format!("play {target}"));
                f.lock().unwrap().running_moves.push("expr".into());
                Json(json!({"uuid": "expr"}))
            }),
        )
        .route(
            "/api/move/recorded-move-datasets/list/{*dataset}",
            get(|State(f): State<FakeState>, Path(dataset): Path<String>| async move {
                record(&f, format!("list {dataset}"));
                Json(json!(["cheerful1", "sad1"]))
            }),
        )
        .route(
            "/api/move/stop",
            post(|State(f): State<FakeState>, Json(body): Json<Value>| async move {
                let uuid = body["uuid"].as_str().unwrap_or("?").to_string();
                record(&f, format!("move-stop {uuid}"));
                f.lock().unwrap().running_moves.retain(|m| *m != uuid);
                Json(json!({"message": "Move cancelled"}))
            }),
        )
        .route(
            "/api/move/goto",
            post(|State(f): State<FakeState>| async move {
                record(&f, "goto");
                let delay = f.lock().unwrap().goto_delay;
                if !delay.is_zero() {
                    // A slow goto also stays running until it's stopped.
                    tokio::time::sleep(delay).await;
                    f.lock().unwrap().running_moves.push("home".into());
                }
                Json(json!({"uuid": "home"}))
            }),
        )
        .route(
            "/api/bridge/kinematics",
            get(|State(f): State<FakeState>| async move {
                let f = f.lock().unwrap();
                Json(json!({"engine": f.engine, "active": f.active_engine, "available": ["AnalyticalKinematics", "Placo"]}))
            })
            .put(put_kinematics),
        )
        .route(
            "/api/apps/current-app-status",
            get(|State(f): State<FakeState>| async move { Json(f.lock().unwrap().app.clone().unwrap_or(Value::Null)) }),
        )
        .route(
            "/api/apps/list-available/installed",
            get(|| async {
                Json(json!([
                    {"name": "reachy_mini_radio", "description": "Radio", "extra": {"custom_app_url": "http://0.0.0.0:1"}},
                    {"name": "hello_world", "extra": {}},
                ]))
            }),
        )
        .route(
            "/api/apps/stop-current-app",
            post(|State(f): State<FakeState>| async move {
                record(&f, "stop-app");
                let mut f = f.lock().unwrap();
                if f.app.is_none() {
                    return (StatusCode::BAD_REQUEST, Json(json!({"detail": "No app is currently running"})));
                }
                f.app = None;
                (StatusCode::OK, Json(json!({"ok": true})))
            }),
        )
        .route(
            "/api/apps/start-app/{name}",
            post(|State(f): State<FakeState>, Path(name): Path<String>| async move {
                record(&f, format!("start-app {name}"));
                f.lock().unwrap().app = Some(json!({"info": {"name": name}, "state": "running"}));
                Json(json!({"ok": true}))
            }),
        )
        // The app catalog (#126).
        .route(
            "/api/apps/list-available/dashboard_selection",
            get(|State(f): State<FakeState>| async move {
                record(&f, "list curated");
                Json(json!([catalog_entry("pollen-robotics/reachy_mini_radio", false), catalog_entry("pollen-robotics/new_app", false)]))
            }),
        )
        .route(
            "/api/apps/list-available/hf_space",
            get(|State(f): State<FakeState>| async move {
                record(&f, "list all");
                Json(json!([catalog_entry("someone/other_app", false), catalog_entry("me/secret_app", true)]))
            }),
        )
        .route(
            "/api/apps/install",
            post(|State(f): State<FakeState>, Json(body): Json<Value>| async move {
                record(&f, format!("install {}", body));
                Json(json!({"job_id": "job-1"}))
            }),
        )
        .route(
            "/api/apps/install-private-space",
            post(|State(f): State<FakeState>, Json(body): Json<Value>| async move {
                record(&f, format!("install-private {}", body["space_id"].as_str().unwrap_or("?")));
                Json(json!({"job_id": "job-2"}))
            }),
        )
        .route(
            "/api/apps/job-status/{id}",
            get(|Path(id): Path<String>| async move {
                if id != "job-1" {
                    return (StatusCode::NOT_FOUND, Json(json!({"detail": "Job ID not found"})));
                }
                (StatusCode::OK, Json(json!({"command": "install", "status": "done", "logs": ["Downloading", "token hf_abcdefghijklmnop leaked", "Installed"]})))
            }),
        )
        .route(
            "/api/apps/remove/{name}",
            post(|State(f): State<FakeState>, Path(name): Path<String>| async move {
                record(&f, format!("remove {name}"));
                Json(json!({"job_id": "job-3"}))
            }),
        )
        .route(
            "/api/apps/update/{name}",
            post(|State(f): State<FakeState>, Path(name): Path<String>| async move {
                record(&f, format!("update {name}"));
                Json(json!({"job_id": "job-4"}))
            }),
        )
        .route(
            "/api/apps/check-updates",
            get(|| async {
                Json(json!({"apps_with_updates": [{"app_name": "hello_world", "space_id": "x/hello_world", "installed_sha": "a", "latest_sha": "b", "update_available": true}], "apps_checked": 2, "apps_skipped": 0}))
            }),
        )
        .route(
            "/api/hf-auth/status",
            get(|State(f): State<FakeState>| async move {
                let user = f.lock().unwrap().hf_user.clone();
                Json(json!({"is_logged_in": user.is_some(), "username": user}))
            }),
        )
        .route(
            "/api/hf-auth/save-token",
            post(|State(f): State<FakeState>, Json(body): Json<Value>| async move {
                let token = body["token"].as_str().unwrap_or_default().to_string();
                record(&f, format!("save-token {token}"));
                if token != "hf_goodtoken123" {
                    return (StatusCode::BAD_REQUEST, Json(json!({"detail": format!("Invalid token {token}")})));
                }
                f.lock().unwrap().hf_user = Some("andry".into());
                (StatusCode::OK, Json(json!({"status": "success", "username": "andry"})))
            }),
        )
        .route(
            "/api/hf-auth/token",
            axum::routing::delete(|State(f): State<FakeState>| async move {
                record(&f, "delete-token");
                f.lock().unwrap().hf_user = None;
                Json(json!({"status": "success"}))
            }),
        )
        .with_state(fake);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

pub fn request(method: &str, uri: &str, body: Option<Value>) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(body.map_or_else(Body::empty, |b| Body::from(b.to_string())))
        .unwrap()
}

pub struct Harness {
    pub fake: FakeState,
    pub state: Shared,
    pub _dir: TempDir,
}

pub struct TempDir(std::path::PathBuf);

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub async fn harness(init: impl FnOnce(&mut Fake)) -> Harness {
    let mut fake = Fake {
        state: "stopped".into(),
        engine: "AnalyticalKinematics".into(),
        ..Default::default()
    };
    init(&mut fake);
    let fake = Arc::new(Mutex::new(fake));
    let url = fake_daemon(fake.clone()).await;
    let dir = std::env::temp_dir().join(format!("rc-test-{}-{}", std::process::id(), rand_suffix()));
    let state = AppState::new(Daemon::new(&url, Duration::from_secs(2)), LogBroker::new(), dir.clone(), Duration::from_millis(10));
    Harness { fake, state, _dir: TempDir(dir) }
}

/// A counter, not the clock: macOS clocks tick in microseconds, so tests
/// starting together got the same directory and deleted each other's.
pub fn rand_suffix() -> u64 {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    NEXT.fetch_add(1, Ordering::Relaxed)
}

impl Harness {
    pub async fn call(&self, method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
        self.send(request(method, uri, body)).await
    }

    pub async fn send(&self, request: Request<Body>) -> (StatusCode, Value) {
        let response = reachy_controller::app(self.state.clone()).oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
    }

    pub fn calls(&self) -> Vec<String> {
        self.fake.lock().unwrap().calls.clone()
    }
}
