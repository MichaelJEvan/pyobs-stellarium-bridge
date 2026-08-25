#!/usr/bin/env python3
"""Round-trip tests for the Stellarium wire format.

Needs neither the VM nor Stellarium -- run it cold:
    python test_protocol.py
"""

import struct

from bridge import (GOTO_SIZE, POSITION_SIZE, dec_to_raw, pack_position,
                    ra_to_raw, raw_to_dec, raw_to_ra, unpack_goto)

TOL = 1e-6  # degrees; the wire format resolves ~8e-8, so this is generous


def _fake_stellarium_goto(ra_deg, dec_deg, timestamp_us=0):
    """Build a goto packet the way Stellarium would, for the reverse direction."""
    return struct.pack("<HHQIi", GOTO_SIZE, 0, timestamp_us,
                       ra_to_raw(ra_deg), dec_to_raw(dec_deg))


def test_packet_sizes():
    assert POSITION_SIZE == 24
    assert GOTO_SIZE == 20
    assert len(pack_position(0.0, 0.0)) == 24


def test_ra_landmarks():
    """Quarter-turns must land on exact powers of two."""
    assert ra_to_raw(0.0) == 0
    assert ra_to_raw(90.0) == 2**30
    assert ra_to_raw(180.0) == 2**31
    assert ra_to_raw(270.0) == 3 * 2**30
    assert ra_to_raw(360.0) == 0, "a full turn must wrap back to zero"


def test_dec_landmarks():
    assert dec_to_raw(0.0) == 0
    assert dec_to_raw(45.0) == 2**29
    assert dec_to_raw(90.0) == 2**30
    assert dec_to_raw(-45.0) == -(2**29)
    assert dec_to_raw(-90.0) == -(2**30)


def test_southern_dec_stays_south():
    """A sign slip here puts the reticle in the wrong hemisphere."""
    for dec in (-1.0, -23.5, -60.0, -89.9):
        assert dec_to_raw(dec) < 0
        assert raw_to_dec(dec_to_raw(dec)) < 0


def test_ra_round_trip():
    for ra in (0.0, 0.5, 15.0, 83.633, 180.0, 266.417, 299.868, 359.999):
        assert abs(raw_to_ra(ra_to_raw(ra)) - ra) < TOL


def test_dec_round_trip():
    for dec in (-90.0, -66.5, -28.936, 0.0, 22.014, 45.0, 89.264, 90.0):
        assert abs(raw_to_dec(dec_to_raw(dec)) - dec) < TOL


def test_dec_clamped_at_poles():
    assert dec_to_raw(91.0) == 2**30
    assert dec_to_raw(-91.0) == -(2**30)


def test_position_header():
    """Stellarium reads length and type first; get those wrong and it hangs up."""
    packet = pack_position(83.633, 22.014, timestamp_us=1234567890, status=0)
    length, msg_type, timestamp, ra_raw, dec_raw, status = struct.unpack("<HHQIii", packet)
    assert length == 24
    assert msg_type == 0
    assert timestamp == 1234567890
    assert status == 0
    assert abs(raw_to_ra(ra_raw) - 83.633) < TOL
    assert abs(raw_to_dec(dec_raw) - 22.014) < TOL


def test_goto_round_trip():
    """Betelgeuse, Polaris, and a southern target through the reverse path."""
    for ra, dec in ((88.793, 7.407), (37.954, 89.264), (201.298, -11.161)):
        got_ra, got_dec, ts = unpack_goto(_fake_stellarium_goto(ra, dec, 42))
        assert abs(got_ra - ra) < TOL
        assert abs(got_dec - dec) < TOL
        assert ts == 42


def test_goto_rejects_bad_input():
    for bad in (b"", b"\x00" * 19, b"\x00" * 24):
        try:
            unpack_goto(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted a {len(bad)}-byte goto")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
