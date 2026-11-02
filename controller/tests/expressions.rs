//! Expressions: serialized plays, the return home, and the cached dataset lists. Against the scripted fake daemon in `common`.

mod common;

use common::*;

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
    tokio::time::timeout(Duration::from_secs(5), reachy_controller::datasets::prefetch(h.state.clone())).await.unwrap();
    assert_eq!(h.calls().len(), 2, "{:?}", h.calls());
}
