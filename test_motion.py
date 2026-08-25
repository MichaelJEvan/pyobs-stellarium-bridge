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


def test_idle_mount_costs_no_extra_calls():
    """Nothing moving, nothing happening -- do not pester the telescope."""
    tel, b, cap = _rig()
    asyncio.run(b._note_motion())          # baseline
    before = tel.status_calls
    for _ in range(5):
        asyncio.run(b._note_motion())
    assert tel.status_calls == before, f"made {tel.status_calls - before} needless calls"


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
