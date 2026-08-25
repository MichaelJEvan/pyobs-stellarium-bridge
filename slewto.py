#!/usr/bin/env python3

"""**********************************************************************

    Developer: Michael J. Evan
    MS Computer Science
    University of Massachusetts Dartmouth - Dec 2026
    Timeline: August 2026
    AAVSO member

    ------------------------------------------------------------------------

    *************** slewto pyobs telescope terminal commands ***************

    python slewto.py

    ------------------------------------------------------------------------
    names resolved for you with --name:

    python slewto.py --name Vega              # common names
    python slewto.py --name "M 31"            # Messier (quote if it has a space)
    python slewto.py --name "NGC 7000"        # NGC and IC
    python slewto.py --name "HIP 11767"       # HIP, HD, SAO, HR, Gaia DR3
    python slewto.py --name "Barnard 33"      # Barnard, Sh2-155, Abell, 3C
    python slewto.py --name Jupiter           # planets, Moon, Sun

    ----------------------------------------------------------------------
    coordinates -- RA first, Dec second, J2000:

    python slewto.py 213.915 19.182           # decimal degrees
    python slewto.py 201.2946 -11.1604        # negative Dec is fine
    python slewto.py 14h15m39s +19d10m55s     # sexagesimal, RA in HOURS
    python slewto.py 14:15:39 +19:10:55       # colon form, same meaning
    python slewto.py 13:25:11 -11:09:38       # "+" optional, "-" works

    A bare number is read as degrees; anything else as sexagesimal.

    astropy does the parsing, so other forms work too:

    python slewto.py "14 15 39" "+19 10 55"   # space separated (quote these)
    python slewto.py 14h15.65m +19d10.9m      # decimal minutes
    python slewto.py 14.26083h 19.182d        # decimal hours, explicit degrees
    python slewto.py 213.915d 19.182d         # "d" forces degrees for RA

    A trailing d or h overrides the default, so 213.915d is degrees of RA even
    though a bare 14:15:39 is read as hours.

    ------------------------------------------------------------------------
    Note:

    Spaces and letter case do not matter: NGC7000, "NGC 7000" and ngc7000 all
    work. Names are resolved against SIMBAD, falling back to NED. For big
    extended objects the two disagree about the centre -- NGC 7000 differs by
    0.2 deg between them, and VizieR by 0.5 deg -- so which database answered
    is reported whenever it is not SIMBAD.

    Caldwell numbers do not resolve -- SIMBAD does not index them, so use the
    NGC equivalent. Pluto does not either: astropy's built-in ephemeris omits
    it. Solar-system positions are computed for now, as seen from this site.
    ------------------------------------------------------------------------
    checks:

    python slewto.py --name M31 --dry-run               # report the target, do not move
    python slewto.py --name Vega --min-alt 30           # refuse below 30 deg (default 10)
    python slewto.py --name Mercury --sun-exclusion 0   # allow near the Sun

    A target is refused below --min-alt, which defaults to the 10 deg pyobs
    itself enforces, or within --sun-exclusion of the Sun (default 15 deg).

    A slew is also refused if the telescope is parked, parking or
    initialising: pyobs returns from move_radec without moving and without
    complaining in those states, so it would otherwise look like it worked.
    Wake the mount with scope.py's init command.
    ------------------------------------------------------------------------
    stellarium:

    python slewto.py --name Mars --no-follow  # leave Stellarium's view alone

    After a successful slew, Stellarium is asked to select the target and
    centre on it. Selecting by name also puts it in the info panel, which
    clicking cannot do -- the reticle sits on top of the target and takes the
    click. This needs the RemoteControl plugin enabled on port 8090; without
    it the slew still happens and a note explains what did not.
    ------------------------------------------------------------------------
    account:

    python slewto.py --name Vega --jid other@localhost

    Two logins on one JID kick each other endlessly, so this must be its own
    account -- not the bridge's and not scope.py's. Defaults to
    scratch@localhost, set by `slewto:` in config.yaml.

    Independent of the bridge: it commands pyobs directly, and touches
    Stellarium only to sync the view after a slew has already finished.

*****************************************************************************"""

