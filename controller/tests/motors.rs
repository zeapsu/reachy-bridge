//! Motor modes: gravity compensation guards and the return home after a mode change. Against the scripted fake daemon in `common`.

mod common;

use common::*;

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
async fn a_motor_mode_change_waits_for_a_power_change() {
    let h = harness(|f| {
        f.state = "running".into();
        f.motors = Some("disabled".into());
    })
    .await;
    let power = h.state.power_lock.lock().await;
    let pending = h.call("POST", "/api/motors/set_mode/enabled", None);
    tokio::pin!(pending);
    assert!(tokio::time::timeout(Duration::from_millis(200), &mut pending).await.is_err(), "waits while power is held");
    assert!(!h.calls().iter().any(|c| c.starts_with("set_mode")));
    drop(power);
    let (status, _) = pending.await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn motors_are_not_enabled_while_powering_off_but_can_always_go_limp() {
    let h = harness(|f| {
        f.state = "stopping".into();
        f.motors = Some("enabled".into());
    })
    .await;
    let (status, _) = h.call("POST", "/api/motors/set_mode/enabled", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(!h.calls().iter().any(|c| c.starts_with("set_mode")));
    let _power = h.state.power_lock.lock().await;
    let (status, _) = h.call("POST", "/api/motors/set_mode/disabled", None).await;
    assert_eq!(status, StatusCode::OK);
}
