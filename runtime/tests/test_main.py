from reachy_bridge_runtime import platforms
from reachy_bridge_runtime.main import for_daemon_args


def _board() -> platforms.Platform:
    return platforms.Platform(
        name="jetson",
        tier="tested",
        host_setup=(lambda: None,),
        features=frozenset({"audio_levels", "camera"}),
    )


def test_the_robot_daemon_keeps_everything():
    board = _board()
    assert for_daemon_args(board, ["--headless", "--fastapi-port", "8000"]) == board


def test_a_sim_daemon_leaves_the_host_and_media_alone():
    board = for_daemon_args(_board(), ["--sim", "--headless", "--no-media"])
    assert board.host_setup == ()
    assert board.features == frozenset()
    assert board.name == "jetson"


def test_a_mockup_sim_daemon_leaves_the_host_alone():
    assert for_daemon_args(_board(), ["--mockup-sim"]).host_setup == ()
