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
    let sim = Arc::new(Mutex::new(Fake { state: "stopped".into(), engine: "AnalyticalKinematics".into(), ..Default::default() }));
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
    s.h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": []}))).await;
    s.h.call("POST", "/api/sim/enter", None).await;
    let (status, _) = s.h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_ne!(status, StatusCode::CONFLICT, "apps aren't refused in sim mode");
    assert!(calls(&s.robot).is_empty(), "nothing reached the robot: {:?}", calls(&s.robot));
    assert!(s.h._dir.path().join("favorites.json").exists());
    assert!(!s.h._dir.path().join("favorites-sim.json").exists());
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
    assert_eq!(body["detail"], "Sound is off in sim mode");
}

/// A `systemctl` on PATH that logs its arguments and, for the sim unit,
/// leaves the sim daemon to the test.
fn fake_systemctl(dir: &std::path::Path) -> std::path::PathBuf {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(dir).unwrap();
    let log = dir.join("systemctl.log");
    let script = dir.join("systemctl");
    std::fs::write(&script, format!("#!/bin/sh\necho \"$*\" >> '{}'\n", log.display())).unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    log
}

#[tokio::test]
async fn the_robots_daemon_is_stopped_for_the_whole_of_sim_mode() {
    let h = harness(|_| {}).await;
    let sim = Arc::new(Mutex::new(Fake { state: "stopped".into(), ..Default::default() }));
    let sim_url = fake_daemon(sim.clone()).await;
    let robot_url = h.state.daemon.base_url();
    let bin = h._dir.path().join("bin");
    let log = fake_systemctl(&bin);
    // SAFETY: only this test spawns systemctl, and no test here reads PATH
    // concurrently in a way a torn update would break.
    let path = format!("{}:{}", bin.display(), std::env::var("PATH").unwrap_or_default());
    unsafe { std::env::set_var("PATH", path) };
    let state = AppState::with_sim(
        Daemon::new(&robot_url, Duration::from_secs(2)),
        LogBroker::new(),
        h._dir.path().to_path_buf(),
        Duration::from_millis(10),
        AuthConfig::default(),
        SimMode::new(&robot_url, &sim_url, Some("sim.service".into()), Some("robot.service".into())),
    );
    let s = Harness { state, ..h };
    let (status, body) = s.call("POST", "/api/sim/enter", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = s.call("POST", "/api/sim/exit", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let calls = std::fs::read_to_string(log).unwrap();
    assert_eq!(
        calls.lines().collect::<Vec<_>>(),
        vec!["--user stop robot.service", "--user start sim.service", "--user stop sim.service", "--user start robot.service"]
    );
}
