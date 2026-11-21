//! Apps: start, status mapping and favorites. Against the scripted fake daemon in `common`.

mod common;

use common::*;

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

// --- The daemon's idle reset (#292) ---------------------------------------
// Clearing a crashed app (stop-current-app) or waking the robot while the
// daemon puts it to sleep would stop the sleep move part-way: the head can
// drop where it stopped.

#[tokio::test]
async fn app_start_after_a_crash_waits_for_the_idle_reset_before_clearing_it() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "hello_world"}, "state": "error", "error": "boom"}));
        f.idle_reset_polls = 3;
    })
    .await;
    let (status, body) = h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "loading");
    assert_eq!(
        h.calls(),
        vec!["idle-reset done", "stop-app", "set_mode enabled", "wake_up", "start-app reachy_mini_radio"]
    );
}

#[tokio::test]
async fn app_start_after_a_clean_exit_waits_before_waking_the_robot() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "hello_world"}, "state": "done"}));
        f.idle_reset_polls = 2;
    })
    .await;
    h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(h.calls(), vec!["idle-reset done", "set_mode enabled", "wake_up", "start-app reachy_mini_radio"]);
}

#[tokio::test]
async fn app_start_without_an_idle_reset_does_not_wait() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    let started = std::time::Instant::now();
    h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(h.calls(), vec!["start-app reachy_mini_radio"]);
    assert_eq!(h.fake.lock().unwrap().idle_reset_reads, 1);
    assert!(started.elapsed() < Duration::from_millis(500), "{:?}", started.elapsed());
}

#[tokio::test]
async fn app_start_and_stop_are_refused_while_the_idle_reset_never_ends() {
    let mut h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "hello_world"}, "state": "error", "error": "boom"}));
        f.idle_reset_polls = u32::MAX;
    })
    .await;
    Arc::get_mut(&mut h.state).unwrap().idle_reset_timeout = Duration::from_millis(300);
    let (status, body) = h.call("POST", "/api/apps/start/reachy_mini_radio", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "refused");
    assert_eq!(body["error"], "Robot is going to sleep; try again shortly");
    let (_, body) = h.call("POST", "/api/apps/stop/hello_world", None).await;
    assert_eq!(body["status"], "refused");
    // Nothing that stops or starts a move reached the daemon.
    assert!(h.calls().is_empty(), "{:?}", h.calls());
}

#[tokio::test]
async fn app_stop_of_a_crashed_app_waits_for_the_idle_reset() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "hello_world"}, "state": "error", "error": "boom"}));
        f.idle_reset_polls = 3;
    })
    .await;
    let (_, body) = h.call("POST", "/api/apps/stop/hello_world", None).await;
    assert_eq!(body["status"], "stopping");
    assert_eq!(h.calls(), vec!["idle-reset done", "stop-app"]);
}

#[tokio::test]
async fn app_stop_takes_the_power_lock() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
        f.app = Some(json!({"info": {"name": "hello_world"}, "state": "running"}));
    })
    .await;
    let power = h.state.power_lock.lock().await;
    let state = h.state.clone();
    let stop = tokio::spawn(async move {
        reachy_controller::app(state).oneshot(request("POST", "/api/apps/stop/hello_world", None)).await.unwrap()
    });
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(h.calls().is_empty(), "stopped under someone else's power action: {:?}", h.calls());
    assert_eq!(h.fake.lock().unwrap().idle_reset_reads, 0);
    drop(power);
    assert_eq!(stop.await.unwrap().status(), StatusCode::OK);
    assert_eq!(h.calls(), vec!["stop-app"]);
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
    // From a Space: its id for a link, and the card's description as the blurb.
    assert_eq!((&body[0]["space"], &body[0]["blurb"]), (&Value::Null, &json!("Radio")));
    assert_eq!((&body[1]["space"], &body[1]["blurb"]), (&json!("someone/hello_space"), &json!("Says hi")));
    assert_eq!(body[1]["author"], "someone");
    assert_eq!(body[1]["display"], "Hello Space");
}

#[tokio::test]
async fn favorites_default_validate_and_persist() {
    let h = harness(|_| {}).await;
    let (_, body) = h.call("GET", "/api/apps/favorites", None).await;
    assert_eq!(body["favorites"], json!(["reachy_mini_radio", "hello_world"]));
    let (status, _) = h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["nope"]}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let (status, _) =
        h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["hello_world", "hello_world"]}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let (status, _) = h.call("PUT", "/api/apps/favorites", Some(json!({"favorites": ["hello_world"]}))).await;
    assert_eq!(status, StatusCode::OK);
    let (_, body) = h.call("GET", "/api/apps/favorites", None).await;
    assert_eq!(body["favorites"], json!(["hello_world"]));
}
