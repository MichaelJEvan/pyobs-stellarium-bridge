#!/usr/bin/env python3

"""**********************************************************************

    Developer: Michael J. Evan

    ***** Interactive control console for the pyobs telescope *****

    python scope.py

    Stays connected and prompts for a command, so the telescope can be
    driven, watched and stopped from one terminal without reconnecting
    each time.

    ------------------------------------------------------------------------
    commands:

    t          slew to a target -- prompts for a name or coordinates,
               accepting everything slewto.py accepts
    h          home: Polaris in the northern hemisphere, Sigma Octantis
               in the southern, or whatever `home:` says in config.yaml
    w          where the telescope is pointing now, stamped in UTC
    abort      stop the mount -- typed in full, on purpose
    park       park the mount -- typed in full, on purpose
    init       wake a parked mount up again. A parked telescope IGNORES
               slews without complaining, so this is not optional
    q          quit the console. It does NOT stop the telescope, and asks
               first if the mount is moving

    Single letters do the harmless things. Anything that stops the mount
    has to be typed out, so a stray keystroke cannot do it.

    ------------------------------------------------------------------------
    during a slew:

    The prompt stays live while the telescope moves, so `abort` can be
    typed mid-slew. Nothing is printed while it slews -- nightwatch.py is the
    live view, and output landing mid-word would make `abort` hard to type.

    Ctrl-C quits. It does NOT stop the telescope -- if a slew is running it
    says so and leaves it running, because Ctrl-C means "quit this program"
    everywhere else and should not quietly command hardware. Type abort to
    stop the mount.

    ------------------------------------------------------------------------
    stellarium:

    After a slew arrives, Stellarium is asked to select the target and centre
    on it, exactly as slewto.py does. Needs the RemoteControl plugin enabled
    on port 8090; without it the slew still happens and a note says so.

    ------------------------------------------------------------------------
    account:

    Logs in as console@localhost, set by `scope:` in config.yaml. It must
    differ from every other program's: two logins on one JID kick each other
    off in a loop, which looks like the client hanging rather than an auth
    problem. Register it once on the machine running pyobs:

      ejabberdctl register console localhost <password>

    ------------------------------------------------------------------------
    what it cannot do:

    It only knows about motion it started itself, in this session. If
    something else is driving -- Stellarium, a script, a scheduler -- it
    will say so rather than pretend to know who.

    Stopping the mount does not stop a scheduler. If pyobs is running a
    programme, aborting a slew halts that movement; the scheduler carries
    on making decisions.

    ------------------------------------------------------------------------

    slewto.py does the same slewing as a one-shot command, and is better
    for scripts. This is for sitting in front of.

"""

import asyncio
import sys
from datetime import datetime, timezone

from bridge import PyobsTelescope, SCOPE_JID, SITE_LAT, altitude_of
from slewto import (MIN_ALTITUDE, SUN_EXCLUSION, parse_coords,
                    resolve, separation_arcsec, sun_distance, tell_stellarium)

NORTH_HOME = "Polaris"
SOUTH_HOME = "Sigma Octantis"
PROMPT = "> "
POLL = 1.0            # how often the slew checks whether it has finished
# Still on the move, one way or another. pyobs 2.0 reports "aborting" while
# a stop is in progress, so treating anything-but-slewing as stopped would
# call it done too early.
MOVING = ("slewing", "aborting", "parking")
ASLEEP = ("parked", "parking", "initializing")   # move_radec silently does
                                                 # nothing in these states
STOP_ATTEMPTS = 4     # a slew in flight can land after our stop
STOP_SETTLE = 1.5     # give the mount a moment to actually halt

def home_target() -> str:
    """The pole star for this hemisphere, unless config.yaml overrides it."""
    try:
        import bridge
        configured = getattr(bridge, "HOME_TARGET", None)
        if configured:
            return configured
    except Exception:
        pass
    return NORTH_HOME if SITE_LAT >= 0 else SOUTH_HOME

def describe(ra: float, dec: float) -> str:
    alt = altitude_of(ra, dec)
    where = f"RA {ra:8.4f}  Dec {dec:+8.4f}"
    return where if alt is None else f"{where}   alt {alt:5.1f} deg"

