#!/usr/bin/env python3

"""*******************************************************************************

    Developer: Michael J. Evan
    August 2026
    Masters Computer Science
    University of Massachusetts Dartmouth - Dec 2026
    AAVSO member

    Bridge between Stellarium's Telescope Control plugin and a pyobs telescope.

    Stellarium connects as a TCP client; reports the telescope's position
    ~1 Hz and forward its "slew" commands to pyobs.

*******************************************************************************"""

import argparse
import asyncio
import contextlib
import logging
import pathlib
import struct
import sys
import time

# slixmpp logs a stringprep warning the moment it is imported, before anything
# has had a chance to configure logging. Silence it first or every tool that
# touches pyobs opens with it.
logging.getLogger("slixmpp.stringprep").setLevel(logging.ERROR)

# slixmpp narrates its own connection ("JID set to...", "Connected to
# server.") which duplicates what we log either side of it, in a different
# format. Warnings and errors from it still come through.
logging.getLogger("slixmpp").setLevel(logging.WARNING)
# pyobs's own client says "Connected to server." between our two lines saying
# the same thing. Its sibling xmppcomm is left alone -- that one reports
# disconnects and reconnects, which are worth seeing.
logging.getLogger("pyobs.comm.xmpp.xmppclient").setLevel(logging.WARNING)

try:
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    import astropy.units as u
except ImportError:      # altitude annotations are a nicety, not a requirement
    EarthLocation = None

try:
    from pyobs.comm.xmpp import XmppComm
    from pyobs.utils.exceptions import RemoteError, RemoteTimeoutError
    # pyobs 2.0: pointing and motion are published state, not RPC getters, and
    # both are addressed by their interface rather than by method name.
    from pyobs.interfaces import IMotion, IPointingRaDec
    # Emitted by the comm layer itself when a module goes offline or comes
    # back, so a client is told rather than having to notice.
    from pyobs.events import ModuleClosedEvent, ModuleOpenedEvent
except ImportError:  # the protocol layer below stays usable without pyobs
    XmppComm = None
    IMotion = IPointingRaDec = None
    ModuleOpenedEvent = ModuleClosedEvent = None
    class RemoteError(Exception): pass
    class RemoteTimeoutError(RemoteError): pass

log = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# Stellarium wire protocol
#
# Little-endian throughout. Angles travel as scaled integers:
#   RA  -- unsigned, 0x100000000 (2**32) spans a full turn (24h / 360 deg)
#   Dec -- signed,   0x40000000  (2**30) spans 90 deg
# ---------------------------------------------------------------------------

RA_SCALE = 2**32 / 360.0
DEC_SCALE = 2**30 / 90.0

MSG_CURRENT_POSITION = 0  # bridge -> Stellarium
MSG_GOTO = 0             # Stellarium -> bridge

_POSITION = struct.Struct("<HHQIii")  # len, type, time_us, ra, dec, status
_GOTO = struct.Struct("<HHQIi")       # len, type, time_us, ra, dec

POSITION_SIZE = _POSITION.size  # 24
GOTO_SIZE = _GOTO.size          # 20


def ra_to_raw(ra_deg: float) -> int:
    """Degrees (0-360, J2000) -> uint32. Wraps, so 360 and 0 agree."""
    return round(ra_deg * RA_SCALE) % 2**32


def raw_to_ra(raw: int) -> float:
    """uint32 -> degrees in [0, 360)."""
    return (raw % 2**32) / RA_SCALE


def dec_to_raw(dec_deg: float) -> int:
    """Degrees (-90..+90) -> int32. Clamped at the poles."""
    return round(max(-90.0, min(90.0, dec_deg)) * DEC_SCALE)


def raw_to_dec(raw: int) -> float:
    """int32 -> degrees in [-90, +90]."""
    return raw / DEC_SCALE


def pack_position(ra_deg: float, dec_deg: float, timestamp_us: int | None = None,
                  status: int = 0) -> bytes:
    """Build the 24-byte position report Stellarium draws its reticle from."""
    if timestamp_us is None:
        timestamp_us = int(time.time() * 1_000_000)
    return _POSITION.pack(POSITION_SIZE, MSG_CURRENT_POSITION, timestamp_us,
                          ra_to_raw(ra_deg), dec_to_raw(dec_deg), status)


def unpack_goto(data: bytes) -> tuple[float, float, int]:
    """Parse a 20-byte goto request -> (ra_deg, dec_deg, timestamp_us)."""
    if len(data) != GOTO_SIZE:
        raise ValueError(f"goto message is {len(data)} bytes, expected {GOTO_SIZE}")
    length, msg_type, timestamp_us, ra_raw, dec_raw = _GOTO.unpack(data)
    if msg_type != MSG_GOTO:
        raise ValueError(f"unexpected message type {msg_type}")
    return raw_to_ra(ra_raw), raw_to_dec(dec_raw), timestamp_us


