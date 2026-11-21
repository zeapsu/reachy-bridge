//! Robot power: status, power on and off, and what power-off stops first. Against the scripted fake daemon in `common`.

mod common;

use common::*;

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

    let state = AppState::new(
        Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)),
        LogBroker::new(),
        "/nonexistent".into(),
        Duration::from_millis(10),
    );
    let response = reachy_controller::app(state)
        .oneshot(Request::get("/api/daemon/status").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let body: Value =
        serde_json::from_slice(&axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap()).unwrap();
    assert_eq!(
        body,
        json!({"reachable": false, "running": false, "manageable": false, "status": "offline", "target": "robot"})
    );
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
async fn power_off_stops_a_playing_expression_first_and_skips_its_return_home() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    let (status, _) =
        h.call("POST", "/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/sad1", None).await;
    assert_eq!(status, StatusCode::OK);
    h.call("POST", "/api/daemon/stop", None).await;
    // Give the player's watcher time to notice the move is gone.
    tokio::time::sleep(Duration::from_millis(200)).await;
    let calls: Vec<String> = h.calls().into_iter().filter(|c| !c.starts_with("list ")).collect();
    assert_eq!(
        calls,
        vec!["play pollen-robotics/reachy-mini-emotions-library/sad1", "move-stop expr", "stop sleep=true"]
    );
    assert!(!h.state.player.active());
}

#[tokio::test]
async fn a_return_home_posted_during_power_off_is_stopped() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.goto_delay = Duration::from_millis(300);
    })
    .await;
    let (status, _) =
        h.call("POST", "/api/expressions/play/pollen-robotics/reachy-mini-emotions-library/sad1", None).await;
    assert_eq!(status, StatusCode::OK);
    // The expression ends; the watcher posts its return home, which the
    // daemon is slow to accept. Power off lands in that window.
    h.fake.lock().unwrap().running_moves.clear();
    tokio::time::sleep(Duration::from_millis(100)).await;
    h.call("POST", "/api/daemon/stop", None).await;
    tokio::time::sleep(Duration::from_millis(400)).await;
    let calls: Vec<String> = h.calls().into_iter().filter(|c| !c.starts_with("list ")).collect();
    assert_eq!(
        calls,
        vec!["play pollen-robotics/reachy-mini-emotions-library/sad1", "goto", "stop sleep=true", "move-stop home"]
    );
    assert!(h.fake.lock().unwrap().running_moves.is_empty());
}

// --- The daemon's idle reset (#292) ---------------------------------------
// After an app exits on its own (or a remote session drops), the daemon
// puts the robot to sleep a moment later. A power off during it must wait,
// then power off from asleep, not cut the sleep move short.

#[tokio::test]
async fn power_off_after_an_app_exits_on_its_own_waits_for_the_idle_reset() {
    for app_state in ["error", "done"] {
        let h = harness(|f| {
            f.state = "running".into();
            f.motors = Some("enabled".into());
            f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": app_state}));
            f.idle_reset_polls = 3;
        })
        .await;
        let (status, body) = h.call("POST", "/api/daemon/stop", None).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        // No stop-current-app (the app isn't active), no stop mid-reset, and
        // the robot is asleep by then, so no second sleep move.
        assert_eq!(h.calls(), vec!["idle-reset done", "stop sleep=false"], "{app_state}");
        assert_eq!(h.fake.lock().unwrap().idle_reset_reads, 4);
    }
}

#[tokio::test]
async fn power_off_after_a_remote_session_drop_waits_for_the_idle_reset() {
    // No app at all: only the runtime's route shows the reset.
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("gravity_compensation".into());
        f.idle_reset_polls = 2;
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["idle-reset done", "stop sleep=false"]);
}

#[tokio::test]
async fn power_off_without_an_idle_reset_reads_once_and_does_not_wait() {
    // Asleep already.
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["stop sleep=false"]);
    assert_eq!(h.fake.lock().unwrap().idle_reset_reads, 1);

    // A requested stop of a running app: keep-awake skips the reset.
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["stop-app", "stop sleep=true"]);
    assert_eq!(h.fake.lock().unwrap().idle_reset_reads, 1);
}

#[tokio::test]
async fn power_off_waits_for_an_idle_reset_its_own_app_stop_falls_back_to() {
    // reachy-mini 1.14.0: a stop whose return home fails requests the reset.
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}));
        f.idle_reset_polls = 2;
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["stop-app", "idle-reset done", "stop sleep=false"]);
}

#[tokio::test]
async fn power_off_goes_ahead_when_the_idle_reset_never_ends() {
    let mut h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.idle_reset_polls = u32::MAX;
    })
    .await;
    Arc::get_mut(&mut h.state).unwrap().idle_reset_timeout = Duration::from_millis(600);
    let (status, _) = h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(h.fake.lock().unwrap().idle_reset_reads > 1);
    assert_eq!(h.calls(), vec!["stop sleep=true"]);
}

#[tokio::test]
async fn power_off_without_the_idle_reset_route_works_as_before() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.idle_reset_missing = true;
    })
    .await;
    h.call("POST", "/api/daemon/stop", None).await;
    assert_eq!(h.calls(), vec!["stop sleep=true"]);
}

#[tokio::test]
async fn an_error_from_the_idle_reset_route_reads_as_pending() {
    let mut h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.idle_reset_status = 500;
    })
    .await;
    Arc::get_mut(&mut h.state).unwrap().idle_reset_timeout = Duration::from_millis(600);
    h.call("POST", "/api/daemon/stop", None).await;
    // It waited (and polled) until the timeout, then powered off.
    assert!(h.fake.lock().unwrap().idle_reset_reads > 1);
    assert_eq!(h.calls(), vec!["stop sleep=true"]);
}
