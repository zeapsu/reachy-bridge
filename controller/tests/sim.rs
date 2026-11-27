//! Sim mode (#149): switching the controller between the robot's daemon and
//! a sim daemon, against two scripted fake daemons in `common`.

mod common;

use common::*;
use reachy_controller::auth::AuthConfig;
use reachy_controller::sim::SimMode;

struct Sim {
    robot: FakeState,
    sim: FakeState,
    h: Harness,
}

/// The robot's fake daemon is `h.fake`; a second one plays the sim daemon,
/// started by nobody (no systemd unit).
async fn sim_harness(init: impl FnOnce(&mut Fake)) -> Sim {
    let h = harness(init).await;
    let sim = Arc::new(Mutex::new(Fake {
        state: "stopped".into(),
        engine: "AnalyticalKinematics".into(),
        ..Default::default()
    }));
    let sim_url = fake_daemon(sim.clone()).await;
    let robot_url = h.state.daemon.base_url();
    let state = AppState::with_sim(
        Daemon::new(&robot_url, Duration::from_secs(2)),
        LogBroker::new(),
        h._dir.path().to_path_buf(),
        Duration::from_millis(10),
        AuthConfig::default(),
        SimMode::new(&robot_url, &sim_url, None, None),
    );
    let robot = h.fake.clone();
    Sim { robot, sim, h: Harness { state, ..h } }
}

fn calls(fake: &FakeState) -> Vec<String> {
    fake.lock().unwrap().calls.clone()
}

fn with_target(method: &str, uri: &str, target: &str) -> Request<Body> {
    Request::builder().method(method).uri(uri).header("x-reachy-target", target).body(Body::empty()).unwrap()
}

#[tokio::test]
async fn entering_is_refused_while_the_robot_is_on() {
    let s = sim_harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    let (status, _) = s.h.call("POST", "/api/sim/enter", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    let (_, body) = s.h.call("GET", "/api/sim", None).await;
    assert_eq!(body, json!({"active": false, "target": "robot"}));
}

#[tokio::test]
async fn sim_mode_moves_every_action_to_the_sim_and_back() {
    let s = sim_harness(|_| {}).await;
    let (status, body) = s.h.call("POST", "/api/sim/enter", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (_, body) = s.h.call("GET", "/api/daemon/status", None).await;
    assert_eq!(body["target"], "sim");

    s.h.call("POST", "/api/daemon/start", None).await;
    assert_eq!(calls(&s.sim), vec!["start wake=true"]);
    assert!(calls(&s.robot).is_empty(), "the robot was never touched: {:?}", calls(&s.robot));

    let (status, _) = s.h.call("POST", "/api/sim/exit", None).await;
    assert_eq!(status, StatusCode::OK);
    let (_, body) = s.h.call("GET", "/api/daemon/status", None).await;
    assert_eq!(body["target"], "robot");
    assert_eq!(body["status"], "stopped", "the robot is still off");
    s.h.call("POST", "/api/daemon/start", None).await;
    assert_eq!(calls(&s.robot), vec!["start wake=true"]);
}

#[tokio::test]
async fn a_tab_that_missed_the_switch_is_refused() {
    let s = sim_harness(|_| {}).await;
    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, body) = s.h.send(with_target("POST", "/api/daemon/start", "robot")).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body["detail"].as_str().unwrap().contains("simulation"));

    s.h.call("POST", "/api/sim/exit", None).await;
    let (status, _) = s.h.send(with_target("POST", "/api/daemon/start", "sim")).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(calls(&s.robot).is_empty() && calls(&s.sim).is_empty());

    let (status, _) = s.h.send(with_target("POST", "/api/daemon/start", "robot")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(calls(&s.robot), vec!["start wake=true"]);
}

#[tokio::test]
async fn apps_run_on_the_sim_with_their_own_pins() {
    let s = sim_harness(|_| {}).await;
    let (status, _) = s.h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["hello_world"]}))).await;
    assert_eq!(status, StatusCode::OK);
    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, _) = s.h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_ne!(status, StatusCode::CONFLICT, "apps aren't refused in sim mode");
    assert!(calls(&s.robot).is_empty(), "nothing reached the robot: {:?}", calls(&s.robot));
    let (status, _) = s.h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["reachy_mini_radio"]}))).await;
    assert_eq!(status, StatusCode::OK);
    let (_, sim_pins) = s.h.call("GET", "/api/apps/favorites", None).await;
    assert_eq!(sim_pins["favorites"], json!(["reachy_mini_radio"]));
    s.h.call("POST", "/api/sim/exit", None).await;
    let (_, robot_pins) = s.h.call("GET", "/api/apps/favorites", None).await;
    assert_eq!(robot_pins["favorites"], json!(["hello_world"]), "the robot's pins are its own");
}

#[tokio::test]
async fn entering_needs_the_robot_confirmed_off() {
    let sim = Arc::new(Mutex::new(Fake { state: "stopped".into(), ..Default::default() }));
    let sim_url = fake_daemon(sim.clone()).await;
    let robot_url = "http://127.0.0.1:9";
    let state = AppState::with_sim(
        Daemon::new(robot_url, Duration::from_millis(200)),
        LogBroker::new(),
        std::env::temp_dir().join("rc-test-sim-unreachable"),
        Duration::from_millis(10),
        AuthConfig::default(),
        SimMode::new(robot_url, &sim_url, None, None),
    );
    let response = reachy_controller::app(state.clone())
        .oneshot(Request::post("/api/sim/enter").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(!state.sim.active());
}

#[tokio::test]
async fn a_drive_socket_from_a_stale_tab_is_refused() {
    let s = sim_harness(|_| {}).await;
    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, _) = s.h.call("GET", "/api/drive/ws?target=robot", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
}

#[tokio::test]
async fn leaving_keeps_expressions_off_until_the_robot_is_home() {
    let s = sim_harness(|_| {}).await;
    s.h.call("POST", "/api/sim/enter", None).await;
    s.h.state.player.set_needs_home(false);
    s.h.call("POST", "/api/sim/exit", None).await;
    assert!(s.h.state.player.needs_home());
}

#[tokio::test]
async fn the_sim_never_sets_the_hosts_sound() {
    let s = sim_harness(|_| {}).await;
    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, body) = s.h.call("POST", "/api/volume/set", Some(json!({"volume": 10}))).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["detail"], "Not in simulation");
}

