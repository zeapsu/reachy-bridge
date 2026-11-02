import numpy as np

from reachy_bridge_runtime.features.wobble import Choice, Gate, Limits, SpeakerSwitch, heard_from_speaker

MOVE = (0.001, 0.0, 0.0, 0.0, 0.0, 0.0)
ZERO = (0.0,) * 6


def make():
    now = [100.0]
    gate = Gate(clock=lambda: now[0])
    sent = []

    def backend(offsets):
        gate.saw_offsets()  # as the wrapped Backend.set_speech_offsets does
        sent.append(offsets)

    return gate, gate.wrap(backend), backend, sent, now


def test_tap_offsets_pass_and_do_not_look_like_an_app():
    _, tap, _, sent, _ = make()
    tap(MOVE)
    tap(MOVE)
    assert sent == [MOVE, MOVE]


def test_silence_writes_one_zero():
    _, tap, _, sent, _ = make()
    tap(MOVE)
    tap(ZERO)
    tap(ZERO)
    tap(MOVE)
    assert sent == [MOVE, ZERO, MOVE]


def test_tap_holds_off_while_an_app_sends_offsets_then_resumes():
    _, tap, app, sent, now = make()
    app(MOVE)
    tap(MOVE)
    assert sent == [MOVE]
    now[0] += Gate.APP_HOLD_S + 0.1
    tap(ZERO)
    assert sent == [MOVE, ZERO]


def test_a_zero_still_goes_out_after_an_app_moved_the_head():
    _, tap, app, sent, now = make()
    tap(ZERO)
    app(MOVE)
    now[0] += Gate.APP_HOLD_S + 0.1
    tap(ZERO)
    assert sent == [ZERO, MOVE, ZERO]


def test_muted_speaker_leaves_only_talk():
    stereo = np.array([0.5, 0.0, 0.5, 0.25], dtype=np.float32)
    assert heard_from_speaker(stereo, lambda: True).tolist() == [0.5, 0.75]
    assert heard_from_speaker(stereo, lambda: False).tolist() == [0.0, 0.25]


def test_silent_robot_does_not_read_the_switch():
    def fail() -> bool:
        raise AssertionError("read the switch")

    assert heard_from_speaker(np.array([0.0, 0.25], dtype=np.float32), fail).tolist() == [0.25]


def test_speaker_switch_is_read_at_most_a_few_times_a_second():
    now = [0.0]
    reads = []
    switch = SpeakerSwitch(read=lambda: reads.append(1) or False, clock=lambda: now[0])
    assert switch.on() is False and switch.on() is False
    assert len(reads) == 1
    now[0] += 0.3
    switch.on()
    assert len(reads) == 2


# Motor ticks for an angle of 0 are 2048, inside these limits.
MOTORS = [(1000.0, 3000.0)] * 6
OFFSETS = (0.01, 0.0, 0.0, 0.0, 0.0, 0.1)


def limits(joints_for):
    def solve(_backend, offsets):
        return np.array([0.0] + [joints_for(offsets)] * 6)

    return Limits(solve, MOTORS)


def test_safe_offsets_are_unchanged():
    assert limits(lambda o: 0.0 if o is None else 0.1).scale(None, OFFSETS) == OFFSETS


def test_zero_is_never_checked():
    def solve(_backend, _offsets):
        raise AssertionError("solved")

    assert Limits(solve, MOTORS).scale(None, (0.0,) * 6) == (0.0,) * 6


def test_offsets_shrink_until_the_joints_stay_close():
    scaled = limits(lambda o: 0.0 if o is None else o[5] * 4.0).scale(None, OFFSETS)
    assert scaled == tuple(0.5 * v for v in OFFSETS)


def test_unreachable_offsets_become_zero():
    assert limits(lambda o: 0.0 if o is None else float("nan")).scale(None, OFFSETS) == (0.0,) * 6


def test_offsets_past_a_motor_limit_become_zero():
    assert limits(lambda o: 0.0 if o is None else -1.55).scale(None, OFFSETS) == (0.0,) * 6


def test_unreachable_target_or_failed_check_gives_zero():
    assert limits(lambda o: float("nan")).scale(None, OFFSETS) == (0.0,) * 6

    def broken(_backend, _offsets):
        raise RuntimeError("no")

    assert Limits(broken, MOTORS).scale(None, OFFSETS) == (0.0,) * 6


def test_an_app_stopping_keeps_the_wobble_the_dashboard_turned_on():
    choice = Choice()
    choice.enabled()
    choice.from_app(choice.enabled)
    assert choice.from_app(choice.keep_on)
    assert not choice.keep_on()  # the dashboard can still turn it off


def test_an_app_turns_off_its_own_wobble():
    choice = Choice()
    choice.from_app(choice.enabled)
    assert not choice.from_app(choice.keep_on)


def test_the_dashboard_turning_it_off_ends_the_choice():
    choice = Choice()
    choice.enabled()
    assert not choice.keep_on()
    assert not choice.from_app(choice.keep_on)


def test_the_wobble_comes_back_after_a_sleep_stops_it():
    choice = Choice()
    choice.enabled()
    choice.sleep_started()
    choice.sleep_started()  # reset_to_sleep calls goto_sleep
    assert not choice.quiescing(choice.keep_on)  # stopped for the moves
    assert not choice.sleep_ended()
    assert choice.sleep_ended()
    assert choice.from_app(choice.keep_on)  # still the dashboard's choice


def test_a_sleep_doesnt_turn_on_a_wobble_nobody_asked_for():
    choice = Choice()
    choice.sleep_started()
    assert not choice.quiescing(choice.keep_on)
    assert not choice.sleep_ended()


def test_a_sleep_that_stopped_nothing_starts_nothing():
    choice = Choice()
    choice.enabled()
    choice.sleep_started()
    assert not choice.sleep_ended()


def test_the_dashboard_turning_it_off_during_a_sleep_keeps_it_off():
    choice = Choice()
    choice.enabled()
    choice.sleep_started()
    choice.quiescing(choice.keep_on)
    assert not choice.keep_on()
    assert not choice.sleep_ended()


def test_a_forgotten_choice_doesnt_keep_an_apps_wobble():
    choice = Choice()
    choice.enabled()
    choice.forget()
    assert not choice.from_app(choice.keep_on)