def check_target(ra: float, dec: float, label: str) -> bool:
    """Same guards as slewto: altitude, then distance from the Sun."""
    alt = altitude_of(ra, dec)
    print(f"  target {label}: {describe(ra, dec)}")
    if alt is not None and alt < MIN_ALTITUDE:
        print(f"  REFUSED: {alt:.1f} deg is below the {MIN_ALTITUDE:.0f} deg limit")
        return False
    try:
        sep = sun_distance(ra, dec)
    except Exception as err:
        print(f"  WARNING: could not check the Sun ({err})")
        return True
    if sep < SUN_EXCLUSION:
        print(f"  REFUSED: {sep:.1f} deg from the Sun (limit {SUN_EXCLUSION:.0f})")
        return False
    if sep < 30.0:
        print(f"  note: {sep:.1f} deg from the Sun")
    return True

class Console:
    def __init__(self, tel: PyobsTelescope):
        self.tel = tel
        self.slewing: asyncio.Task | None = None   # a slew *we* started
        self.target: tuple[float, float] | None = None
        self.abandoning = False   # quitting: let go of the slew, do not stop it

    # -- telescope actions --------------------------------------------------

    async def _slew(self, ra: float, dec: float, label: str = "") -> None:
        """Move, quietly. Cancelling this stops the mount.

        Deliberately silent while it runs: this is a prompt, and output
        arriving mid-word makes `abort` hard to type. nightwatch.py is the live
        view, in its own window, where nothing is competing for the cursor.
        """
        self.target = (ra, dec)
        print(f"  slewing{' to ' + label if label else ''}...")
        try:
            move = asyncio.create_task(self.tel.move_radec(ra, dec))
            while not move.done():
                await asyncio.sleep(POLL)
            await move
            landed = await self.tel.get_radec()
            off = separation_arcsec(*landed, ra, dec)
            if self.tel.position_is_fresh:
                print(f"  arrived: {describe(*landed)}   ({off:.1f} arcsec off)")
            else:
                # The telescope has not published a position recently, so the
                # only reading we have is whatever it last said -- which may
                # predate the slew entirely. Saying "arrived" here would put a
                # position in front of the operator that nothing has confirmed.
                print(f"  cannot confirm arrival -- no fresh position from the "
                      f"telescope. Last reported: {describe(*landed)}")
            # Move Stellarium's view too, the same way slewto does. Silent if
            # the RemoteControl plugin is not enabled.
            tell_stellarium(label or None, ra, dec)
            _redraw_prompt()
        except asyncio.CancelledError:
            # Cancelled by abort -> stop the mount. Cancelled by quitting ->
            # let go of it, because that is what we told the user we do.
            if not self.abandoning:
                await self._stop("slew cancelled")
            raise
        except Exception as err:
            print(f"  slew failed: {err}")
            _redraw_prompt()
        finally:
            self.target = None

    async def _stop(self, why: str) -> None:
        """Stop the mount, and do not say so until it is actually stopped.

        A slew command already in flight will land *after* our stop and set
        the mount going, so one stop_motion is not enough: issue it, watch,
        and issue it again if it is still moving. Saying "stopped" when it is
        not is worse than not offering abort at all.
        """
        print(f"  {why} -- stopping the telescope...")
        for attempt in range(1, STOP_ATTEMPTS + 1):
            try:
                await self.tel.stop_motion()
            except Exception as err:
                print(f"  stop_motion failed: {err}")
            await asyncio.sleep(STOP_SETTLE)
            try:
                status = await self.tel.get_motion_status()
            except Exception as err:
                print(f"  could not read the motion status: {err}")
                status = None
            # "Not slewing" is the whole test. Drivers disagree about what to
            # call a stopped mount -- idle, positioned, tracking -- but they
            # all agree on what moving looks like.
            if status is not None and status not in MOVING:
                print(f"  stopped ({status}) at "
                      f"{describe(*await self.tel.get_radec())}")
                return
            if attempt < STOP_ATTEMPTS:
                print(f"  still {status} -- stopping again "
                      f"({attempt}/{STOP_ATTEMPTS})")
        print("  IT IS STILL MOVING after %d attempts -- use the pyobs GUI or "
              "the mount itself" % STOP_ATTEMPTS)

    async def _init(self) -> None:
        print("  initialising...")
        try:
            await self.tel.init()
            print(f"  {await self.tel.get_motion_status()}")
        except Exception as err:
            print(f"  init failed: {err}")

    async def _park(self) -> None:
        """Park, and stay interruptible while it happens.

        A park is a slew -- often the longest of the night, right across the
        sky, and the one most likely to be heading somewhere you did not
        intend. It used to be awaited straight from the command loop, which
        blocked the prompt: an `abort` typed while it ran sat in the input
        buffer until the park finished, and was then answered with "nothing to
        abort -- the telescope is parked". Measured 2026-08-30. The mount was
        uninterruptible for the whole move and nothing said so.
        """
        print("  parking...")
        try:
            await self.tel.park()
            print(f"  parked at {describe(*await self.tel.get_radec())}")
            _redraw_prompt()
        except asyncio.CancelledError:
            if not self.abandoning:
                await self._stop("park cancelled")
            raise
        except Exception as err:
            print(f"  park failed: {err}")
            _redraw_prompt()

    async def _where(self) -> None:
        try:
            ra, dec = await self.tel.get_radec()
            status = await self.tel.get_motion_status()
        except Exception as err:
            print(f"  could not read the telescope: {err}")
            return
        mine = " (this session)" if self.slewing and not self.slewing.done() else ""
        # UTC, and dated: this line gets pasted into notes, where a bare
        # local clock time is ambiguous by the next morning.
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"  {stamp}   {describe(ra, dec)}   {status}{mine}")

    # -- command handling ---------------------------------------------------

    async def _moving(self) -> bool:
        """Is the mount moving, whoever started it?"""
        try:
            return (await self.tel.get_motion_status()) in MOVING
        except Exception:
            return False

    def _busy(self) -> bool:
        return self.slewing is not None and not self.slewing.done()

    async def _awake(self) -> bool:
        """False if the mount would silently ignore a slew.

        pyobs returns from move_radec without moving or complaining when the
        telescope is parked, parking or initializing, which otherwise looks
        like a slew that worked and went nowhere.
        """
        try:
            status = await self.tel.get_motion_status()
        except Exception as err:
            print(f"  could not read the telescope: {err}")
            return False
        if status in ASLEEP:
            print(f"  the telescope is {status} -- it will ignore a slew "
                  f"without saying so.")
            print("  type  init  to wake it up first")
            return False
        return True

    async def _target_from_user(self, text: str) -> tuple[float, float] | None:
        """Read a target the way slewto does: a name, or RA and Dec."""
        parts = text.split()
        try:
            if len(parts) >= 2:
                return parse_coords(parts[0], parts[1])
            return resolve(text)
        except SystemExit as err:          # resolve/parse exit on bad input
            print(f"  {err}")
            return None

    async def do_target(self) -> None:
        if self._busy():
            print("  the mount is already moving -- abort first")
            return
        if not await self._awake():        # ask before making them type a target
            return
        text = (await ainput("  target (name, or RA Dec): ")).strip()
        if not text:
            return
        got = await self._target_from_user(text)
        if got is None:
            return
        if not check_target(*got, text):
            return
        self.slewing = asyncio.create_task(self._slew(*got, label=text))

    async def do_home(self) -> None:
        if self._busy():
            print("  the mount is already moving -- abort first")
            return
        if not await self._awake():
            return
        name = home_target()
        try:
            ra, dec = resolve(name)
        except SystemExit as err:
            print(f"  {err}")
            return
        if not check_target(ra, dec, name):
            print(f"  set a reachable `home:` in config.yaml for your site")
            return
        self.slewing = asyncio.create_task(self._slew(ra, dec, label=name))

    async def do_abort(self) -> None:
        if self._busy():
            self.slewing.cancel()          # _slew stops the mount on the way out
            try:
                await self.slewing
            except asyncio.CancelledError:
                pass
            return
        # Not our motion. Say exactly that, rather than guessing whose it is.
        try:
            status = await self.tel.get_motion_status()
        except Exception as err:
            print(f"  could not read the telescope: {err}")
            return
        if status not in MOVING:
            print(f"  nothing to abort -- the telescope is {status}")
            return
        print(f"  the telescope is {status}, but I did not start it")
        print("  (Stellarium, a script or a scheduler may have -- and stopping")
        print("   the mount does NOT stop a scheduler)")
        if (await ainput("  stop it anyway? (y/N): ")).strip().lower() != "y":
            print("  left alone")
            return
        await self._stop("aborting")

    async def do_park(self) -> None:
        if self._busy():
            print("  the mount is already moving -- abort first")
            return
        if (await ainput("  park the telescope? (y/N): ")).strip().lower() != "y":
            print("  left alone")
            return
        # Same slot a slew uses, so `abort` cancels it and _busy() knows the
        # mount is under our command. One idea -- "we are moving it" -- rather
        # than two that behave differently.
        self.slewing = asyncio.create_task(self._park())

    # -- the loop -----------------------------------------------------------

    async def run(self) -> None:
        print(MENU)
        while True:
            try:
                line = (await ainput(PROMPT)).strip()
            except (EOFError, asyncio.CancelledError):
                return
            except KeyboardInterrupt:
                if self._busy():
                    print()
                    await self.do_abort()
                    continue
                return

            cmd = line.lower()
            if cmd in ("q", "quit", "exit"):
                # Leaving a slew running is fine, but not by accident.
                if self._busy() or await self._moving():
                    print("  the telescope is still moving. Quitting will "
                          "leave it moving.")
                    answer = (await ainput("  quit anyway? (y/N): ")).strip()
                    if answer.lower() != "y":
                        print("  staying -- type abort to stop it")
                        continue
                return
            elif cmd == "t":
                await self.do_target()
            elif cmd == "h":
                await self.do_home()
            elif cmd == "w":
                await self._where()
            elif cmd == "abort":
                await self.do_abort()
            elif cmd == "park":
                await self.do_park()
            elif cmd == "init":
                await self._init()
            elif cmd in ("?", "help"):
                print(MENU)
            elif cmd in ("a", "p"):
                print(f"  type it out: {'abort' if cmd == 'a' else 'park'}")
            elif cmd:
                print("  ?  --  t target, h home, w where, "
                      "abort, park, init, q quit")

