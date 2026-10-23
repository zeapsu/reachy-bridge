//! Controller routes against a scripted fake daemon on a loopback port.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Path, Query, State};
use axum::http::{Request, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::LogBroker;
use reachy_controller::state::{AppState, Shared};
use serde_json::{json, Value};
use tower::ServiceExt;

#[derive(Default)]
struct Fake {
    /// Daemon state; the backend is up when `motors` is `Some`.
    state: String,
    motors: Option<String>,
    engine: String,
    active_engine: Option<String>,
    app: Option<Value>,
    running_moves: Vec<String>,
    kinematics_put_status: u16,
    set_mode_status: u16,
    calls: Vec<String>,
}

type FakeState = Arc<Mutex<Fake>>;

fn record(fake: &FakeState, call: impl Into<String>) {
    fake.lock().unwrap().calls.push(call.into());
}

fn status_body(f: &Fake) -> Value {
    json!({
        "state": f.state,
        "backend_status": f.motors.as_ref().map(|m| json!({"motor_control_mode": m})),
        "error": null,
    })
}

async fn fake_daemon(fake: FakeState) -> String {
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
            "/api/move/goto",
            post(|State(f): State<FakeState>| async move {
                record(&f, "goto");
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
        .with_state(fake);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

struct Harness {
    fake: FakeState,
    state: Shared,
    _dir: TempDir,
}

struct TempDir(std::path::PathBuf);

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn harness(init: impl FnOnce(&mut Fake)) -> Harness {
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

fn rand_suffix() -> u128 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
}

impl Harness {
    async fn call(&self, method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
        let request = Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/json")
            .body(body.map_or_else(Body::empty, |b| Body::from(b.to_string())))
            .unwrap();
        let response = reachy_controller::app(self.state.clone()).oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
    }

    fn calls(&self) -> Vec<String> {
        self.fake.lock().unwrap().calls.clone()
    }
}

#[tokio::test]
async fn status_reports_asleep_and_offline() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    let (_, body) = h.call("GET", "/api/daemon/status", None).await;
    assert_eq!(body["status"], "asleep");
    assert_eq!(body["running"], false);
    assert_eq!(body["motors"], "disabled");

    let state = AppState::new(Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)), LogBroker::new(), "/nonexistent".into(), Duration::from_millis(10));
    let response = reachy_controller::app(state)
        .oneshot(Request::get("/api/daemon/status").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap()).unwrap();
    assert_eq!(body, json!({"reachable": false, "running": false, "manageable": false, "status": "offline"}));
}

#[tokio::test]
async fn power_on_from_off_starts_with_wake_and_from_asleep_wakes_in_place() {
    let h = harness(|_| {}).await;
    h.call("POST", "/api/daemon/start", None).await;
    assert_eq!(h.calls(), vec!["start wake=true"]);

    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    h.call("POST", "/api/daemon/start", None).await;
    assert_eq!(h.calls(), vec!["set_mode enabled", "wake_up"]);
}

#[tokio::test]
async fn concurrent_power_on_starts_once() {
    let h = harness(|_| {}).await;
    let (a, b) = tokio::join!(h.call("POST", "/api/daemon/start", None), h.call("POST", "/api/daemon/start", None));
    assert_eq!(a.0, StatusCode::OK);
    assert_eq!(b.0, StatusCode::OK);
    assert_eq!(h.calls(), vec!["start wake=true"]);
}

#[tokio::test]
async fn power_off_stops_the_app_and_skips_the_sleep_move_when_asleep() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["stop-app", "stop sleep=true"]);
    assert!(h.state.player.needs_home());

    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["stop sleep=false"]);
}

#[tokio::test]
async fn kinematics_switch_while_on_cycles_power() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("AnalyticalKinematics".into());
    })
    .await;
    let (status, body) = h.call("PUT", "/api/daemon/kinematics", Some(json!({"engine": "Placo"}))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["restarted"], true);
    assert_eq!(h.calls(), vec!["stop sleep=true", "kinematics Placo", "start wake=true"]);
}

#[tokio::test]
async fn kinematics_switch_keeps_an_asleep_robot_asleep() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
        f.active_engine = Some("AnalyticalKinematics".into());
    })
    .await;
    h.call("PUT", "/api/daemon/kinematics", Some(json!({"engine": "Placo"}))).await;
    assert_eq!(h.calls(), vec!["stop sleep=false", "kinematics Placo", "start wake=false"]);
}