import argparse
import asyncio
import logging
import math
import random
import re
import sys
import time

from bridge import PyobsTelescope, altitude_of

def separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """True angle between two positions.

    Subtracting RA would exaggerate wildly near the poles, where lines of RA
    converge -- a 49 arcsec RA difference at Dec 89 is under 1 arcsec of sky.
    """
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    a = SkyCoord(ra1 * u.deg, dec1 * u.deg)
    b = SkyCoord(ra2 * u.deg, dec2 * u.deg)
    return a.separation(b).arcsecond

from bridge import SLEWTO_JID as DEFAULT_JID   # set in config.yaml; must not
                                               # collide with the bridge or GUI
MIN_ALTITUDE = 10.0                 # pyobs refuses below this; check before asking
SUN_EXCLUSION = 15.0                # degrees; Mercury and Venus stray inside it
ASLEEP = ("parked", "parking", "initializing")   # pyobs silently ignores a
                                                 # slew in these states

STELLARIUM_API = "http://127.0.0.1:8090"   # Stellarium's RemoteControl plugin
STELLARIUM_TIMEOUT = 1.0                   # never let the view-follow stall a slew

EARTH_JOKES = [
    "Jokes on you -- you are standing on it.",
    "Slewing to Earth. Please look down. Target acquired.",
    "Cannot slew to Earth: the telescope is already bolted to it.",
    "Earth located. It is the large one under the tripod.",
    "Refusing. The dome is in the way, and so is the planet.",
    "Earth is a solved problem. Try something further away.",
    "Achievement unlocked: pointed a telescope at the floor.",
    "That is a geology instrument you want, not a telescope.",
    "You obviously failed Astronomy 101 🤣",
    "No tenure track position for you!",
    "Nice try ... you'll be detailing Professor Husser's car for that one...🔭",
    "Shitter's full - and so are you!",
]
PROGRESS_EVERY = 3.0

log = logging.getLogger("slewto")


def shield_negatives(argv: list[str]) -> list[str]:
    """Stop argparse reading a negative declination as an option flag.

    argparse only exempts plain negative numbers, so "-11:09:38" and
    "-11d09m38s" look like flags to it. A leading space is enough to make it
    a positional again, and the coordinate parser strips it back off.
    """
    return [f" {a}" if re.match(r"^-\d", a) else a for a in argv]

def parse_coords(ra_text: str, dec_text: str) -> tuple[float, float]:
    """Accept decimal degrees, or sexagesimal with RA in hours.

    A bare number is degrees; anything else is parsed as sexagesimal, where
    RA is hours (14h15m39s or 14:15:39) and Dec is degrees.
    """
    ra_text, dec_text = ra_text.strip(), dec_text.strip()
    try:
        return float(ra_text), float(dec_text)
    except ValueError:
        pass
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    try:
        c = SkyCoord(ra_text, dec_text, unit=(u.hourangle, u.deg))
    except Exception as err:
        sys.exit(f"could not read {ra_text!r} {dec_text!r} as coordinates: {err}")
    return c.ra.degree, c.dec.degree

def _site():
    from astropy.coordinates import EarthLocation
    import astropy.units as u
    from bridge import SITE_ELEV, SITE_LAT, SITE_LON
    return EarthLocation(lat=SITE_LAT * u.deg, lon=SITE_LON * u.deg,
                         height=SITE_ELEV * u.m)


