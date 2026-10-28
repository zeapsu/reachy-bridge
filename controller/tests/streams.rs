//! The robot state stream. Against the scripted fake daemon in `common`.

mod common;

use common::*;

#[tokio::test]
async fn state_stream_waits_for_the_robot_instead_of_retrying_the_socket() {
    let h = harness(|_| {}).await;
    let attempts = |h: &Harness| h.fake.lock().unwrap().calls.iter().filter(|c| *c == "state-ws").count();
    let (tx, _rx) = tokio::sync::watch::channel(None);
    let task = tokio::spawn(reachy_controller::streams::run_state(h.state.daemon.clone(), tx));
    tokio::time::sleep(Duration::from_millis(2500)).await;
    assert_eq!(attempts(&h), 0, "no socket attempts while the robot is off");
    h.fake.lock().unwrap().state = "running".into();
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert!(attempts(&h) >= 1, "connects once the daemon runs");
    task.abort();
}
