//! Expression playback: one at a time, then a silent return to the home pose.
//!
//! The daemon doesn't serialize REST moves (daemon-contract.md, "Moves"), so
//! two plays would run interleaved target loops. The player allows one session
//! at a time. A watcher task owns every session transition: playing →
//! returning_home → cleared. `stop` only asks the daemon to stop the move; the
//! watcher sees it vanish and carries on.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::daemon::{Daemon, DaemonError};

const BUSY: &str = "Robot is busy with another move.";
const NOT_HOMED: &str =
    "Robot pose is not homed — set Motors to Enabled, or turn off gravity compensation, to return home.";

#[derive(Clone, Debug, PartialEq)]
pub enum Phase {
    Playing,
    /// A manual drive session (#152) holds the robot; see [`ExpressionPlayer::start_drive`].
    Driving,
    ReturningHome,
}

#[derive(Clone, Debug)]
struct Session {
    uuid: Option<String>,
    dataset: Option<String>,
    name: Option<String>,
    phase: Phase,
    /// The motor-mode epoch the session started in. Its return home clears
    /// `needs_home` only if the mode never changed since: a goto "completes"
    /// on schedule even without torque.
    epoch: u64,
}

#[derive(Debug, PartialEq)]
pub enum PlayError {
    Busy(&'static str),
    Unavailable,
    NotFound,
}

pub struct ExpressionPlayer {
    daemon: Daemon,
    poll: Duration,
    session: Mutex<Option<Session>>,
    /// Held across check-and-start so two plays can't both pass the checks.
    start_lock: tokio::sync::Mutex<()>,
    /// Set on any torque change (#67): the pose is unknown, so plays are
    /// refused until a return home verifiably completes.
    needs_home: AtomicBool,
    /// Bumped on every motor-mode change. A return home only clears
    /// `needs_home` if no mode change happened while it ran: daemon moves
    /// finish on schedule whether or not the motors had torque.
    mode_epoch: AtomicU64,
    /// Bumped by `halt`. A return home captured before a halt must not post
    /// (or keep) its goto: it would run under the sleep move.
    halts: AtomicU64,
}

impl ExpressionPlayer {
    pub fn new(daemon: Daemon, poll: Duration) -> Arc<Self> {
        Arc::new(Self {
            daemon,
            poll,
            session: Mutex::new(None),
            start_lock: tokio::sync::Mutex::new(()),
            needs_home: AtomicBool::new(false),
            mode_epoch: AtomicU64::new(0),
            halts: AtomicU64::new(0),
        })
    }

    pub fn active(&self) -> bool {
        self.session.lock().unwrap().is_some()
    }

    pub fn set_needs_home(&self, value: bool) {
        self.needs_home.store(value, Ordering::SeqCst);
    }

    pub fn needs_home(&self) -> bool {
        self.needs_home.load(Ordering::SeqCst)
    }

    /// Record a motor-mode change: the pose is no longer trusted.
    pub fn mode_changed(&self) {
        self.needs_home.store(true, Ordering::SeqCst);
        self.mode_epoch.fetch_add(1, Ordering::SeqCst);
    }

    pub async fn play(self: &Arc<Self>, dataset: &str, name: &str) -> Result<Value, PlayError> {
        let _guard = self.start_lock.lock().await;
        let epoch = self.mode_epoch.load(Ordering::SeqCst);
        if self.active() {
            return Err(PlayError::Busy(BUSY));
        }
        if self.needs_home() {
            return Err(PlayError::Busy(NOT_HOMED));
        }
        match self.daemon.running_moves().await {
            None => return Err(PlayError::Unavailable),
            Some(moves) if !moves.is_empty() => return Err(PlayError::Busy(BUSY)),
            Some(_) => {}
        }
        let path = format!("move/play/recorded-move-dataset/{dataset}/{name}");
        let result = match self.daemon.post(&path, &[]).await {
            Ok(result) => result,
            Err(DaemonError::Refused { status: 404, .. }) => return Err(PlayError::NotFound),
            Err(DaemonError::Refused { status, detail }) => {
                tracing::warn!(status, ?detail, "daemon refused expression {dataset}/{name}");
                return Err(PlayError::Unavailable);
            }
            Err(DaemonError::Unavailable) => return Err(PlayError::Unavailable),
        };
        // A 200 without a uuid would leave the robot moving untracked.
        let Some(uuid) = result.get("uuid").and_then(Value::as_str).map(str::to_string) else {
            return Err(PlayError::Unavailable);
        };
        *self.session.lock().unwrap() = Some(Session {
            uuid: Some(uuid.clone()),
            dataset: Some(dataset.to_string()),
            name: Some(name.to_string()),
            phase: Phase::Playing,
            epoch,
        });
        let player = Arc::clone(self);
        let watched = uuid.clone();
        let halts = self.halts.load(Ordering::SeqCst);
        tokio::spawn(async move { player.watch(watched, epoch, halts).await });
        Ok(json!({"status": "playing", "uuid": uuid, "dataset": dataset, "move": name}))
    }