# ---------------------------------------------------------------------------
# pyobs access
#
# Deliberately knows nothing about Stellarium -- the planned Alpaca server
# should be able to import this class as-is.
# ---------------------------------------------------------------------------

# Placeholders only -- the real values belong in config.yaml, which is not
# committed. Nothing here should identify a particular machine or site.
JID = "stellarium@localhost"
PASSWORD = "pyobs"                 # pyobs's own documented example
SERVER = "localhost:5222"
TELESCOPE = "telescope"
# Site of the telescope, used only to annotate logs with altitude.
SITE_LAT, SITE_LON, SITE_ELEV = 0.0, 0.0, 0.0
# Where we listen for Stellarium.
HOST = "127.0.0.1"
PORT = 10001
# slewto.py's own account. It must differ from JID above: two logins on one
# JID kick each other endlessly. Lives here so all settings share one file.
SLEWTO_JID = "scratch@localhost"
# scope.py needs its own again: running it alongside slewto.py on one JID
# makes the two kick each other off in a loop.
SCOPE_JID = "console@localhost"

# scope.py's `h` command. None means "pick the pole star for this hemisphere"
# from the site latitude, which is what almost everyone wants. Set `scope.home`
# in config.yaml to override it -- anything slewto.py accepts as a target.
HOME_TARGET = None

DEFAULT_CONFIG = pathlib.Path(__file__).with_name("config.yaml")
CONFIG_APPLIED: list[str] = []   # filled by load_config, reported once logging is up


def _config_from_argv() -> pathlib.Path:
    """Choose the config file, at import, before anything reads it.

    Two reasons it happens here rather than in __main__:

    The class defaults below (`jid: str = JID`, `module: str = TELESCOPE`)
    are bound when Python defines the class, which is further down this file.
    Loading a different config after that point changes the globals and
    nothing else -- the bridge would go on using the first config's telescope
    while the log claimed otherwise.

    And argv is only consulted when bridge.py is the program being run.
    slewto.py and scope.py import this module and parse their own arguments;
    reading argv unconditionally would swallow theirs.
    """
    if pathlib.Path(sys.argv[0]).name != "bridge.py":
        return DEFAULT_CONFIG
    parser = argparse.ArgumentParser(
        description="Report a pyobs telescope's position to Stellarium, and "
                    "forward Stellarium's slews back to pyobs.")
    parser.add_argument(
        "--config", metavar="PATH", type=pathlib.Path, default=DEFAULT_CONFIG,
        help="settings file (default: config.yaml beside this script). Give "
             "each telescope its own, with its own pyobs.module, pyobs.jid "
             "and listen.port, and run one bridge per telescope.")
    return parser.parse_args().config


CONFIG_FILE = _config_from_argv()

_CONFIG_KEYS = {
    "pyobs": {"server": "SERVER", "jid": "JID", "password": "PASSWORD",
              "module": "TELESCOPE"},
    "site": {"latitude": "SITE_LAT", "longitude": "SITE_LON",
             "elevation": "SITE_ELEV"},
    "listen": {"host": "HOST", "port": "PORT"},
    "slewto": {"jid": "SLEWTO_JID"},
    "scope": {"jid": "SCOPE_JID", "home": "HOME_TARGET"},
}


def load_config(path: pathlib.Path = CONFIG_FILE) -> None:
    """Override the defaults above from config.yaml, if it is there.

    Deliberately forgiving: a missing or unreadable file leaves the defaults
    in place and says so. Being unable to read a config file is not a reason
    to refuse to run. Unknown keys are warned about rather than ignored, so a
    typo tells you instead of quietly doing nothing.
    """
    if not path.exists():
        log.warning("config     no %s -- running on placeholder settings, which "
                    "will not reach a real observatory. Copy "
                    "config.example.yaml to %s.", path.name, path.name)
        return
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as err:
        log.warning("config     could not read %s (%s); using defaults",
                    path.name, err)
        return
    if not isinstance(data, dict):
        log.warning("config     %s is not a mapping; using defaults", path.name)
        return

    applied = []
    for section, values in data.items():
        known = _CONFIG_KEYS.get(section)
        if known is None:
            log.warning("config     ignoring unknown section %r", section)
            continue
        if not isinstance(values, dict):
            log.warning("config     section %r should be a mapping", section)
            continue
        for key, value in values.items():
            name = known.get(key)
            if name is None:
                log.warning("config     ignoring unknown key %s.%s", section, key)
                continue
            globals()[name] = value
            applied.append(f"{section}.{key}")
    CONFIG_APPLIED.extend(applied)

