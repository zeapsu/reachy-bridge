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
