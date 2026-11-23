//! Conversation on wake (#334): the switch, and the app start after a power-on. Against the scripted fake daemon in `common`.

mod common;

use std::sync::atomic::Ordering::SeqCst;

use common::*;
use reachy_controller::auth::AuthContext;
use reachy_controller::profiles::{Access, Profile};
use reachy_controller::routes::wake_app;

const URI: &str = "/api/system/wake-app";

/// The harness with an installed app as the wake app.
async fn wake_harness(init: impl FnOnce(&mut Fake)) -> Harness {
    let mut h = harness(init).await;
    Arc::get_mut(&mut h.state).unwrap().wake_app = "hello_world".into();
    h
}

async fn switch_on(h: &Harness) {
    let (status, body) = h.call("PUT", URI, Some(json!({"enabled": true}))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
}

/// The fake daemon's calls once `last` is among them (the app starts in the background).
async fn calls_until(h: &Harness, last: &str) -> Vec<String> {
    for _ in 0..200 {
        if h.calls().iter().any(|c| c == last) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    h.calls()
}

/// Long enough for a background start to have reached the daemon: it polls every 250 ms.
async fn settle() {
    tokio::time::sleep(Duration::from_millis(600)).await;
}

#[tokio::test]
async fn the_switch_is_off_by_default_and_is_saved() {
    let h = wake_harness(|_| {}).await;
    let (status, body) = h.call("GET", URI, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"app": "hello_world", "name": "Hello World", "installed": true, "enabled": false}));
    for bad in [json!({}), json!({"enabled": "yes"}), json!([true])] {
        assert_eq!(h.call("PUT", URI, Some(bad)).await.0, StatusCode::UNPROCESSABLE_ENTITY);
    }
    let (status, body) = h.call("PUT", URI, Some(json!({"enabled": true}))).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["enabled"], true);
    assert_eq!(h.call("GET", URI, None).await.1["enabled"], true);
    assert!(h._dir.path().join("wake-app.json").exists());
    h.call("PUT", URI, Some(json!({"enabled": false}))).await;
    assert_eq!(h.call("GET", URI, None).await.1["enabled"], false);
}

#[tokio::test]
async fn the_switch_needs_the_app_installed() {
    // The Conversation app isn't among the fake daemon's apps.
    let mut h = harness(|_| {}).await;
    Arc::get_mut(&mut h.state).unwrap().wake_app = "reachy_mini_conversation_app".into();
    let (_, body) = h.call("GET", URI, None).await;
    assert_eq!(body["app"], "reachy_mini_conversation_app");
    assert_eq!(body["installed"], false);
    let (status, body) = h.call("PUT", URI, Some(json!({"enabled": true}))).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(h.call("GET", URI, None).await.1["enabled"], false);
    assert_eq!(h.call("PUT", URI, Some(json!({"enabled": false}))).await.0, StatusCode::OK);
}

