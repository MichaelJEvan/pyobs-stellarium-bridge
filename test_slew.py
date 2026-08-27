#!/usr/bin/env python3
"""Slew-retry behaviour, driven by a fake proxy.

The sim slews in seconds, so an RPC timeout mid-slew never happens there.
A real mount takes longer, so these cases have to be tested with a stand-in.

    python test_slew.py
"""

import asyncio

import bridge
from bridge import PyobsTelescope


class FakeState:
    """Stands in for pyobs 2.0's MotionState."""

    def __init__(self, status):
        self.status = status


class FakeProxy:
    """Stands in for a pyobs 2.0 telescope proxy.

    Motion is published state now, so the mount is watched by reading
    get_state rather than by calling a get_motion_status RPC. move_radec is
    still a real remote call.
    """

    def __init__(self, move_raises=None, statuses=("tracking",)):
        self.move_raises = move_raises
        self.statuses = list(statuses)
        self.moves = []
        self.status_calls = 0

    async def move_radec(self, ra, dec):
        self.moves.append((ra, dec))
        if self.move_raises is not None:
            raise self.move_raises

    def get_state(self, interface, *, max_age=None):
        if interface is not bridge.IMotion:
            return None
        self.status_calls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return FakeState(status)

    async def wait_for_state(self, interface, timeout=10.0, *, max_age=None):
        return self.get_state(interface, max_age=max_age)


class FakeComm:
    """Yields the fake proxy from `async with comm.proxy(...)`, as 2.0 does."""

    def __init__(self, proxy):
        self._proxy = proxy

    def proxy(self, _name, _interface=None):
        proxy = self._proxy

        class _Ctx:
            async def __aenter__(self):
                return proxy

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _telescope(proxy):
    """A telescope whose every proxy lookup yields the fake.

    2.0 resolves a proxy per call instead of caching one, so the fake has to
    come back from the comm each time rather than be pinned once.
    """
    tel = PyobsTelescope()
    tel._comm = FakeComm(proxy)
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


class FakeRaDec:
    """Stands in for pyobs 2.0's RaDecState."""

    def __init__(self, ra, dec):
        self.ra, self.dec = ra, dec


class SlowProxy(FakeProxy):
    """A mount that never finishes, standing in for one whose module died."""

    async def move_radec(self, ra, dec):
        self.moves.append((ra, dec))
        await asyncio.sleep(3600)


class MovingProxy(SlowProxy):
    """A mount that is somewhere new each time its position is read."""

    def __init__(self):
        super().__init__()
        self.ra = 0.0

    def get_state(self, interface, *, max_age=None):
        if interface is bridge.IPointingRaDec:
            self.ra += 10.0
            return FakeRaDec(self.ra, 0.0)
        return super().get_state(interface, max_age=max_age)


def test_the_reported_position_is_from_during_the_slew():
    """"Last seen at" must mean last seen, not "before it started moving".

    A client blocked in move_radec reads nothing for the whole slew, so the
    newest position it can name is the pre-slew one. Seen 2026-08-27: contact
    was lost 82 seconds into a slew at 0.5 deg/s -- the mount had travelled
    about 40 degrees -- and scope.py reported it as last seen at Polaris,
    where it had started. On real hardware that is the number somebody would
    walk outside and act on.
    """
    async def run():
        proxy = MovingProxy()
        tel = _telescope(proxy)
        tel.last_radec = (999.0, 0.0)          # where it was before the slew
        slew = asyncio.create_task(tel.move_radec(10.0, 20.0))
        await asyncio.sleep(bridge.POSITION_REFRESH * 3)   # let it move
        await tel._module_changed(bridge.ModuleClosedEvent(), "telescope")
        try:
            await slew
        except bridge.TelescopeLost as err:
            return str(err)
        raise AssertionError("expected TelescopeLost")

    message = asyncio.run(run())
    assert "999" not in message, \
        f"reported the position from before the slew: {message}"


def test_losing_the_module_abandons_the_slew_at_once():
    """Do not wait out a timeout for an answer that is not coming.

    pyobs gives move_radec twenty minutes, which is what a professional mount
    might genuinely need. It is useless to somebody standing next to a moving
    telescope: if the module has gone, nothing can command that mount, and the
    operator needs to know now rather than after lunch. Measured 2026-08-27 --
    scope.py sat at "slewing to vega..." for ten minutes after the module was
    killed, while the bridge had reported it gone within seconds.
    """
    async def run():
        proxy = SlowProxy()
        tel = _telescope(proxy)
        tel.last_radec = (123.0, 45.0)
        slew = asyncio.create_task(tel.move_radec(10.0, 20.0))
        await asyncio.sleep(0.05)                      # let it get going
        assert not slew.done(), "should still be waiting on the mount"
        # The module goes, exactly as the comm would tell us.
        await tel._module_changed(bridge.ModuleClosedEvent(), "telescope")
        try:
            await asyncio.wait_for(slew, timeout=1.0)
        except asyncio.TimeoutError:
            raise AssertionError("still waiting a second after the module went")
        except bridge.TelescopeLost as err:
            return str(err)
        raise AssertionError("returned normally instead of reporting the loss")

    message = asyncio.run(run())
    assert "lost contact" in message, message
    assert "RA 123.0000" in message, \
        f"should say where it was when contact went: {message}"


def test_losing_the_module_is_not_reported_as_a_timeout():
    """A timeout and a dead module deserve different words.

    "Timed out" means it is probably still slewing. "Lost contact" means
    nothing can command the mount at all. Conflating them would tell an
    operator to keep waiting when they should be walking outside.
    """
    async def run():
        proxy = SlowProxy()
        tel = _telescope(proxy)
        slew = asyncio.create_task(tel.move_radec(1.0, 2.0))
        await asyncio.sleep(0.05)
        await tel._module_changed(bridge.ModuleClosedEvent(), "telescope")
        try:
            await slew
        except bridge.TelescopeLost:
            return "lost"
        except Exception as err:
            return type(err).__name__
        return "no error"

    assert asyncio.run(run()) == "lost", "a dead module must not look like a timeout"


def test_a_settle_watch_gives_up_when_the_module_goes():
    """wait_until_settled must not poll a module that is not there."""
    async def run():
        proxy = FakeProxy(statuses=["slewing"])
        tel = _telescope(proxy)
        tel._module_lost.set()
        return await tel.wait_until_settled(timeout=5.0)

    assert asyncio.run(run()) is False


if __name__ == "__main__":
    bridge.SETTLE_POLL = 0.01      # keep the suite quick
    bridge.RECONNECT_DELAY = 0.01
    bridge.POSITION_REFRESH = 0.01
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