    /// Standalone return home (after the motors are re-enabled, #67). It uses
    /// the session machinery, so plays and app starts are blocked meanwhile.
    pub async fn return_home(self: &Arc<Self>) -> Value {
        let _guard = self.start_lock.lock().await;
        let epoch = self.mode_epoch.load(Ordering::SeqCst);
        {
            let mut session = self.session.lock().unwrap();
            if session.is_some() {
                return json!({"status": "busy", "detail": BUSY});
            }
            *session = Some(Session { uuid: None, dataset: None, name: None, phase: Phase::ReturningHome, epoch });
        }
        let player = Arc::clone(self);
        let halts = self.halts.load(Ordering::SeqCst);
        tokio::spawn(async move { player.goto_home_and_clear(epoch, halts).await });
        json!({"status": "returning_home"})
    }

    /// Claim the robot for a manual drive session (#152), under the same
    /// checks as a play. Returns the motor-mode epoch the session started in:
    /// [`Self::driving`] turns false once the mode changes.
    pub async fn start_drive(&self) -> Result<u64, PlayError> {
        let _guard = self.start_lock.lock().await;
        let mut session = self.session.lock().unwrap();
        if session.is_some() {
            return Err(PlayError::Busy(BUSY));
        }
        if self.needs_home() {
            return Err(PlayError::Busy(NOT_HOMED));
        }
        let epoch = self.mode_epoch.load(Ordering::SeqCst);
        *session = Some(Session { uuid: None, dataset: None, name: None, phase: Phase::Driving, epoch });
        Ok(epoch)
    }

    /// True while the drive session started in `epoch` may keep moving the robot.
    pub fn driving(&self, epoch: u64) -> bool {
        self.mode_epoch.load(Ordering::SeqCst) == epoch
            && matches!(&*self.session.lock().unwrap(), Some(Session { phase: Phase::Driving, .. }))
    }

    /// Drop a drive session without a return home, so the motor-mode change
    /// or power action that called it can run its own (#152). The pose is
    /// left untrusted.
    pub fn abort_drive(&self) {
        let mut session = self.session.lock().unwrap();
        if matches!(&*session, Some(Session { phase: Phase::Driving, .. })) {
            *session = None;
            self.needs_home.store(true, Ordering::SeqCst);
        }
    }

    /// End the drive session started in `epoch`. With `home`, and no motor-mode
    /// change since it started, it ends with the silent return home. Without,
    /// it only clears the session: after a mode change the motors may have no
    /// torque, and while an app runs the app owns the robot's moves.
    pub fn end_drive(self: &Arc<Self>, epoch: u64, home: bool) {
        let runtime = tokio::runtime::Handle::try_current().ok();
        // Read with the session locked, so a halt can't slip in between.
        let halts;
        {
            let mut session = self.session.lock().unwrap();
            match session.as_mut() {
                Some(s) if s.phase == Phase::Driving && s.epoch == epoch => {
                    if !home || runtime.is_none() || self.mode_epoch.load(Ordering::SeqCst) != epoch {
                        // The head stays where it was driven: not home.
                        *session = None;
                        self.needs_home.store(true, Ordering::SeqCst);
                        return;
                    }
                    s.phase = Phase::ReturningHome;
                }
                _ => return,
            }
            halts = self.halts.load(Ordering::SeqCst);
        }
        let player = Arc::clone(self);
        if let Some(runtime) = runtime {
            runtime.spawn(async move { player.goto_home_and_clear(epoch, halts).await });
        }
    }

    pub async fn stop(&self) -> Value {
        let session = self.session.lock().unwrap().clone();
        match session {
            None => json!({"status": "idle"}),
            Some(Session { phase: Phase::Driving, .. }) => json!({"status": "driving"}),
            Some(Session { phase: Phase::ReturningHome, .. }) => json!({"status": "returning_home"}),
            Some(Session { uuid, .. }) => {
                if let Some(uuid) = uuid {
                    // Best effort: the daemon answers 500 for a move that already ended.
                    let _ = self.daemon.post_json("move/stop", &json!({"uuid": uuid})).await;
                }
                json!({"status": "stopping"})
            }
        }
    }