def tell_stellarium(name: str | None, ra: float, dec: float) -> None:
    """Ask Stellarium to select the target and centre on it.

    Best effort and deliberately toothless: if the RemoteControl plugin is not
    enabled, or Stellarium is not running, or it stalls, we say so once and
    carry on. The slew never depends on this.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    def post(path: str, fields: dict) -> None:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(f"{STELLARIUM_API}{path}", data=data)
        urllib.request.urlopen(req, timeout=STELLARIUM_TIMEOUT).read()

    try:
        if name:
            # Selecting by name puts the object in the info panel -- clicking
            # cannot, because the telescope reticle sits on top of it.
            post("/api/main/focus", {"target": name})
        else:
            # Stellarium wants a J2000 unit vector here, not RA/Dec degrees.
            r, d = math.radians(ra), math.radians(dec)
            vec = (math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d))
            post("/api/main/focus", {"position": "[%.10f,%.10f,%.10f]" % vec})
    except Exception as err:
        # Deliberately broad: this is a convenience, and nothing it can do
        # wrong is worth failing a slew that already succeeded.
        reason = getattr(err, "reason", err)
        where = name or "the target"
        print(f"note: the slew above SUCCEEDED -- only Stellarium's view did "
              f"not follow ({reason}).")
        print(f"      Press Cmd-F and search {where} to see the reticle, or "
              f"enable Stellarium's")
        print(f"      RemoteControl plugin on port 8090 to have the view "
              f"follow automatically.")

def sun_distance(ra: float, dec: float) -> float:
    """Degrees between a target and the Sun, right now."""
    from astropy.coordinates import SkyCoord, get_body
    from astropy.time import Time
    import astropy.units as u
    t = Time.now()
    sun = get_body("sun", t, _site())
    target = SkyCoord(ra * u.deg, dec * u.deg)
    return target.separation(SkyCoord(sun.ra, sun.dec)).degree


def resolve(name: str) -> tuple[float, float]:
    """Look a target up by name -- solar system first, then SIMBAD.

    Solar-system positions are for right now and seen from this site, so they
    are a snapshot: the planets move, and a long slew to one lands slightly
    behind. Fine for pointing, not for precise work.
    """
    from astropy.coordinates import get_body, solar_system_ephemeris
    from astropy.time import Time

    key = name.strip().lower()
    # Anything Earth-ish resolves to a direction but never a target, and
    # someone will always try it for the laugh. Laugh back.
    if "earth" in key:
        sys.exit(random.choice(EARTH_JOKES))
    if key in set(solar_system_ephemeris.bodies):
        try:
            body = get_body(key, Time.now(), _site())
        except Exception as err:
            sys.exit(f"could not place {name!r}: {err}")
        # Use the GCRS position as returned: its axes already match J2000, and
        # it is the direction from *here*. Converting to ICRS would re-origin
        # it on the solar system barycentre -- 3.5 degrees wrong for Jupiter.
        return body.ra.degree, body.dec.degree

    # Pin the database rather than using astropy's default "all", which walks
    # SIMBAD -> NED -> VizieR and so can answer from a different catalogue run
    # to run. They disagree by up to half a degree on extended objects.
    from astropy.coordinates import SkyCoord
    from astropy.coordinates.name_resolve import sesame_database

    for database in ("simbad", "ned"):
        try:
            with sesame_database.set(database):
                c = SkyCoord.from_name(name)
        except Exception:
            continue
        if database != "simbad":
            print(f"note: {name!r} not in SIMBAD, position is from {database.upper()}")
        return c.ra.degree, c.dec.degree
    sys.exit(f"could not look up {name!r} in SIMBAD or NED")

async def slew(tel: PyobsTelescope, ra: float, dec: float) -> None:
    start = await tel.get_radec()
    print(f"from   RA {start[0]:9.4f}  Dec {start[1]:+9.4f}")
    t0 = time.monotonic()
    move = asyncio.create_task(tel.move_radec(ra, dec))
    while not move.done():
        try:
            now = await tel.get_radec()
            print(f"  t+{time.monotonic() - t0:5.1f}s  RA {now[0]:9.4f}  Dec {now[1]:+9.4f}")
        except Exception as err:
            print(f"  t+{time.monotonic() - t0:5.1f}s  ({err})")
        await asyncio.sleep(PROGRESS_EVERY)
    await move
    end = await tel.get_radec()
    alt = altitude_of(*end)
    off = separation_arcsec(end[0], end[1], ra, dec)
    print(f"landed RA {end[0]:9.4f}  Dec {end[1]:+9.4f}"
          f"{'' if alt is None else f'   alt {alt:.1f} deg'}"
          f"   ({off:.1f} arcsec off, {time.monotonic() - t0:.0f}s)")

async def run(args) -> int:
    ra, dec = (resolve(args.name) if args.name
               else parse_coords(args.ra, args.dec))
    label = f"{args.name} " if args.name else ""
    alt = altitude_of(ra, dec)

    print(f"target {label}RA {ra:.4f}  Dec {dec:+.4f}"
          f"{'' if alt is None else f'   alt {alt:.1f} deg'}")
    if alt is not None and alt < args.min_alt:
        print(f"REFUSING: {alt:.1f} deg is below the {args.min_alt:.0f} deg limit")
        return 1

    # Pointing near the Sun ruins cameras and eyepieces. Mercury and Venus are
    # the usual way to end up there, so check every target, not just those two.
    try:
        sun_sep = sun_distance(ra, dec)
    except Exception as err:
        print(f"WARNING: could not check the Sun's position ({err})")
        sun_sep = None
    if sun_sep is not None:
        if sun_sep < args.sun_exclusion:
            print(f"REFUSING: {sun_sep:.1f} deg from the Sun "
                  f"(limit {args.sun_exclusion:.0f}). Override with --sun-exclusion 0")
            return 1
        if sun_sep < 30.0:
            print(f"note: {sun_sep:.1f} deg from the Sun")
    if args.dry_run:
        print("dry run -- not moving")
        return 0

    tel = PyobsTelescope(jid=args.jid)
    try:
        await tel.connect()
    except Exception as err:  # noqa: keep the message below
        print(f"could not reach pyobs as {args.jid}: {err}\n"
              f"register the account on the VM with:\n"
              f"  sudo -u ejabberd /opt/ejabberd-23.10/bin/ejabberdctl "
              f"register {args.jid.split('@')[0]} localhost pyobs")
        return 1
    # A parked mount returns from move_radec without moving and without
    # complaining, which otherwise looks like a slew that worked.
    try:
        status = await tel.get_motion_status()
    except Exception as err:
        print(f"WARNING: could not read the motion status ({err})")
        status = None
    if status in ASLEEP:
        print(f"REFUSING: the telescope is {status} -- it would ignore this "
              f"slew without saying so.")
        print("  wake it with scope.py's  init  command first")
        await tel.close()
        return 1

    try:
        await slew(tel, ra, dec)
        if not args.no_follow:
            tell_stellarium(args.name, ra, dec)
    finally:
        await tel.close()
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ra", nargs="?", help="RA: degrees, or 14h15m39s / 14:15:39")
    ap.add_argument("dec", nargs="?", help="Dec: degrees, or +19d10m55s / +19:10:55")
    ap.add_argument("--name", help="look the target up by name instead")
    ap.add_argument("--jid", default=DEFAULT_JID,
                    help="XMPP account to use; must not be the bridge's")
    ap.add_argument("--min-alt", type=float, default=MIN_ALTITUDE,
                    help="refuse targets below this altitude")
    ap.add_argument("--sun-exclusion", type=float, default=SUN_EXCLUSION,
                    help="refuse targets this close to the Sun (0 disables)")
    ap.add_argument("--dry-run", action="store_true", help="check altitude only")
    ap.add_argument("--no-follow", action="store_true",
                    help="do not ask Stellarium to centre on the target")
    args = ap.parse_args(shield_negatives(sys.argv[1:]))

    if args.name is None and (args.ra is None or args.dec is None):
        ap.error("give RA and Dec, or --name "
                 "(or run scope.py for an interactive console)")

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-7s %(message)s")
    return asyncio.run(run(args))

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C quits this program. It does not command the telescope, so
        # say so rather than leaving a traceback and an assumption.
        print("\ninterrupted -- the telescope was NOT stopped and is probably "
              "still slewing.")
        print("  use scope.py and type abort if you want it to stop.")
        sys.exit(130)