# Must run before anything below binds these as default arguments.
load_config(CONFIG_FILE)

RESOURCE = "pyobs"     # must match the modules: pyobs addresses peers as
                       # <module>@<domain>/<own resource>, so changing this
                       # makes every module invisible

RECONNECT_DELAY = 3.0  # a module that raises drops XMPP and returns in ~2 s
RETRIES = 3
MAX_BACKOFF = 30.0    # ceiling on the reconnect backoff
BAD_POLLS = 3         # consecutive failures before we rebuild the link
OPEN_TIMEOUT = 20.0   # comm.open() hangs rather than raising if pyobs is away
POSITION_REFRESH = 1.0  # while a slew is running, keep last_radec current at
                        # the rate the telescope publishes. Costs nothing --
                        # a read is a local cache lookup, measured under 10 ms
                        # -- and it is what makes "last seen at" mean the last
                        # position rather than the one before the slew began.
STATE_WAIT = 15.0     # how long to wait for a first value after subscribing.
                      # Deliberately no max_age on the read: a dummy telescope
                      # publishes only when a slew finishes or while tracking at
                      # a non-zero rate, so a settled mount stops publishing and
                      # any max_age would report it as having no position at all.
                      # Freshness is judged from the value's own timestamp
                      # instead -- see position_age.

class TelescopeLost(Exception):
    """Contact with the telescope module was lost while we were waiting on it.

    Distinct from a timeout on purpose. A timeout means "no answer yet, it is
    probably still working"; this means "the module is gone, no answer is
    coming, and nothing can command the mount until it returns". Those deserve
    different words in front of an operator.
    """


_RETRYABLE = (RemoteError, RemoteTimeoutError, asyncio.TimeoutError, TimeoutError)
_TIMEOUTS = (RemoteTimeoutError, asyncio.TimeoutError, TimeoutError)

SETTLED = {"idle", "tracking"}   # motion states that mean "not moving"
SLEW_TIMEOUT = 300.0             # how long to watch a slew that outlived its RPC
SETTLE_POLL = 2.0


