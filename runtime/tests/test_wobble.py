from reachy_bridge_runtime.features.wobble import Gate

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