/// A fake `systemctl` that logs its arguments.
fn fake_systemctl(dir: &std::path::Path) -> (std::path::PathBuf, std::path::PathBuf) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(dir).unwrap();
    let log = dir.join("systemctl.log");
    let script = dir.join("systemctl");
    std::fs::write(&script, format!("#!/bin/sh\necho \"$*\" >> '{}'\n", log.display())).unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    (script, log)
}

#[tokio::test]
async fn the_robots_daemon_is_stopped_for_the_whole_of_sim_mode() {
    let h = harness(|_| {}).await;
    let sim = Arc::new(Mutex::new(Fake { state: "stopped".into(), ..Default::default() }));
    let sim_url = fake_daemon(sim.clone()).await;
    let robot_url = h.state.daemon.base_url();
    let (systemctl, log) = fake_systemctl(&h._dir.path().join("bin"));
    let state = AppState::with_sim(
        Daemon::new(&robot_url, Duration::from_secs(2)),
        LogBroker::new(),
        h._dir.path().to_path_buf(),
        Duration::from_millis(10),
        AuthConfig::default(),
        SimMode::new(&robot_url, &sim_url, Some("sim.service".into()), Some("robot.service".into()))
            .with_systemctl(systemctl.to_str().unwrap()),
    );
    let s = Harness { state, ..h };
    let (status, body) = s.call("POST", "/api/sim/enter", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = s.call("POST", "/api/sim/exit", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let calls = std::fs::read_to_string(log).unwrap();
    assert_eq!(
        calls.lines().collect::<Vec<_>>(),
        vec![
            "--user stop robot.service",
            "--user start sim.service",
            "--user stop sim.service",
            "--user start robot.service"
        ]
    );
}

async fn raw(s: &Sim, uri: &str) -> (StatusCode, String, Vec<u8>) {
    let request = Request::builder().uri(uri).body(Body::empty()).unwrap();
    let response = reachy_controller::app(s.h.state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let content_type =
        response.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or_default().to_string();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
    (status, content_type, bytes.to_vec())
}

#[tokio::test]
async fn sim_sounds_are_served_only_in_sim_mode() {
    let s = sim_harness(|_| {}).await;
    let file = format!("/api/sim/sounds/{SOUND_TOKEN}");
    // The robot's sounds stay on the robot.
    assert_eq!(raw(&s, &file).await.0, StatusCode::CONFLICT);
    assert_eq!(raw(&s, "/api/sim/sounds-stream").await.0, StatusCode::CONFLICT);

    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, content_type, body) = raw(&s, &file).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(content_type, "audio/x-wav");
    assert_eq!(body, b"RIFF");
    assert_eq!(raw(&s, &format!("/api/sim/sounds/{}", "f".repeat(32))).await.0, StatusCode::NOT_FOUND);
    // Not a token: never forwarded.
    assert_eq!(raw(&s, "/api/sim/sounds/..%2F..%2Fdaemon").await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn the_test_sound_plays_in_the_browser_in_sim_mode() {
    let s = sim_harness(|_| {}).await;
    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, _) = s.h.call("POST", "/api/volume/test-sound", None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(calls(&s.sim).contains(&"test-sound".to_string()));
    // Setting the volume still is refused: it would set the host's speaker.
    let (status, _) = s.h.call("POST", "/api/volume/set", Some(json!({"volume": 10}))).await;
    assert_eq!(status, StatusCode::CONFLICT);
    // So is playing a named file: it would come back to the browser.
    let (status, _) = s.h.call("POST", "/api/media/play_sound", Some(json!({"file": "/home/x/a.wav"}))).await;
    assert_eq!(status, StatusCode::CONFLICT);
}

#[tokio::test]
async fn the_sim_has_its_own_wake_app_switch() {
    // The fake daemons both have hello_world installed.
    let mut s = sim_harness(|_| {}).await;
    Arc::get_mut(&mut s.h.state).unwrap().wake_app = "hello_world".into();
    let uri = "/api/system/wake-app";
    let file = |name: &str| s.h._dir.path().join(name);
    assert_eq!(s.h.call("POST", "/api/sim/enter", None).await.0, StatusCode::OK);
    assert_eq!(s.h.call("PUT", uri, Some(json!({"enabled": true}))).await.0, StatusCode::OK);
    assert!(file("wake-app-sim.json").exists());
    assert!(!file("wake-app.json").exists(), "the robot's switch is untouched");
    assert_eq!(s.h.call("POST", "/api/sim/exit", None).await.0, StatusCode::OK);
    assert_eq!(s.h.call("GET", uri, None).await.1["enabled"], false, "on for the sim only");
    // A power-on of the robot starts nothing.
    s.h.call("POST", "/api/daemon/start", None).await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(calls(&s.robot), vec!["start wake=true"]);
}
