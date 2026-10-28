//! The kinematics engine switch and the power cycle it needs. Against the scripted fake daemon in `common`.

mod common;

use common::*;

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
