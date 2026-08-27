#!/usr/bin/env python3
"""The bridge should log telescope motion whoever commanded it.

    python test_motion.py
"""

import asyncio
import logging

import bridge


class FakeTelescope:
    def __init__(self):
        self.last_radec = (213.915, 19.182)
        self.status = "tracking"
        self.status_calls = 0

    async def get_motion_status(self):
        self.status_calls += 1
        return self.status


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _rig():
    tel = FakeTelescope()
    b = bridge.StellariumBridge(tel, port=10098)
    cap = Capture()
    bridge.log.addHandler(cap)
    bridge.log.setLevel(logging.INFO)
    return tel, b, cap


def test_first_poll_reports_the_full_picture():
    """On startup the first line should say where it is, not just what it is."""
    tel, b, cap = _rig()
    asyncio.run(b._note_motion())
    line = next((l for l in cap.lines if "tracking" in l), None)
    assert line, cap.lines
    assert "RA 213.9150" in line, line
    assert "deg up" in line, line


def test_a_stationary_status_change_is_logged():
    """Park and init do not move the mount. The log must still say so.

    On 1.54 the bridge skipped the status read whenever the position had not
    changed, because every read was an RPC round trip. On 2.0 it is a local
    read of state the module already pushed, so there is nothing to save --
    and skipping it meant a telescope could be parked in silence, which is
    what happened on 2026-08-27.
    """
    tel, b, cap = _rig()
    asyncio.run(b._note_motion())          # baseline: tracking
    cap.lines.clear()
    tel.status = "parked"                  # parked, without moving an inch
    asyncio.run(b._note_motion())
    assert any("parked" in line for line in cap.lines), cap.lines


def test_an_unchanged_status_is_not_repeated():
    """Reading every second is fine. Printing every second is not.

    This is what the old "do not pester the telescope" test was really
    protecting -- a readable log -- and that concern outlived the RPC cost
    that used to justify it.
    """
    tel, b, cap = _rig()
    asyncio.run(b._note_motion())          # baseline
    cap.lines.clear()
    for _ in range(5):
        asyncio.run(b._note_motion())
    assert not cap.lines, f"repeated an unchanged status: {cap.lines}"


def test_third_party_slew_is_logged():
    """Someone else moved the telescope; the log should say so."""
    tel, b, cap = _rig()
    asyncio.run(b._note_motion())          # baseline: tracking
    cap.lines.clear()

    tel.last_radec = (200.0, 30.0)         # it moved, and it is still moving
    tel.status = "slewing"
    asyncio.run(b._note_motion())
    assert any("slewing" in line for line in cap.lines), cap.lines

    cap.lines.clear()
    tel.last_radec = (37.954, 89.264)      # arrived
    tel.status = "tracking"
    asyncio.run(b._note_motion())
    settled = [line for line in cap.lines if "tracking" in line]
    assert settled, cap.lines
    assert "RA 37.9540" in settled[0], settled
    assert "deg up" in settled[0], "altitude annotation missing: " + settled[0]


def test_keeps_watching_while_slewing():
    """While it is moving we must keep asking, or we never see it stop."""
    tel, b, cap = _rig()
    asyncio.run(b._note_motion())
    tel.last_radec = (100.0, 40.0)
    tel.status = "slewing"
    asyncio.run(b._note_motion())
    before = tel.status_calls
    asyncio.run(b._note_motion())          # position unchanged, still slewing
    assert tel.status_calls == before + 1, "stopped watching mid-slew"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
