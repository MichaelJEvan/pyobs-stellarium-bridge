#!/usr/bin/env python3
"""Console behaviour around stopping the telescope.

The dangerous confusions are "I quit, so it stopped" (it did not) and "I
aborted, so it stopped" (it must have). These pin both down.

    python test_scope.py
"""

import asyncio

import scope


class FakeTelescope:
    def __init__(self, status="tracking"):
        self.status = status
        self.radec = (100.0, 20.0)
        self.calls = []          # every remote call, in order

    async def get_radec(self):
        return self.radec

    async def get_motion_status(self):
        return self.status

    async def move_radec(self, ra, dec):
        self.calls.append("move_radec")
        self.status = "slewing"
        await asyncio.sleep(30)          # long enough to be interrupted
        self.status = "tracking"

    # The real methods, not a catch-all _call. A fake that answers any name
    # will happily pass while the real class has no such method -- which is
    # exactly how scope.py's abort, park and init stayed broken through the
    # 2.0 port with these tests green.
    async def stop_motion(self):
        self.calls.append("stop_motion")
        self.status = "tracking"

    async def init(self):
        self.calls.append("init")

    async def park(self):
        self.calls.append("park")
        self.status = "parking"
        await asyncio.sleep(30)          # a park is a slew; long enough to stop
        self.status = "parked"


def _console(status="tracking"):
    tel = FakeTelescope(status)
    return tel, scope.Console(tel)


def _answers(*replies):
    """Feed scripted answers to the console's prompts."""
    queue = list(replies)
    async def fake_input(prompt=""):
        return queue.pop(0) if queue else ""
    scope.ainput = fake_input


async def _start_slew(console):
    console.slewing = asyncio.create_task(console._slew(100.0, 20.0))
    await asyncio.sleep(0.05)            # let it get going
    assert console._busy(), "slew did not start"


def test_abort_stops_our_own_slew():
    async def go():
        tel, console = _console()
        await _start_slew(console)
        await console.do_abort()
        return tel
    tel = asyncio.run(go())
    assert "stop_motion" in tel.calls, f"never stopped the mount: {tel.calls}"


def test_quitting_does_not_stop_the_telescope():
    """The message says the telescope was not touched -- it must be true."""
    async def go():
        tel, console = _console()
        await _start_slew(console)
        console.abandoning = True        # what main() does on the way out
        console.slewing.cancel()
        try:
            await console.slewing
        except asyncio.CancelledError:
            pass
        return tel
    tel = asyncio.run(go())
    assert "stop_motion" not in tel.calls, \
        f"quitting stopped the telescope after saying it would not: {tel.calls}"


def test_abort_with_nothing_moving():
    async def go():
        tel, console = _console(status="tracking")
        _answers()
        await console.do_abort()
        return tel
    tel = asyncio.run(go())
    assert "stop_motion" not in tel.calls, "stopped an idle telescope"


def test_foreign_motion_asks_first_and_no_means_no():
    """Someone else is slewing. Declining must leave it alone."""
    async def go():
        tel, console = _console(status="slewing")
        _answers("n")
        await console.do_abort()
        return tel
    tel = asyncio.run(go())
    assert "stop_motion" not in tel.calls, "stopped it despite being told no"


def test_foreign_motion_stops_when_confirmed():
    async def go():
        tel, console = _console(status="slewing")
        _answers("y")
        await console.do_abort()
        return tel
    tel = asyncio.run(go())
    assert "stop_motion" in tel.calls, f"confirmed but never stopped: {tel.calls}"


def test_stop_retries_when_a_slew_lands_after_it():
    """The bug this was written for: abort races the slew command.

    stop_motion arrives before the mount has started moving, so it does
    nothing; the slew then lands and it sets off. One stop is not enough.
    """
    class LateSlew(FakeTelescope):
        def __init__(self):
            super().__init__(status="slewing")
            self.stops = 0

        async def stop_motion(self):
            self.calls.append("stop_motion")
            self.stops += 1
            if self.stops >= 2:            # the first one misses
                self.status = "tracking"

    async def go():
        tel = LateSlew()
        console = scope.Console(tel)
        scope.STOP_SETTLE = 0.01
        await console._stop("testing")
        return tel
    tel = asyncio.run(go())
    assert tel.stops >= 2, f"gave up after {tel.stops} stop(s)"
    assert tel.status == "tracking", "never actually stopped"


