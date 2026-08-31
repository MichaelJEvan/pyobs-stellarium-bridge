#!/usr/bin/env python3
"""The bridge must hang up rather than show a position it cannot vouch for.

    python test_stale.py
"""

import asyncio
import struct

import bridge

PORT = 10099


class FakeTelescope:
    """A telescope whose position age we control directly."""

    def __init__(self):
        self.last_radec = (213.915, 19.182)
        self.age = 0.0
        self.connected = True
        self.last_motion_status = "tracking"

    @property
    def position_age(self):
        return self.age

    @property
    def module_visible(self):
        return True

    async def connect(self):
        pass

    async def close(self):
        pass

    async def get_radec(self):
        return self.last_radec


async def _read_one(timeout=2.0):
    """Connect and try to read a position packet. None means hung up on."""
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    try:
        data = await asyncio.wait_for(reader.readexactly(24), timeout)
        return struct.unpack("<HHQIii", data)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        return None
    finally:
        writer.close()


async def scenario():
    bridge.STALE_AFTER = 2.0
    bridge.POLL_INTERVAL = 0.2
    tel = FakeTelescope()
    b = bridge.StellariumBridge(tel, port=PORT)
    server = asyncio.create_task(b.run())
    await asyncio.sleep(0.5)
    results = {}

    results["fresh_serves"] = await _read_one() is not None

    tel.age = 99.0                      # telescope has gone quiet
    await asyncio.sleep(0.5)
    results["stale_refuses"] = await _read_one() is None

    tel.age = 0.0                       # ...and comes back
    await asyncio.sleep(0.5)
    results["recovers"] = await _read_one() is not None

    # A module that is present, answering, and says it has no idea.
    # Introduced 2026-08-30 by the INDI module: when its link to the mount
    # dies it reports `unknown` and stops publishing, but pyobs keeps handing
    # back the last value it was given, so the read still succeeds and the age
    # never grows. Age alone cannot see this -- and the reticle sat in
    # Stellarium looking authoritative while the mount was unreachable.
    tel.last_motion_status = "unknown"
    await asyncio.sleep(0.5)
    results["unknown_refuses"] = await _read_one() is None

    tel.last_motion_status = "tracking"
    await asyncio.sleep(0.5)
    results["recovers_from_unknown"] = await _read_one() is not None

    server.cancel()
    return results


def test_stale_handling():
    r = asyncio.run(scenario())
    assert r["fresh_serves"], "fresh position should be served"
    assert r["stale_refuses"], "stale position should NOT be served"
    assert r["recovers"], "should serve again once the position is fresh"
    assert r["unknown_refuses"], "a telescope reporting unknown must NOT be drawn"
    assert r["recovers_from_unknown"], "should serve again once the status is real"
    return r


if __name__ == "__main__":
    r = test_stale_handling()
    for name, ok in r.items():
        print(f"  ok  {name}")
    print(f"\n{len(r)} passed")