#[tokio::test]
async fn the_switch_turns_off_while_the_daemon_is_down() {
    let dir = TempDir(std::env::temp_dir().join(format!("rc-wake-{}-{}", std::process::id(), rand_suffix())));
    std::fs::create_dir_all(dir.path()).unwrap();
    std::fs::write(dir.path().join("wake-app.json"), r#"{"enabled": true}"#).unwrap();
    let state = AppState::new(
        Daemon::new("http://127.0.0.1:9", Duration::from_millis(200)),
        LogBroker::new(),
        dir.path().into(),
        Duration::from_millis(10),
    );
    let put = |enabled: bool| {
        let app = reachy_controller::app(state.clone());
        async move { app.oneshot(request("PUT", URI, Some(json!({"enabled": enabled})))).await.unwrap().status() }
    };
    // The switch still reads, so the dashboard can show it and turn it off.
    let response = reachy_controller::app(state.clone()).oneshot(request("GET", URI, None)).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value =
        serde_json::from_slice(&axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap()).unwrap();
    assert_eq!((body["installed"].clone(), body["enabled"].clone()), (Value::Null, json!(true)));
    assert_eq!(put(true).await, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(put(false).await, StatusCode::OK);
    assert_eq!(std::fs::read_to_string(dir.path().join("wake-app.json")).unwrap(), r#"{"enabled":false}"#);
}

#[tokio::test]
async fn an_app_that_went_missing_another_way_switches_it_off() {
    // On in the file, and not among the fake daemon's apps: removed by a
    // session the controller didn't see.
    let gone = || async {
        let mut h = harness(|_| {}).await;
        Arc::get_mut(&mut h.state).unwrap().wake_app = "reachy_mini_conversation_app".into();
        std::fs::write(h._dir.path().join("wake-app.json"), r#"{"enabled": true}"#).unwrap();
        h
    };
    let saved = |h: &Harness| std::fs::read_to_string(h._dir.path().join("wake-app.json")).unwrap();
    // Reading the switch finds out.
    let h = gone().await;
    assert_eq!(h.call("GET", URI, None).await.1["enabled"], false);
    assert_eq!(saved(&h), r#"{"enabled":false}"#);
    // So does a power-on, which starts nothing.
    let h = gone().await;
    h.call("POST", "/api/daemon/start", None).await;
    settle().await;
    assert_eq!(h.calls(), vec!["start wake=true"]);
    assert_eq!(saved(&h), r#"{"enabled":false}"#);
}

#[tokio::test]
async fn removing_the_app_switches_it_off() {
    let h = wake_harness(|_| {}).await;
    switch_on(&h).await;
    // Another app's removal leaves it on.
    h.call("POST", "/api/apps/remove/reachy_mini_radio", None).await;
    assert_eq!(h.call("GET", URI, None).await.1["enabled"], true);
    let (status, body) = h.call("POST", "/api/apps/remove/hello_world", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(h.call("GET", URI, None).await.1["enabled"], false);
}

#[tokio::test]
async fn power_on_starts_the_app_after_the_wake() {
    let h = wake_harness(|_| {}).await;
    switch_on(&h).await;
    h.call("POST", "/api/daemon/start", None).await;
    assert_eq!(calls_until(&h, "start-app hello_world").await, vec!["start wake=true", "start-app hello_world"]);

    // Asleep: woken in place, then the app.
    let h = wake_harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    switch_on(&h).await;
    h.call("POST", "/api/daemon/start", None).await;
    assert_eq!(
        calls_until(&h, "start-app hello_world").await,
        vec!["set_mode enabled", "wake_up", "start-app hello_world"]
    );
}

#[tokio::test]
async fn no_app_starts_with_the_switch_off_or_a_robot_already_on() {
    let h = wake_harness(|_| {}).await;
    h.call("POST", "/api/daemon/start", None).await;
    settle().await;
    assert_eq!(h.calls(), vec!["start wake=true"]);

    let h = wake_harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    switch_on(&h).await;
    h.call("POST", "/api/daemon/start", None).await;
    settle().await;
    assert!(h.calls().is_empty(), "{:?}", h.calls());
}

#[tokio::test]
async fn a_robot_powered_off_before_the_app_starts_stays_off() {
    let h = wake_harness(|_| {}).await;
    switch_on(&h).await;
    // `join!` polls the power-on first and the power lock is handed over in
    // order (tokio's mutex is fair), so the off takes the lock before the
    // background start, which then finds the robot off.
    let (_, _) = tokio::join!(h.call("POST", "/api/daemon/start", None), h.call("POST", "/api/daemon/stop", None));
    settle().await;
    let calls = h.calls();
    assert!(!calls.iter().any(|c| c.starts_with("start-app")), "{calls:?}");
    assert_eq!(h.fake.lock().unwrap().state, "stopped");
}

#[tokio::test]
async fn a_robot_made_limp_during_the_wake_gets_no_app() {
    // Asleep, and the wake move is still playing when someone lets the motors go.
    let h = wake_harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
        f.running_moves = vec!["wake".into()];
    })
    .await;
    switch_on(&h).await;
    h.call("POST", "/api/daemon/start", None).await;
    let (status, body) = h.call("POST", "/api/motors/set_mode/disabled", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    // Even with the motors on again later, that power-on's app is gone.
    {
        let mut f = h.fake.lock().unwrap();
        f.motors = Some("enabled".into());
        f.running_moves.clear();
    }
    settle().await;
    assert_eq!(h.calls(), vec!["set_mode enabled", "wake_up", "set_mode disabled"]);
}

#[tokio::test]
async fn the_app_waits_for_the_wake_move_without_holding_up_a_power_off() {
    let h = wake_harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
        f.running_moves = vec!["wake".into()];
    })
    .await;
    switch_on(&h).await;
    h.call("POST", "/api/daemon/start", None).await;
    settle().await;
    assert_eq!(h.calls(), vec!["set_mode enabled", "wake_up"], "no app while the wake move plays");
    // The power lock is free meanwhile: the off doesn't wait out the wake
    // (20 s, held under the lock before).
    let off = tokio::time::timeout(Duration::from_secs(10), h.call("POST", "/api/daemon/stop", None)).await;
    assert_eq!(off.expect("power off waited for the wake").0, StatusCode::OK);
    h.fake.lock().unwrap().running_moves.clear();
    settle().await;
    let calls = h.calls();
    assert!(!calls.iter().any(|c| c.starts_with("start-app")), "{calls:?}");
}

#[tokio::test]
async fn an_app_already_running_keeps_the_robot() {
    // The fake daemon's start makes the robot `running` at once, so the app
    // already in the slot is what the background start finds.
    let h = wake_harness(|f| f.app = Some(json!({"info": {"name": "reachy_mini_radio"}, "state": "running"}))).await;
    switch_on(&h).await;
    h.call("POST", "/api/daemon/start", None).await;
    settle().await;
    assert_eq!(h.calls(), vec!["start wake=true"]);
}

#[tokio::test]
async fn a_profile_without_the_app_does_not_get_it_by_powering_on() {
    let h = wake_harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    switch_on(&h).await;
    let ctx = |access: Access| AuthContext {
        peer: None,
        authenticated: true,
        device: None,
        person: None,
        role: None,
        access,
        shared: false,
        local: false,
        credential: None,
        https: false,
    };
    let profile = |apps: &[&str]| -> Access {
        let profile: Profile = serde_json::from_value(json!({"name": "Mia", "apps": apps})).unwrap();
        Access::Limited(Arc::new(profile))
    };
    let epoch = h.state.power_epoch.load(SeqCst);
    wake_app::after_power_on(&h.state, Some(&ctx(profile(&["reachy_mini_radio"]))), epoch);
    wake_app::after_power_on(&h.state, Some(&ctx(Access::Unpicked)), epoch);
    settle().await;
    assert!(h.calls().is_empty(), "{:?}", h.calls());
    wake_app::after_power_on(&h.state, Some(&ctx(profile(&["hello_world"]))), epoch);
    assert_eq!(calls_until(&h, "start-app hello_world").await, vec!["start-app hello_world"]);
}

#[tokio::test]
async fn a_start_left_over_from_an_earlier_power_on_is_dropped() {
    // The robot is on from a later power-on (someone else's, perhaps): the
    // start decided for the earlier one doesn't act on it.
    let h = wake_harness(|f| {
        f.state = "running".into();
        f.motors = Some("enabled".into());
    })
    .await;
    switch_on(&h).await;
    let earlier = h.state.power_epoch.load(SeqCst);
    h.state.power_epoch.fetch_add(1, SeqCst);
    wake_app::after_power_on(&h.state, None, earlier);
    settle().await;
    assert!(h.calls().is_empty(), "{:?}", h.calls());
}