def test_stop_admits_when_it_cannot_stop_it():
    """If it will not stop, say so loudly rather than claiming success."""
    class Runaway(FakeTelescope):
        def __init__(self):
            super().__init__(status="slewing")
        async def stop_motion(self):
            self.calls.append("stop_motion")   # and never actually stops

    async def go():
        tel = Runaway()
        console = scope.Console(tel)
        scope.STOP_SETTLE = 0.01
        await console._stop("testing")
        return tel
    tel = asyncio.run(go())
    assert tel.calls.count("stop_motion") == scope.STOP_ATTEMPTS, \
        f"only tried {tel.calls.count('stop_motion')} times"
    assert tel.status == "slewing"


def test_stop_accepts_any_non_slewing_state():
    """Real drivers stop into states the simulator never uses.

    positioned, parked, idle -- all mean "not going anywhere". Only slewing
    means it is still travelling.
    """
    for landed_in in ("idle", "positioned", "parked", "tracking", "unknown"):
        class Stops(FakeTelescope):
            def __init__(self):
                super().__init__(status="slewing")
            async def stop_motion(self):
                self.calls.append("stop_motion")
                self.status = landed_in

        async def go():
            tel = Stops()
            console = scope.Console(tel)
            scope.STOP_SETTLE = 0.01
            await console._stop("testing")
            return tel
        tel = asyncio.run(go())
        assert tel.calls.count("stop_motion") == 1, \
            f"{landed_in!r} was treated as still moving"


def test_aborting_is_not_stopped_yet():
    """pyobs 2.0 goes slewing -> aborting -> idle.

    Reporting success while it is still "aborting" would be claiming it had
    stopped before it had.
    """
    class TwoStage(FakeTelescope):
        def __init__(self):
            super().__init__(status="slewing")
            self.seen = []

        async def stop_motion(self):
            self.calls.append("stop_motion")
            # first stop starts the abort, it finishes a moment later
            self.status = "aborting" if self.status == "slewing" else "idle"

        async def get_motion_status(self):
            self.seen.append(self.status)
            if self.status == "aborting":       # settles on the next look
                self.status = "idle"
            return self.seen[-1]

    async def go():
        tel = TwoStage()
        console = scope.Console(tel)
        scope.STOP_SETTLE = 0.01
        await console._stop("testing")
        return tel
    tel = asyncio.run(go())
    assert "aborting" in tel.seen, "never saw the aborting state"
    assert tel.calls.count("stop_motion") >= 2, \
        "declared it stopped while it was still aborting"


def test_park_needs_confirming():
    async def go():
        tel, console = _console()
        _answers("n")
        await console.do_park()
        declined = list(tel.calls)
        _answers("y")
        await console.do_park()
        return declined, tel.calls
    declined, after = asyncio.run(go())
    assert "park" not in declined, "parked without being confirmed"
    assert "park" in after, f"confirmed but never parked: {after}"


def test_a_park_can_be_aborted() -> None:
    """A park is the longest move of the night and must be interruptible.

    Measured 2026-08-30: `park` was awaited straight from the command loop,
    which blocked the prompt. An `abort` typed while it ran sat in the input
    buffer until the park had finished, and was then answered with "nothing to
    abort -- the telescope is parked". The mount was uninterruptible for the
    whole swing across the sky and nothing said so.

    A slew was already backgrounded and abortable. Park now uses the same
    slot, so there is one idea -- the mount is moving under our command --
    rather than two that behave differently.
    """
    async def run():
        tel, con = _console()
        _answers("y")                             # yes, park it
        await con.do_park()
        await asyncio.sleep(0.05)                # let it get going
        assert con._busy(), "park did not register as the mount moving"
        await con.do_abort()
        return tel.calls

    calls = asyncio.run(run())
    assert "park" in calls, calls
    assert "stop_motion" in calls, f"abort never reached the telescope: {calls}"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
