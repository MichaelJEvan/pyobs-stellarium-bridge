#!/usr/bin/env python3
"""**************************************************************************

    Developer: Michael J. Evan — August 2026
    Masters Computer Science
    University of Massachusetts Dartmouth - Dec 2026
    AAVSO member

    Live readout of where the telescope is pointing.

    Connects to the bridge exactly as Stellarium does, so it needs no pyobs
    account and puts no extra load on the telescope -- it just reads the position
    packets the bridge is already sending.

    python nightwatch.py

    Shows local and UTC time, RA/Dec as reported by the telescope, and Alt/Az
    computed here from that RA/Dec and the site coordinates in config.yaml --
    so a wrong site gives confidently wrong altitudes.

    Motion is inferred from the position changing, not read from pyobs; the
    position packet carries no status field we can trust. "very low" appears
    below 15 degrees, "BELOW HORIZON" below 0.

**************************************************************************"""

import argparse
import shutil
import socket
import struct
import sys
import time

from astropy.coordinates import FK5, AltAz, EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u

from bridge import (HOST, POSITION_SIZE, PORT, SITE_ELEV, SITE_LAT, SITE_LON,
                    raw_to_dec, raw_to_ra)

# Alt/Az comes from the site constants in bridge.py -- a wrong site gives
# confidently wrong altitudes with nothing to hint at it.

MOVING_THRESHOLD = 0.001   # degrees between packets that counts as movement
STALE_AFTER = 5.0          # seconds without a packet before we say so


def _site() -> EarthLocation:
    return EarthLocation(lat=SITE_LAT * u.deg, lon=SITE_LON * u.deg,
                         height=SITE_ELEV * u.m)


def to_altaz(ra_deg: float, dec_deg: float, site: EarthLocation) -> tuple[float, float]:
    """Where that RA/Dec sits in the sky right now."""
    frame = AltAz(obstime=Time.now(), location=site)
    p = SkyCoord(ra_deg * u.deg, dec_deg * u.deg).transform_to(frame)
    return p.alt.degree, p.az.degree


def meridian_minutes(ra_deg: float, site: EarthLocation) -> float:
    """Minutes until this RA crosses the meridian; negative means it already has.

    Hour angle is local sidereal time minus RA (of date, so the bridge's J2000
    value is precessed first). Negative HA is east of the meridian, heading
    for it at one sidereal hour per hour.
    """
    now = Time.now()
    eod = SkyCoord(ra_deg * u.deg, 0 * u.deg,
                   frame=FK5(equinox="J2000")).transform_to(FK5(equinox=now))
    lst = now.sidereal_time("apparent", longitude=site.lon)
    ha = (lst - eod.ra).wrap_at(180 * u.deg).hourangle
    return float(-ha * 60.0 / 1.0027379)   # sidereal minutes -> clock minutes


def meridian_line(minutes: float) -> str:
    m = abs(minutes)
    clock = f"{int(m // 60)}h {int(m % 60):02d}m" if m >= 60 else f"{int(m)}m {int(m % 1 * 60):02d}s"
    return f"in {clock}" if minutes >= 0 else f"crossed {clock} ago"


def ra_hms(ra_deg: float) -> str:
    hours = ra_deg / 15.0
    h = int(hours)
    m = int((hours - h) * 60)
    s = (hours - h - m / 60) * 3600
    return f"{h:02d}h{m:02d}m{s:04.1f}s"


def dec_dms(dec_deg: float) -> str:
    sign = "-" if dec_deg < 0 else "+"
    d = abs(dec_deg)
    deg = int(d)
    m = int((d - deg) * 60)
    s = (d - deg - m / 60) * 3600
    return f"{sign}{deg:02d}°{m:02d}'{s:04.1f}\""


def term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def angle_dms(deg: float) -> str:
    """Degrees as d m s, for altitude and azimuth."""
    sign = "-" if deg < 0 else ""
    d = abs(deg)
    whole = int(d)
    m = int((d - whole) * 60)
    s = (d - whole - m / 60) * 3600
    return f"{sign}{whole:02d}°{m:02d}'{s:04.1f}\""


def render(state: dict, width: int = 80) -> list[str]:
    """Lay the readout out compactly, sized to the terminal."""
    now = time.strftime("%H:%M:%S")
    utc = time.strftime("%H:%M:%S", time.gmtime())
    inner = max(28, min(width - 4, 52))
    lines = [None,          # rules above and below the title, sized once the
             "   pyobs telescope coordinates",
             None,          # rows are built
             # local in the left column, UTC in the right, so both land on
             # the columns the coordinates use. Z marks UTC and sits where
             # the degree signs do.
             f"  Time {now:>12}  {utc:>9}Z"]

    if state.get("ra") is None:
        lines.append(f"  {state['note']}")
    else:
        alt, az = state["alt"], state["az"]
        lines.append(f"  RA   {ra_hms(state['ra']):>13} {state['ra']:>9.4f}°")
        lines.append(f"  Dec  {dec_dms(state['dec']):>13} {state['dec']:>+9.4f}°")
        flag = "  BELOW HORIZON" if alt < 0 else ("  very low" if alt < 15 else "")
        lines.append(f"  Alt  {angle_dms(alt):>13} {alt:>9.4f}°{flag}")
        lines.append(f"  Az   {angle_dms(az):>13} {az:>9.4f}°")
        if state.get("meridian") is not None:
            lines.append("")
            lines.append(f"  Meridian{meridian_line(state['meridian']):>24}")

    # Rule ends one character past the widest row, rather than running on to
    # the edge of the terminal.
    widest = max(len(l) for l in lines if l)
    rule = "  " + "-" * min(inner, widest - 2)
    lines[0] = lines[2] = rule

    motion, age = state["motion"], state["age"]
    # right-align the freshness marker to the same column as the values above
    lines.append(f"  {motion:<19}{age:>9}")

    # blank line between rows; title and rule stay together
    spaced = lines[:3]          # rule, title, rule -- kept tight together
    for line in lines[3:]:
        spaced += ["", line]
    return spaced