    /// Before power off: drop the session (so no return home follows), stop
    /// every running move and wait up to `timeout` for the daemon to report
    /// none. A move left running fights the sleep move: the robot stutters
    /// and the antennas end wherever the last target loop left them.
    pub async fn halt(&self, timeout: Duration) {
        {
            // Under start_lock: a play or return home being started finishes
            // registering its session first, so it's cleared and its move
            // stopped below, and the needs_home gate refuses any after.
            let _guard = self.start_lock.lock().await;
            self.halts.fetch_add(1, Ordering::SeqCst);
            self.clear();
            self.mode_changed();
        }
        let deadline = tokio::time::Instant::now() + timeout;
        let mut stopped = Vec::new();
        loop {
            // A failed poll isn't "nothing running": the daemon's loop can
            // stall for seconds while it builds a recorded move.
            let moves = self.daemon.running_moves().await;
            if moves.as_ref().is_some_and(Vec::is_empty) {
                return;
            }
            for uuid in moves.unwrap_or_default() {
                // Once per move; a stop that didn't get through (a timeout,
                // the daemon busy) is retried on the next poll.
                if !stopped.contains(&uuid) && self.daemon.post_json("move/stop", &json!({"uuid": uuid})).await.is_ok() {
                    stopped.push(uuid);
                }
            }
            if tokio::time::Instant::now() >= deadline {
                tracing::warn!("moves still running at power off");
                return;
            }
            tokio::time::sleep(self.poll.min(Duration::from_millis(100))).await;
        }
    }

    pub fn current(&self) -> Value {
        match &*self.session.lock().unwrap() {
            None => json!({"active": false}),
            Some(s) => json!({
                "active": true,
                "phase": match s.phase { Phase::Playing => "playing", Phase::Driving => "driving", Phase::ReturningHome => "returning_home" },
                "dataset": s.dataset,
                "move": s.name,
                "uuid": s.uuid,
            }),
        }
    }

    fn clear(&self) {
        *self.session.lock().unwrap() = None;
    }

    /// Wait until `uuid` leaves the daemon's running moves. Two failed polls
    /// in a row mean the daemon is gone (returns false); one is tolerated.
    async fn wait_until_gone(&self, uuid: &str) -> bool {
        let mut failures = 0;
        loop {
            tokio::time::sleep(self.poll).await;
            match self.daemon.running_moves().await {
                None => {
                    failures += 1;
                    if failures >= 2 {
                        return false;
                    }
                }
                Some(moves) => {
                    failures = 0;
                    if !moves.iter().any(|m| m == uuid) {
                        return true;
                    }
                }
            }
        }
    }

    async fn watch(self: Arc<Self>, uuid: String, epoch: u64, halts: u64) {
        if !self.wait_until_gone(&uuid).await {
            self.clear();
            return;
        }
        {
            let mut session = self.session.lock().unwrap();
            match session.as_mut() {
                Some(s) => s.phase = Phase::ReturningHome,
                None => return,
            }
        }
        self.goto_home_and_clear(epoch, halts).await;
    }

    /// Silent goto to the wake-up home pose (no head roll, no sound): head at
    /// identity, antennas at the ±10° park offset, body yaw 0, over 1.5 s.
    async fn goto_home_and_clear(&self, epoch: u64, halts: u64) {
        let halted = || self.halts.load(Ordering::SeqCst) != halts;
        let home = json!({
            "head_pose": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "antennas": [-0.1745, 0.1745],
            "body_yaw": 0.0,
            "duration": 1.5,
        });
        if halted() {
            return;
        }
        let mut result = self.daemon.post_json("move/goto", &home).await;
        if result.is_err() {
            // One transient failure must not cost the return home.
            tokio::time::sleep(self.poll).await;
            if halted() {
                return;
            }
            result = self.daemon.post_json("move/goto", &home).await;
        }
        let uuid = result.ok().and_then(|r| r.get("uuid").and_then(Value::as_str).map(str::to_string));
        let Some(uuid) = uuid else {
            self.clear();
            return;
        };
        if halted() {
            // `halt` ran while the goto was being posted and may not have seen it.
            let _ = self.daemon.post_json("move/stop", &json!({"uuid": uuid})).await;
            return;
        }
        if self.wait_until_gone(&uuid).await && self.mode_epoch.load(Ordering::SeqCst) == epoch {
            self.needs_home.store(false, Ordering::SeqCst);
        }
        self.clear();
    }
}