MENU = """
  t      slew to a target        w      where it is pointing
  h      home (pole star)        ?      this list
  abort  stop the mount          park   park the mount
  init   wake a parked mount     q      quit
"""

def _redraw_prompt() -> None:
    """Put the prompt back after output that arrived by itself.

    A slew finishes long after the prompt was drawn, so its message lands
    underneath and leaves the user looking at a blank line.
    """
    sys.stdout.write(PROMPT)
    sys.stdout.flush()


async def ainput(prompt: str) -> str:
    """Read a line without blocking the event loop or swallowing Ctrl-C.

    input() in a worker thread cannot be interrupted -- Ctrl-C would sit
    unnoticed until Enter was pressed. Letting the loop watch stdin instead
    keeps the main thread free, so the signal arrives when it is sent.
    """
    loop = asyncio.get_running_loop()
    sys.stdout.write(prompt)
    sys.stdout.flush()

    line = loop.create_future()

    def _readable() -> None:
        text = sys.stdin.readline()
        loop.remove_reader(sys.stdin)
        if not line.done():
            line.set_result(text)

    try:
        loop.add_reader(sys.stdin, _readable)
    except (NotImplementedError, ValueError):     # not a selectable stdin
        return await loop.run_in_executor(None, input)

    try:
        text = await line
    finally:
        try:
            loop.remove_reader(sys.stdin)
        except Exception:
            pass
    if text == "":                                 # EOF: piped input ran out
        raise EOFError
    return text.rstrip("\n")