_last_width = None


def draw(lines: list[str], drawn: int, tty: bool) -> int:
    """Redraw in place. Returns how many rows were written.

    Two things break the cursor-up arithmetic: a line long enough to wrap
    (it occupies two rows), and the window being resized under us (rows drawn
    at the old width are still on screen). Truncate for the first, and repaint
    the whole screen for the second.
    """
    global _last_width
    size = shutil.get_terminal_size((80, 24))
    width, height = size.columns, size.lines
    lines = [line[:width - 1] for line in lines]

    # A frame as tall as the pane scrolls the terminal when the last line is
    # written, so cursor-up no longer lands where the frame began and the
    # display drifts a row per second. Drop the blank spacer lines to fit;
    # if it still will not, give up on redrawing and just scroll.
    if len(lines) > height - 1:
        lines = [line for line in lines if line.strip()]
    if len(lines) > height - 1:
        tty = False

    if tty and width != _last_width:
        sys.stdout.write("\033[2J\033[H")   # resized: start from a clean screen
        drawn = 0
    _last_width = width
    if tty and drawn:
        sys.stdout.write(f"\033[{drawn}A")
    for line in lines:
        sys.stdout.write(("\033[K" + line + "\n") if tty else (line + "\n"))
    sys.stdout.flush()
    return len(lines)


def read_exactly(sock: socket.socket, n: int) -> tuple[str, bytes | None]:
    """-> ("ok", data) | ("timeout", None) | ("closed", None).

    A timeout is not a hangup: it means the packets went quiet, which the
    display should show rather than silently freeze on the last reading.
    """
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (socket.timeout, TimeoutError):
            return "timeout", None
        if not chunk:
            return "closed", None
        buf += chunk
    return "ok", buf


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    args = ap.parse_args()

    site = _site()
    tty = sys.stdout.isatty() and not args.once
    state = {"ra": None, "dec": None, "meridian": None,
             "motion": "connecting", "age": "",
             "note": "waiting for the bridge..."}
    prev = None
    last_packet = 0.0
    sock = None
    drawn = 0

    while True:
        if sock is None:
            try:
                sock = socket.create_connection((args.host, args.port), timeout=2)
                sock.settimeout(2)
                state["note"] = "connected, waiting for a position..."
            except OSError:
                state.update(ra=None, motion="no bridge",
                             note=f"cannot reach the bridge on {args.host}:{args.port}",
                             age="")
                drawn = draw(render(state, term_width()), drawn, tty)
                if args.once:
                    return
                time.sleep(2)
                continue

        status, data = read_exactly(sock, POSITION_SIZE)
        if status == "timeout":
            # Still connected, just nothing arriving -- say so and keep waiting.
            gap = time.monotonic() - last_packet if last_packet else None
            if gap is not None and gap > STALE_AFTER:
                state.update(motion="no packets",
                             age=f"last packet {gap:.0f}s ago")
                drawn = draw(render(state, term_width()), drawn, tty)
            if args.once:
                return
            continue
        if status == "closed":
            # A hangup is the bridge telling us the position went stale.
            sock.close()
            sock = None
            state.update(ra=None, motion="bridge hung up",
                         note="bridge has no fresh position -- telescope unreachable?",
                         age="")
            drawn = draw(render(state, term_width()), drawn, tty)
            if args.once:
                return
            time.sleep(2)
            continue

        _, _, _, ra_raw, dec_raw, _ = struct.unpack("<HHQIii", data)
        ra, dec = raw_to_ra(ra_raw), raw_to_dec(dec_raw)
        alt, az = to_altaz(ra, dec, site)
        state["meridian"] = meridian_minutes(ra, site)
        moved = prev is not None and (abs(ra - prev[0]) > MOVING_THRESHOLD
                                      or abs(dec - prev[1]) > MOVING_THRESHOLD)
        prev = (ra, dec)
        last_packet = time.monotonic()
        # "steady" rather than "tracking": the position holding still is all we
        # can see from out here -- a parked or idle mount looks identical.
        state.update(ra=ra, dec=dec, alt=alt, az=az,
                     motion="moving" if moved else "steady", age="live")
        drawn = draw(render(state, term_width()), drawn, tty)
        if args.once:
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