class PyobsTelescope:
    """Thin async client for a pyobs telescope module, with patient retries."""

    def __init__(self, jid: str = JID, password: str = PASSWORD,
                 server: str = SERVER, module: str = TELESCOPE,
                 resource: str = RESOURCE):
        # Two logins sharing a JID kick each other in an endless loop, and
        # the resource cannot be varied to dodge that (see RESOURCE), so a
        # second instance needs its own registered account.
        self._jid, self._password, self._server = jid, password, server
        self._module, self._resource = module, resource
        self._comm = None
        self._proxy = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._proxy_stale = False        # set by the module-event handler below
        self._module_lost = asyncio.Event()   # set the moment the module goes
        self.connected = False
        self.position_is_fresh = False   # was the last position actually published
                                         # recently, or is it the last thing we heard?
        self.last_motion_status: str | None = None
        self.last_radec: tuple[float, float] | None = None
        self._last_update = 0.0

    async def _open(self) -> None:
        if XmppComm is None:
            raise RuntimeError("pyobs is not installed in this environment")
        log.info("pyobs      connecting to %s as %s", self._server, self._jid)
        self._comm = XmppComm(jid=self._jid, password=self._password,
                              server=self._server, resource=self._resource)
        try:
            await asyncio.wait_for(self._comm.open(), OPEN_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(Exception):
                await self._comm.close()
            self._comm = None
            raise
        await self._watch_module()
        self.connected = True
        log.info("pyobs      connected")

    async def _watch_module(self) -> None:
        """Ask pyobs to tell us when the telescope module comes or goes.

        Checking presence at the moment we resolve a proxy is a sample, not a
        watch, and a program that sits idle between keystrokes samples very
        rarely. On 2026-08-27 a container restart happened entirely between
        two of scope.py's checks: the module left and returned unseen, the
        subscription died with it, and the proxy went on replaying the first
        value the new module published -- it showed the sim's startup position
        for two hours while the bridge, which polls every second and so could
        not miss the outage, tracked correctly throughout.

        These events come from the comm layer itself, so nothing is missed
        however long we sit doing nothing.
        """
        if ModuleOpenedEvent is None or self._comm is None:
            return
        for event_class in (ModuleOpenedEvent, ModuleClosedEvent):
            try:
                await self._comm.register_event(event_class, self._module_changed)
            except Exception as err:
                # Not fatal: _get_proxy's presence check still covers the
                # common case. Say so rather than silently losing the watch.
                log.warning("pyobs      could not watch for %s (%s); falling "
                            "back to polling presence", event_class.__name__, err)

    async def _module_changed(self, event: object, sender: str) -> bool:
        """Mark the proxy stale. Do not tear it down here.

        This runs on the comm's own task while another coroutine may be
        mid-call on that proxy. Setting a flag lets _get_proxy do the swap at
        a point where nothing is using it.
        """
        if sender == self._module:
            going = isinstance(event, ModuleClosedEvent)
            log.info("pyobs      telescope module %s; the proxy is stale",
                     "went away" if going else "reappeared")
            self._proxy_stale = True
            # Anything blocked waiting on the module is waiting for an answer
            # that is no longer coming. Say so now rather than sitting quiet
            # until a timeout: pyobs gives move_radec twenty minutes, which is
            # useless to somebody standing next to a moving telescope.
            if going:
                self._module_lost.set()
            else:
                self._module_lost.clear()
        return True

    async def connect(self) -> None:
        """Keep trying until pyobs answers -- the VM may not be up yet."""
        delay = RECONNECT_DELAY
        while True:
            try:
                await self._open()
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning("pyobs      connect failed (%s); retrying in %.0f s",
                            err or type(err).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)

    async def reconnect(self) -> None:
        """Tear the link down and build a fresh one."""
        log.warning("pyobs      link looks dead; reconnecting")
        self.connected = False
        try:
            await self.close()
        except Exception:
            pass
        await self.connect()

    async def close(self) -> None:
        if self._comm is not None:
            await self._release_proxy()
            await self._comm.close()
            self._comm = None
            self.connected = False
            log.info("pyobs      link closed")

    @property
    def position_age(self) -> float:
        """Seconds since we last successfully read the position.

        Deliberately NOT the age of the published value. 2.0 state means "what
        is true right now", and a telescope that has stopped moving stops
        publishing -- a dummy one publishes only when a slew finishes or while
        tracking at a non-zero rate. Judging freshness by the value's own
        timestamp therefore declares a perfectly healthy settled mount stale
        after ten seconds and hangs up on Stellarium (seen 2026-08-26).

        What this guards against is pyobs being unreachable, and that shows up
        as a failed read or module_visible going false, not as an old value.
        """
        if self._last_update == 0.0:
            return float("inf")
        return time.monotonic() - self._last_update

    @property
    def module_visible(self) -> bool:
        """Is the telescope module actually announcing itself right now?"""
        try:
            return self._comm is not None and self._module in self._comm.clients
        except Exception:
            return False

    async def _get_proxy(self):
        """One proxy, held open for the life of the link.

        State arrives by subscription and the first value lands a second or two
        after resolving, so a proxy opened and closed per call never receives
        anything -- measured 2026-08-26 against the 2.0 sim: None on subscribe,
        populated three seconds later. AsyncExitStack is what the upgrade notes
        recommend for a proxy that has to outlive one `async with` block.
        """
        # Take the flag before doing anything that awaits. An event arriving
        # while we are resolving would otherwise be cleared below and lost --
        # and a module coming back is exactly when that event fires.
        stale, self._proxy_stale = self._proxy_stale, False
        if self._proxy is not None and (stale or not self.module_visible):
            # The module went away. Its subscription went with it, but the
            # proxy keeps handing back the last value it ever received -- so a
            # caller sees a frozen position and never learns it is frozen.
            # Drop it and resolve a fresh one. (Seen 2026-08-26: scope.py held
            # a proxy across a container restart and reported "arrived" at a
            # position 44 degrees from the target for the rest of the session.)
            await self._release_proxy()
        if self._proxy is None:
            self._stack = contextlib.AsyncExitStack()
            await self._stack.__aenter__()
            self._proxy = await self._stack.enter_async_context(
                self._comm.proxy(self._module))
        return self._proxy

    async def _release_proxy(self) -> None:
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._proxy, self._stack = None, None

    async def _with_proxy(self, interface, what: str, body, *,
                          retry_timeouts: bool = True):
        """Run `body(proxy)` against a freshly resolved proxy, with retries.

        In pyobs 2.0 a Proxy is a context manager -- `async with comm.proxy(...)`
        -- and holding one open is no longer the way to keep talking to a
        module, so there is nothing to cache. Resolving is cheap: state arrives
        on subscribe, so a proxy has the current value the moment it opens.

        retry_timeouts=False for calls where a timeout does not mean failure --
        re-sending them would be actively harmful.
        """
        for attempt in range(1, RETRIES + 1):
            try:
                return await body(await self._get_proxy())
            except _RETRYABLE as err:
                if not retry_timeouts and isinstance(err, _TIMEOUTS):
                    raise
                await self._release_proxy()   # re-resolve on the next attempt
                if attempt == RETRIES:
                    log.error("%s failed after %d attempts: %s", what, RETRIES, err)
                    raise
                log.warning("%s failed (%s), retry %d/%d in %.0f s",
                            what, type(err).__name__, attempt, RETRIES,
                            RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    @staticmethod
    async def _read_state(proxy, interface):
        """Latest published value for an interface, waiting only if we have none.

        get_state is a local read of the cached value -- no round trip. It
        returns None if nothing has arrived or the value is older than
        max_age, and only then is it worth waiting for the next publish.
        """
        # Deliberately no max_age. pyobs offers one so a dead publisher is not
        # trusted forever, and for something published on a timer -- weather,
        # say -- that is right. A telescope publishes only when it arrives
        # somewhere, so a mount that settled an hour ago has one true value that
        # happens to be an hour old, and any max_age discards it and reports a
        # perfectly healthy telescope as having no position at all.
        #
        # Liveness is judged by whether the module is there and our subscription
        # to it is live (see _get_proxy, which drops a proxy whose module has
        # gone), not by the age of the last thing it said.
        state = proxy.get_state(interface)
        if state is None:
            state = await proxy.wait_for_state(interface, timeout=STATE_WAIT)
        return state

    async def get_radec(self) -> tuple[float, float]:
        """Current pointing in degrees, J2000. Caches the result.

        2.0 removed the get_radec RPC: the telescope publishes RaDecState and
        we read the last value. Nothing is asked of the module, so a busy or
        sulking one cannot stall us.
        """
        async def read(proxy):
            return await self._read_state(proxy, IPointingRaDec)

        state = await self._with_proxy(IPointingRaDec, "get_radec", read)
        if state is not None:
            self.last_radec = (float(state.ra), float(state.dec))
            self._last_update = time.monotonic()
            self.position_is_fresh = True
            return self.last_radec

        # Nothing fresh. If the module is still there, the mount has simply not
        # moved -- keep the last position rather than blanking the reticle, but
        # say it is not fresh so callers that need certainty (has it arrived?)
        # can refuse to answer. If the module is gone, we genuinely do not know.
        if self.module_visible and self.last_radec is not None:
            self._last_update = time.monotonic()
            self.position_is_fresh = False
            return self.last_radec
        self.position_is_fresh = False
        raise RemoteTimeoutError("no RaDecState published within "
                                 f"{STATE_WAIT:.0f} s")

    async def wait_until_settled(self, timeout: float = SLEW_TIMEOUT) -> bool:
        """Poll motion status until the mount stops moving."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._module_lost.is_set():
                log.warning("pyobs      gave up watching the mount: the module "
                            "is gone")
                return False
            try:
                status = await self.get_motion_status()
            except Exception as err:
                log.warning("pyobs      could not read motion status: %s", err)
                status = None
            if status in SETTLED:
                log.info("pyobs      mount settled (%s)", status)
                return True
            await asyncio.sleep(SETTLE_POLL)
        log.warning("pyobs      mount still moving after %.0f s", timeout)
        return False

    async def move_radec(self, ra_deg: float, dec_deg: float) -> None:
        """Slew. Raises remotely if the target is below the altitude limit.

        move_radec blocks for the whole move, and pyobs times an RPC out at
        ~30 s. The sim slews in seconds so this never shows there, but a real
        mount can easily take longer -- and re-sending the command to a mount
        that is already moving is worse than waiting. So on a timeout, watch
        the motion status instead of retrying.
        """
        log.info("pyobs      slewing to RA %.4f Dec %.4f", ra_deg, dec_deg)

        async def go(proxy):
            return await proxy.move_radec(ra_deg, dec_deg)

        async def keep_position_current():
            """Read the published position while the slew runs.

            A client blocked in move_radec otherwise learns nothing for the
            whole slew, so if contact is lost the newest position it can name
            is the one from before it started moving. Seen 2026-08-27: the
            mount had travelled about 40 degrees, and scope.py reported it as
            last seen at Polaris. On real hardware that is the number an
            operator would act on.
            """
            while True:
                await asyncio.sleep(POSITION_REFRESH)
                with contextlib.suppress(Exception):
                    await self.get_radec()

        self._module_lost.clear()
        slew = asyncio.ensure_future(
            self._with_proxy(IPointingRaDec, "move_radec", go,
                             retry_timeouts=False))
        lost = asyncio.ensure_future(self._module_lost.wait())
        watch = asyncio.ensure_future(keep_position_current())
        try:
            done, _ = await asyncio.wait({slew, lost},
                                         return_when=asyncio.FIRST_COMPLETED)
            if lost in done:
                # The module vanished. Do not fall through to watching motion
                # status -- there is nothing left to ask.
                where = ("last seen at RA %.4f Dec %.4f" % self.last_radec
                         if self.last_radec else "position unknown")
                raise TelescopeLost(
                    f"lost contact with the telescope during the slew; {where}. "
                    "The mount will carry on doing whatever it was last told.")
            await slew          # completed or raised on its own
        except _TIMEOUTS:
            log.info("move_radec RPC timed out; the slew is probably still "
                     "running, watching motion status")
            await self.wait_until_settled()
        finally:
            for task in (slew, lost, watch):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task

    async def get_motion_status(self) -> str:
        """Lowercase status string, e.g. 'idle' / 'slewing'.

        2.0 publishes MotionState instead of answering get_motion_status. It
        carries an overall `status` plus a per-device list; the overall one is
        what the reticle and the slew watcher care about.
        """
        async def read(proxy):
            return await self._read_state(proxy, IMotion)

        state = await self._with_proxy(IMotion, "get_motion_status", read)
        if state is not None:
            self.last_motion_status = str(state.status).lower()
            return self.last_motion_status

        # Motion status is published on change, not on a timer, so a mount that
        # settled an hour ago has nothing recent to offer -- and that is not the
        # same as not knowing. While the module is there, its last announced
        # status still stands; only its disappearance means we have lost track.
        if self.module_visible and self.last_motion_status is not None:
            return self.last_motion_status
        raise RemoteTimeoutError("no MotionState published within "
                                 f"{STATE_WAIT:.0f} s")

    # -- IMotion commands ------------------------------------------------
    # These used to be reached through _call(), which the 2.0 port replaced
    # with _with_proxy. Nothing took over the three command names, so
    # scope.py's abort, park and init quietly stopped working -- they caught
    # the AttributeError and reported failure honestly, which is the only
    # reason it was not worse. They belong here rather than in scope.py:
    # PyobsTelescope is the part meant to be reused, and an Alpaca server
    # would want them too.
    #
    # Signatures checked against the installed pyobs 2.0.1:
    #   init(**kwargs), park(**kwargs), stop_motion(device=None, **kwargs)

    async def stop_motion(self) -> None:
        """Stop the mount. No device name stops everything the module drives.

        Retries on timeout, unlike move_radec: re-issuing a stop is harmless,
        and scope.py deliberately issues it more than once because a slew
        already in flight can land after the first one.
        """
        async def go(proxy):
            return await proxy.stop_motion()

        await self._with_proxy(IMotion, "stop_motion", go)

    async def init(self) -> None:
        """Wake a parked mount.

        Blocks while the mount initialises, so a timeout does not mean it
        failed -- same reasoning as move_radec. Do not re-send.
        """
        async def go(proxy):
            return await proxy.init()

        await self._with_proxy(IMotion, "init", go, retry_timeouts=False)

    async def park(self) -> None:
        """Park the mount. Blocks while it parks, so a timeout is not failure."""
        async def go(proxy):
            return await proxy.park()

        await self._with_proxy(IMotion, "park", go, retry_timeouts=False)

# ---------------------------------------------------------------------------
# Stellarium TCP server
#
# Stellarium is the client; we listen. One background poller keeps a fresh
# position so a slow or sulking XMPP call never stalls the reticle.
# ---------------------------------------------------------------------------

POLL_INTERVAL = 1.0
MODULE_WAIT = 5.0     # gentler cadence while the module is away
STALE_AFTER = 10.0    # past this, stop showing a position we cannot vouch for
MOVED_THRESHOLD = 0.01   # degrees of change that counts as the mount moving
HORIZON = 0.0            # degrees: below this the mount points into the ground
HORIZON_POLL = 30.0      # seconds between altitude checks; the sky moves 0.25 deg/min


def altitude_of(ra_deg: float, dec_deg: float) -> float | None:
    """Altitude of that position right now, or None without astropy."""
    if EarthLocation is None:
        return None
    site = EarthLocation(lat=SITE_LAT * u.deg, lon=SITE_LON * u.deg,
                         height=SITE_ELEV * u.m)
    frame = AltAz(obstime=Time.now(), location=site)
    return SkyCoord(ra_deg * u.deg, dec_deg * u.deg).transform_to(frame).alt.degree

def _position_note(pos: tuple[float, float]) -> str:
    alt = altitude_of(*pos)
    where = f"RA {pos[0]:.4f} Dec {pos[1]:+.4f}"
    return where if alt is None else f"{where}, {alt:.0f} deg up"


class StellariumBridge:
    def __init__(self, telescope: PyobsTelescope, host: str = HOST, port: int = PORT):
        self._tel = telescope
        self._host, self._port = host, port
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._pending: tuple[float, float] | None = None   # newest requested target
        self._in_flight: tuple[float, float] | None = None
        self._slewer: asyncio.Task | None = None
        self._stale_dropped = False
        self._last_pos: tuple[float, float] | None = None
        self._last_status: str | None = None
        self._below_horizon: bool | None = None   # None until the first check
        self._horizon_checked = 0.0

    def _spawn(self, coro) -> None:
        """Track every task so a client hangup can't leak one."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _poll_forever(self) -> None:
        """Refresh the cached position ~1 Hz.

        pyobs can vanish under us -- the VM reboots, the sim gets restarted --
        so a run of failures rebuilds the link rather than killing the bridge.
        Stellarium keeps its reticle on the last known position meanwhile.
        """
        await self._tel.connect()
        misses = 0
        waiting = False
        while True:
            try:
                await self._tel.get_radec()
                if waiting:
                    log.info("pyobs      telescope module is back")
                if self._stale_dropped:
                    log.info("client     position is fresh again; clients may reconnect")
                    self._stale_dropped = False
                misses, waiting = 0, False
                await self._note_motion()
                self._note_horizon()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._tel.module_visible:
                    # The link is fine, the module simply is not there --
                    # rebuilding the link would only thrash, so just wait.
                    if not waiting:
                        log.warning("pyobs      telescope module not present; waiting for it")
                        waiting = True
                    misses = 0
                    await asyncio.sleep(MODULE_WAIT)
                    continue
                misses += 1
                log.warning("pyobs      position poll failed (%d/%d): %s",
                            misses, BAD_POLLS, err)
                if misses >= BAD_POLLS:
                    misses = 0
                    try:
                        await self._tel.reconnect()
                    except asyncio.CancelledError:
                        raise
                    except Exception as err2:
                        log.error("pyobs      reconnect attempt failed: %s", err2)
            await asyncio.sleep(POLL_INTERVAL)

    async def _note_motion(self) -> None:
        """Log state changes even when someone else moved the telescope.

        This used to skip the status read whenever the mount had not moved,
        because on 1.54 every read was an RPC round trip and it was not worth
        one per second to watch a settled mount. That optimisation cost us
        park and init: neither moves the mount, so both happened in silence
        and the log never said the telescope was parked (seen 2026-08-27).

        On 2.0 the saving no longer exists -- get_motion_status is a local
        read of state the module already pushed us, not a round trip -- so we
        always ask. Status changes still only print when they change.
        """
        pos = self._tel.last_radec
        if pos is None:
            return
        moved = self._last_pos is None or max(
            abs(pos[0] - self._last_pos[0]), abs(pos[1] - self._last_pos[1])
        ) > MOVED_THRESHOLD
        self._last_pos = pos
        try:
            status = await self._tel.get_motion_status()
        except Exception:
            return
        if status != self._last_status:
            if status in SETTLED:
                # Includes the very first reading: on startup this line is the
                # whole picture of where things stand.
                log.info("pyobs      telescope %s -- %s", status, _position_note(pos))
            else:
                log.info("pyobs      telescope %s", status)   # mid-slew: position is moving
            self._last_status = status
        elif moved and status in SETTLED:
            log.info("pyobs      telescope moved -- %s", _position_note(pos))

    def _note_horizon(self) -> None:
        """Warn when the telescope is left pointing below the horizon.

        pyobs checks its altitude limit only at the moment it accepts a slew
        (BaseTelescope.move_radec); nothing re-checks it afterwards. A mount
        tracking a setting target therefore follows it straight down through
        the horizon and goes on reporting "tracking" from underground. We
        watch and say so -- this is a monitoring bridge, not an interlock --
        but the log should not stay silent about it all night.

        Edge-triggered: one line at each crossing, not one a second.
        """
        pos = self._tel.last_radec
        if pos is None:
            return
        now = time.monotonic()
        if now - self._horizon_checked < HORIZON_POLL:
            return
        self._horizon_checked = now
        alt = altitude_of(*pos)
        if alt is None:          # no astropy: nothing to measure against
            return
        below = alt < HORIZON
        if below == self._below_horizon:
            return
        first = self._below_horizon is None
        self._below_horizon = below
        if below:
            log.warning("horizon    telescope is %.0f deg below the horizon "
                        "(%s); pyobs will not stop it", -alt,
                        self._last_status or "motion status unknown")
        elif not first:
            log.info("horizon    telescope is back above the horizon "
                     "(%.0f deg up)", alt)

    @property
    def _stale(self) -> bool:
        """Is the cached position too good to show, or the telescope too lost?

        Two different ways of not knowing where a telescope is pointing.

        It can go quiet: the read fails or the module vanishes, and that shows
        up as age. Short blips ride through on the last known position; past
        STALE_AFTER we hang up, so Stellarium shows Disconnected rather than a
        confident reticle over where the telescope used to be.

        Or it can be right there, answering every read, and honestly say it
        has no idea -- which is what the INDI module now reports when its own
        link to the mount dies. Age cannot see that: pyobs keeps handing back
        the last value it was given, so every read succeeds and the clock
        never starts. Seen 2026-08-30, the module said `unknown` for a quarter
        of an hour while the reticle sat in the sky looking authoritative.

        A module admitting it does not know is better evidence than any
        timer, so it is taken at its word.
        """
        if self._tel.last_motion_status == "unknown":
            return True
        return self._tel.position_age > STALE_AFTER

    async def _send_forever(self, writer: asyncio.StreamWriter) -> None:
        """Feed one client the cached position until it goes away."""
        while True:
            if self._stale:
                if not self._stale_dropped:
                    log.warning("client     position %.0f s old; hanging up rather "
                                "than showing a stale reticle",
                                self._tel.position_age)
                    self._stale_dropped = True
                writer.close()
                return
            if self._tel.last_radec is not None:
                ra, dec = self._tel.last_radec
                writer.write(pack_position(ra, dec))
                await writer.drain()
            await asyncio.sleep(POLL_INTERVAL)

    def _request_slew(self, ra_deg: float, dec_deg: float) -> None:
        """Queue a target. Holding the slew key repeats it dozens of times a
        second, and firing a move_radec per packet makes them fight over the
        telescope's motion lock, so collapse repeats and keep one slew going."""
        target = (ra_deg, dec_deg)
        if target in (self._in_flight, self._pending):
            log.debug("client     ignoring repeat goto RA %.4f Dec %.4f", ra_deg, dec_deg)
            return
        log.info("client     goto request: RA %.4f Dec %.4f", ra_deg, dec_deg)
        self._pending = target
        if self._slewer is None or self._slewer.done():
            self._slewer = asyncio.create_task(self._slew_worker())
            self._tasks.add(self._slewer)
            self._slewer.add_done_callback(self._tasks.discard)

    async def _slew_worker(self) -> None:
        """Serialise slews; a burst of gotos collapses to the newest target."""
        while self._pending is not None:
            self._in_flight, self._pending = self._pending, None
            ra_deg, dec_deg = self._in_flight
            try:
                await self._tel.move_radec(ra_deg, dec_deg)
                log.info("pyobs      slew finished")
            except Exception as err:
                log.error("pyobs      slew to RA %.4f Dec %.4f failed: %s", ra_deg, dec_deg, err)
        self._in_flight = None

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        if self._stale:
            writer.close()      # nothing worth showing; do not log the churn
            return
        log.info("client     connected (%d connected)", len(self._clients) + 1)
        self._clients.add(writer)
        sender = asyncio.create_task(self._send_forever(writer))
        try:
            while True:
                header = await reader.readexactly(2)
                length = struct.unpack("<H", header)[0]
                if length < 2:
                    log.warning("client     bogus message length %d, dropping it", length)
                    break
                message = header + await reader.readexactly(length - 2)
                if length == GOTO_SIZE:
                    ra, dec, _ = unpack_goto(message)
                    self._request_slew(ra, dec)
                else:
                    log.debug("client     ignoring %d-byte message", length)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass                      # a client hanging up is not an error
        except Exception as err:
            log.error("client     error: %s", err)
        finally:
            sender.cancel()  # no orphaned senders across reconnects
            self._clients.discard(writer)
            # after the discard, so the count is what is actually left
            log.info("client     gone (%d connected)", len(self._clients))
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def run(self) -> None:
        # The listener comes up first: Stellarium can connect and sit there
        # quite happily while we are still chasing the telescope.
        poller = asyncio.create_task(self._poll_forever())
        server = await asyncio.start_server(self._handle, self._host, self._port)
        log.info("client     listening on %s:%d -- point Stellarium here",
                 self._host, self._port)
        try:
            async with server:
                await server.serve_forever()
        finally:
            poller.cancel()
            for task in list(self._tasks):
                task.cancel()
            await self._tel.close()

async def main() -> None:
    bridge = StellariumBridge(PyobsTelescope())
    try:
        await bridge.run()
    except asyncio.CancelledError:
        pass

class _DropSlixmppHandleError(logging.Filter):
    """slixmpp's XEP_0009 calls a handle_error() it does not define, so an
    error IQ from a module makes the library throw while logging the error.
    Two tracebacks, no consequence -- our retry catches the RemoteError."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "handle_error" not in record.getMessage() and not (
            record.exc_info and "handle_error" in str(record.exc_info[1]))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    # Must sit on the handler: records from child loggers propagate straight
    # to ancestor handlers and never see an ancestor logger's filters.
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_DropSlixmppHandleError())
    if CONFIG_APPLIED:
        log.info("config     %s: %s", CONFIG_FILE.name, ", ".join(CONFIG_APPLIED))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