#[tokio::test]
async fn kinematics_switch_refused_while_an_app_runs_and_rolled_back_on_error() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("AnalyticalKinematics".into());
        f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
    })
    .await;
    let (status, _) = h.call("PUT", "/api/daemon/kinematics", Some(json!({"engine": "Placo"}))).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(h.calls().is_empty());

    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("AnalyticalKinematics".into());
        f.kinematics_put_status = 422;
    })
    .await;
    let (status, body) = h.call("PUT", "/api/daemon/kinematics", Some(json!({"engine": "Bogus"}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["detail"], "bad engine");
    assert_eq!(h.calls(), vec!["stop sleep=true", "kinematics Bogus", "start wake=true"]);
}

#[tokio::test]
async fn kinematics_same_engine_is_a_no_op_and_bad_body_is_422() {
    let h = harness(|_| {}).await;
    let (status, body) = h.call("PUT", "/api/daemon/kinematics", Some(json!({"engine": "AnalyticalKinematics"}))).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["restarted"], false);
    let (status, _) = h.call("PUT", "/api/daemon/kinematics", Some(json!({"nope": 1}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(h.calls().is_empty());
}

#[tokio::test]
async fn gravity_compensation_guards() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("AnalyticalKinematics".into());
    })
    .await;
    let (status, body) = h.call("POST", "/api/motors/set_mode/gravity_compensation", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["detail"], "Gravity compensation needs Placo kinematics");

    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("Placo".into());
        f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
    })
    .await;
    let (status, _) = h.call("POST", "/api/motors/set_mode/gravity_compensation", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(h.calls().is_empty());

    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("Placo".into());
        f.set_mode_status = 500;
    })
    .await;
    let (status, _) = h.call("POST", "/api/motors/set_mode/gravity_compensation", None).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    let (status, _) = h.call("POST", "/api/motors/set_mode/turbo", None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn app_start_clears_a_crashed_app_and_wakes_the_robot_first() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
        f.app = Some(json!({"info": {"name": "hello_world"}, "state": "error", "error": "boom"}));
    })
    .await;
    let (status, body) = h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "loading");
    assert_eq!(body["ui_port"], 1);
    assert_eq!(h.calls(), vec!["stop-app", "set_mode enabled", "wake_up", "start-app reachy_mini_radio"]);
}

#[tokio::test]
async fn app_start_leaves_gravity_compensation_and_refuses_unknown_apps() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("gravity_compensation".into());
    })
    .await;
    h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(h.calls(), vec!["set_mode enabled", "start-app reachy_mini_radio"]);
    let (status, _) = h.call("POST", "/api/apps/start/nope", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn app_status_maps_daemon_states() {
    let h = harness(|f| f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "starting"}))).await;
    let (_, body) = h.call("GET", "/api/apps/status", None).await;
    assert_eq!(body[0]["name"], "reachy_mini_radio");
    assert_eq!(body[0]["display"], "Radio");
    assert_eq!(body[0]["status"], "loading");
    assert_eq!(body[0]["has_ui"], true);
    assert_eq!(body[1]["status"], "stopped");
    assert_eq!(body[1]["has_ui"], false);
}

#[tokio::test]
async fn favorites_default_validate_and_persist() {
    let h = harness(|_| {}).await;
    let (_, body) = h.call("GET", "/api/apps/favorites", None).await;
    assert_eq!(body["favorites"], json!(["reachy_mini_radio", "hello_world"]));
    let (status, _) = h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["nope"]}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let (status, _) = h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["hello_world", "hello_world"]}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let (status, _) = h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["hello_world"]}))).await;
    assert_eq!(status, StatusCode::OK);
    let (_, body) = h.call("GET", "/api/apps/favorites", None).await;
    assert_eq!(body["favorites"], json!(["hello_world"]));
}

