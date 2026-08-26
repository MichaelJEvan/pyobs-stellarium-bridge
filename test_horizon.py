#!/usr/bin/env python3
"""The bridge should say when the telescope is left pointing underground.

pyobs enforces its altitude limit only when it accepts a slew, so a tracking
mount will follow a setting target below the horizon and never mention it.

    python test_horizon.py
"""

import logging

import bridge

_real_altitude_of = bridge.altitude_of


class FakeTelescope:
    def __init__(self, pos=(213.915, 19.182)):
        self.last_radec = pos
        self.status = "tracking"

    async def get_motion_status(self):
        return self.status


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _rig(alt):
    """A bridge whose idea of altitude we control, so tests do not need a sky."""
    tel = FakeTelescope()
    b = bridge.StellariumBridge(tel, port=10097)
    b._last_status = "tracking"
    cap = Capture()
    bridge.log.addHandler(cap)
    bridge.log.setLevel(logging.INFO)
    bridge.altitude_of = lambda *_: alt
    return tel, b, cap


def _set_alt(alt):
    bridge.altitude_of = lambda *_: alt


def _tick(b):
    """One check, ignoring the throttle."""
    b._horizon_checked = 0.0
    b._note_horizon()


def teardown():
    bridge.altitude_of = _real_altitude_of


def test_setting_below_the_horizon_warns():
    tel, b, cap = _rig(20.0)
    _tick(b)                                   # above: nothing to say
    assert not cap.lines, cap.lines
    _set_alt(-24.3)
    _tick(b)
    line = next((l for l in cap.lines if "horizon" in l), None)
    assert line, cap.lines
    assert "24 deg below" in line, line
    assert "tracking" in line, "the warning should say what it thinks it is doing"


def test_warns_only_once_per_crossing():
    tel, b, cap = _rig(-24.3)
    _tick(b)
    assert len(cap.lines) == 1, cap.lines
    for _ in range(5):
        _tick(b)
    assert len(cap.lines) == 1, "warned again without a crossing: " + str(cap.lines)


def test_coming_back_up_is_reported():
    tel, b, cap = _rig(-5.0)
    _tick(b)
    cap.lines.clear()
    _set_alt(10.0)
    _tick(b)
    assert any("back above the horizon" in l for l in cap.lines), cap.lines


def test_starting_up_above_the_horizon_is_silent():
    """Nothing is wrong, so nothing should be said."""
    tel, b, cap = _rig(45.0)
    _tick(b)
    assert not cap.lines, cap.lines


def test_checks_are_throttled():
    """The sky moves 0.25 deg/min; an astropy transform a second is waste."""
    calls = []
    tel, b, cap = _rig(45.0)
    bridge.altitude_of = lambda *_: (calls.append(1), 45.0)[1]
    for _ in range(10):
        b._note_horizon()                      # throttle left in place
    assert len(calls) == 1, f"{len(calls)} altitude transforms in one burst"


def test_without_astropy_it_stays_quiet():
    tel, b, cap = _rig(None)
    _tick(b)
    assert not cap.lines, cap.lines

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        teardown()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
