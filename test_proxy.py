#!/usr/bin/env python3
"""A proxy must not outlive the module it points at.

pyobs 2.0 delivers state by subscription, and a Proxy caches what it has been
sent. If the module goes away, the subscription dies with it -- but the proxy
keeps handing back the last value it ever received, and nothing in it says so.
A caller then reads a frozen position forever and cannot tell.

Seen 2026-08-26: scope.py held a proxy across a container restart and reported
"arrived" at a position 44 degrees from the target for the rest of the session.

    python test_proxy.py
"""

import asyncio

import bridge
from bridge import PyobsTelescope


class FakeProxy:
    def __init__(self, tag):
        self.tag = tag
        self.closed = False


class FakeComm:
    """A comm whose module can come and go, handing out a new proxy each time."""

    def __init__(self):
        self.clients = ["telescope"]
        self.handed_out = []

    def proxy(self, _name, _interface=None):
        comm = self

        class _Ctx:
            async def __aenter__(self):
                proxy = FakeProxy(f"proxy{len(comm.handed_out)}")
                comm.handed_out.append(proxy)
                return proxy

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _telescope():
    tel = PyobsTelescope()
    tel._comm = FakeComm()
    return tel


def test_the_same_proxy_is_reused_while_the_module_is_there():
    """Resolving per call would lose the subscription, so it must be held."""
    async def run():
        tel = _telescope()
        first = await tel._get_proxy()
        second = await tel._get_proxy()
        return first, second, tel._comm.handed_out
    first, second, handed_out = asyncio.run(run())
    assert first is second, "a held proxy must be reused"
    assert len(handed_out) == 1, f"resolved {len(handed_out)} proxies, want 1"


def test_a_proxy_is_dropped_when_its_module_disappears():
    """The frozen-position bug: a dead subscription must not be kept."""
    async def run():
        tel = _telescope()
        first = await tel._get_proxy()
        tel._comm.clients = []          # the module goes away
        second = await tel._get_proxy()
        return first, second
    first, second = asyncio.run(run())
    assert second is not first, \
        "a proxy whose module vanished must be replaced, not reused"


def test_it_recovers_when_the_module_comes_back():
    """A restarted module must be picked up without restarting the program."""
    async def run():
        tel = _telescope()
        await tel._get_proxy()
        tel._comm.clients = []
        await tel._get_proxy()
        tel._comm.clients = ["telescope"]   # module restarts
        third = await tel._get_proxy()
        fourth = await tel._get_proxy()
        return third, fourth
    third, fourth = asyncio.run(run())
    assert third is fourth, "should settle on one proxy again once it is back"


def test_a_module_event_replaces_the_proxy_though_presence_never_dropped():
    """The case a presence check cannot catch, and the reason for the events.

    scope.py only looks at pyobs when you type something. On 2026-08-27 a
    container restart happened entirely between two of its checks: the module
    was there before and there after, so nothing looked wrong -- but the
    subscription had died with the old container and the proxy replayed the
    new module's startup position for two hours while the bridge tracked
    correctly. pyobs's own ModuleOpened/ModuleClosed events close that window.
    """
    async def run():
        tel = _telescope()
        first = await tel._get_proxy()
        # The module went away and came back while nobody was looking, so
        # presence never dropped -- exactly the situation that fooled us.
        await tel._module_changed(bridge.ModuleOpenedEvent(), "telescope")
        assert tel._comm.clients == ["telescope"], "presence should still look fine"
        second = await tel._get_proxy()
        return first, second
    first, second = asyncio.run(run())
    assert second is not first, \
        "a module event must force a fresh proxy even while presence looks fine"


def test_an_event_about_another_module_is_ignored():
    """A camera restarting says nothing about our telescope's subscription."""
    async def run():
        tel = _telescope()
        first = await tel._get_proxy()
        await tel._module_changed(bridge.ModuleOpenedEvent(), "camera")
        second = await tel._get_proxy()
        return first, second
    first, second = asyncio.run(run())
    assert second is first, "another module's event must not disturb our proxy"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