#[tokio::test]
async fn expressions_serialize_return_home_and_block_while_an_app_runs() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    let uri = "/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/cheerful1";
    let (status, body) = h.call("POST", uri, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["dataset"], "pollen-robotics/reachy-mini-emotions-library");
    let (status, _) = h.call("POST", uri, None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    // An app start is refused while the expression plays.
    let (_, body) = h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(body["status"], "refused");

    // The move ends: the player goes home and clears the session.
    h.fake.lock().unwrap().running_moves.clear();
    for _ in 0..100 {
        if h.call("GET", "/api/expressions/current", None).await.1["active"] == false {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert_eq!(h.call("GET", "/api/expressions/current", None).await.1["active"], false);
    assert!(h.calls().contains(&"goto".to_string()));

    h.fake.lock().unwrap().app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
    let (status, _) = h.call("POST", uri, None).await;
    assert_eq!(status, StatusCode::CONFLICT);
}

#[tokio::test]
async fn expressions_refuse_until_homed_after_a_motor_change() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    h.call("POST", "/api/motors/set_mode/disabled", None).await;
    let uri = "/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/cheerful1";
    let (status, body) = h.call("POST", uri, None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body["detail"].as_str().unwrap().contains("not homed"));
}

#[tokio::test]
async fn wildcard_routes_refuse_traversal_to_other_daemon_paths() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    for uri in [
        "/api/expressions/play/..%2F..%2F..%2Fdaemon/start%3Fwake_up=true",
        "/api/expressions/play/%2e%2e/%2e%2e/daemon/stop",
        "/api/expressions/play/pollen-robotics/x/..%2F..%2Fmotors%2Fset_mode%2Fdisabled",
    ] {
        let (status, _) = h.call("POST", uri, None).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{uri}");
    }
    let (status, _) = h.call("GET", "/api/move/recorded-move-datasets/list/..%2F..%2F..%2Fdaemon%2Fstatus", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(h.calls().is_empty(), "{:?}", h.calls());
}

#[tokio::test]
async fn enabling_motors_skips_the_return_home_while_an_app_runs() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
        f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
    })
    .await;
    let (status, _) = h.call("POST", "/api/motors/set_mode/enabled", None).await;
    assert_eq!(status, StatusCode::OK);
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(h.calls(), vec!["set_mode enabled"]);
    assert!(h.state.player.needs_home());
}

#[tokio::test]
async fn a_failed_mode_change_still_distrusts_the_pose() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.set_mode_status = 500;
    })
    .await;
    let (status, _) = h.call("POST", "/api/motors/set_mode/disabled", None).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert!(h.state.player.needs_home());
}

#[tokio::test]
async fn dataset_lists_are_fetched_once_and_kept_on_disk() {
    let h = harness(|_| {}).await;
    let uri = "/api/move/recorded-move-datasets/list/pollen-robotics/reachy-mini-emotions-library";
    let (a, b) = tokio::join!(h.call("GET", uri, None), h.call("GET", uri, None));
    assert_eq!(a.1, json!(["cheerful1", "sad1"]));
    assert_eq!(b.1, a.1);
    assert_eq!(h.calls(), vec!["list pollen-robotics/reachy-mini-emotions-library"]);
    // A restarted controller reads the cache file instead of the daemon.
    let reloaded = reachy_controller::datasets::DatasetCache::new(h.state.state_dir.join("datasets.json"));
    assert_eq!(reloaded.get("pollen-robotics/reachy-mini-emotions-library"), Some(json!(["cheerful1", "sad1"])));
}

#[tokio::test]
async fn prefetch_runs_only_while_the_robot_is_off() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    let task = tokio::spawn(reachy_controller::datasets::prefetch(h.state.clone()));
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(h.calls().is_empty(), "{:?}", h.calls());
    task.abort();

    let h = harness(|_| {}).await;
    tokio::time::timeout(Duration::from_secs(5), reachy_controller::datasets::prefetch(h.state.clone()))
        .await
        .unwrap();
    assert_eq!(h.calls().len(), 2, "{:?}", h.calls());
}

#[tokio::test]
async fn gravity_compensation_waits_for_an_expression() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.active_engine = Some("Placo".into());
    })
    .await;
    let (status, _) =
        h.call("POST", "/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/cheerful1", None).await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = h.call("POST", "/api/motors/set_mode/gravity_compensation", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body["detail"].as_str().unwrap().contains("expression"));
}

#[tokio::test]
async fn volume_is_validated() {
    let h = harness(|_| {}).await;
    let (status, _) = h.call("POST", "/api/volume/set", Some(json!({"volume": 101}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = h.call("POST", "/api/volume/set", Some(json!({"volume": "50"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn log_stream_rejects_unknown_sources_and_starts_with_a_reset() {
    let h = harness(|_| {}).await;
    let (status, _) = h.call("GET", "/api/logs/stream?source=nope", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    h.state.logs.publish("daemon", "hello");
    let response = reachy_controller::app(h.state.clone())
        .oneshot(Request::get("/api/logs/stream?source=daemon").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let mut body = response.into_body().into_data_stream();
    use futures_util::StreamExt;
    let first = body.next().await.unwrap().unwrap();
    let text = String::from_utf8_lossy(&first);
    assert!(text.starts_with("event: log-reset\ndata: "), "{text}");
    assert!(text.contains(r#""lines":["hello"]"#), "{text}");
}