async def main() -> int:
    tel = PyobsTelescope(jid=SCOPE_JID)
    print(f"connecting as {SCOPE_JID}...")
    try:
        await tel.connect()
    except Exception as err:
        print(f"could not reach pyobs: {err}")
        return 1

    console = Console(tel)
    try:
        await console._where()
        await console.run()
    finally:
        # Ask the telescope, not our own bookkeeping: a failed abort ends our
        # task while leaving the mount moving, and "not touched" would be a
        # lie in exactly the case it matters.
        try:
            status = await tel.get_motion_status()
        except Exception:
            status = None
        # Two ways it can be moving: the mount says so, or we issued a slew
        # that has not finished -- which may not have reached it yet, so the
        # status can still read "tracking" for a moment.
        issued = console._busy()
        if issued:
            console.abandoning = True
            console.slewing.cancel()
        if status in MOVING:
            print(f"\nquitting -- the telescope is STILL {status.upper()}, "
                  f"it was not stopped.")
            print("  type abort instead if you want it to stop.")
        elif issued:
            # Our slew had not returned yet, but the mount already reads as
            # settled -- say what we know rather than picking one.
            print(f"\nquitting -- a slew was still running ({status or 'unknown'}); "
                  f"it was not stopped.")
        elif status is None:
            print("\nquitting -- could not reach the telescope to check "
                  "whether it is moving.")
        else:
            print(f"\nquitting -- the telescope was not touched ({status}).")
        await tel.close()
    return 0

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print()
