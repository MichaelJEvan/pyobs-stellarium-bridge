#!/usr/bin/env python3
"""Slew-retry behaviour, driven by a fake proxy.

The sim slews in seconds, so an RPC timeout mid-slew never happens there.
A real mount takes longer, so these cases have to be tested with a stand-in.

    python test_slew.py
"""

import asyncio

import bridge
from bridge import PyobsTelescope


class FakeProxy:
    """Stands in for a pyobs telescope proxy."""

    def __init__(self, move_raises=None, statuses=("tracking",)):
        self.move_raises = move_raises
        self.statuses = list(statuses)
        self.moves = []
        self.status_calls = 0

    async def move_radec(self, ra, dec):
        self.moves.append((ra, dec))
        if self.move_raises is not None:
            raise self.move_raises

    async def get_motion_status(self):
        self.status_calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def _telescope(proxy):
    """A telescope whose proxy lookup always yields the fake.

    _call drops its cached proxy on a RemoteError, so pinning it here keeps
    the fake alive across retries.
    """
    tel = PyobsTelescope()

    async def _always_the_fake():
        return proxy

    tel._get_proxy = _always_the_fake
    return tel


def test_timeout_does_not_resend_the_slew():
    """A slow mount must not be commanded twice."""
    proxy = FakeProxy(move_raises=asyncio.TimeoutError(),
                      statuses=["slewing", "slewing", "tracking"])
    asyncio.run(_telescope(proxy).move_radec(10.0, 20.0))
    assert proxy.moves == [(10.0, 20.0)], f"sent {len(proxy.moves)} commands, want 1"
    assert proxy.status_calls >= 2, "should have watched the motion status"


def test_timeout_waits_for_the_mount_to_settle():
    proxy = FakeProxy(move_raises=asyncio.TimeoutError(),
                      statuses=["slewing"] * 4 + ["idle"])
    asyncio.run(_telescope(proxy).move_radec(1.0, 2.0))
    assert proxy.status_calls == 5, f"stopped watching after {proxy.status_calls} polls"


def test_gives_up_if_the_mount_never_settles():
    proxy = FakeProxy(move_raises=asyncio.TimeoutError(), statuses=["slewing"])
    tel = _telescope(proxy)
    assert asyncio.run(tel.wait_until_settled(timeout=0.05)) is False


def test_other_errors_still_retry():
    """Only timeouts are exempt -- a genuine failure should be retried."""
    proxy = FakeProxy(move_raises=bridge.RemoteError("boom"))
    try:
        asyncio.run(_telescope(proxy).move_radec(3.0, 4.0))
    except Exception:
        pass
    assert len(proxy.moves) == bridge.RETRIES, \
        f"retried {len(proxy.moves)} times, want {bridge.RETRIES}"


if __name__ == "__main__":
    bridge.SETTLE_POLL = 0.01      # keep the suite quick
    bridge.RECONNECT_DELAY = 0.01
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
