# Stellarium ↔ pyobs bridge

**Developer:** Michael J. Evan  
Masters Computer Science  
University of Massachusetts Dartmouth - Dec 2026  
AAVSO member  


A Python suite that connects a pyobs observatory to Stellarium. The telescope
can be driven from Stellarium, from the terminal, or by pyobs itself — any
telescope control commands can be observed in real-time from Stellarium.

<img width="1832" height="1448" alt="Stellarium_tracking" src="https://github.com/user-attachments/assets/ac605a5b-2a0e-44a9-a91c-19c8c8304481" />
<img width="1832" height="1447" alt="pyobs_bridge_suite" src="https://github.com/user-attachments/assets/e508862b-86bb-4805-9e6f-1f6cb8a5d3b1" />

The suite was developed by integrating a control bridge (`bridge.py`) to the
pyobs observatory control program / simulator, which reports the pyobs
telescope's position in Stellarium once per second, providing a visual
reference of pyobs tracking within Stellarium. A live terminal readout of
where the telescope is pointing can be accessed via `nightwatch.py`, which reads
from `bridge.py`.

`slewto.py` slews the pyobs telescope from the command line, independent of
Stellarium and the bridge. It takes a target by name — stars, Messier, NGC,
catalogue IDs, planets — or as coordinates in decimal degrees or sexagesimal.
It checks altitude and distance from the Sun before committing, reports
progress during the slew and how close it landed, then syncs Stellarium's view
to the target.

`scope.py` is an interactive console for the pyobs telescope — slew, home,
park, abort and init from one prompt, staying connected between commands.

**Future Release:**

Integration with pyobs 2.0, which broadcasts its state instead of answering
polls. Currently the bridge polls pyobs and pushes to Stellarium at 1 Hz — two
clocks that don't line up, which is where the slight lag comes from. With a
broadcast underneath, the cache is always current and the polling client gets
fresh data whenever it asks.

An ASCOM Alpaca server, so SkyChart / Cartes du Ciel 4.2+ can connect
natively. Alpaca is a REST protocol that SkyChart already speaks, so no bridge
would be needed on that side — the server would expose the same pyobs
telescope over HTTP instead of Stellarium's binary protocol, reusing the
`PyobsTelescope` class this suite is built on. Anything else speaking Alpaca —
NINA, KStars, SkySafari — would work too.

## What's here

| file | what it does |
|---|---|
| `bridge.py` | Reports the telescope's position to Stellarium once a second, and forwards Stellarium's slew commands back to pyobs. |
| `nightwatch.py` | Live terminal readout of where the telescope is pointing, read from the bridge. |
| `slewto.py` | Command-line slewing, by name or coordinates. |
| `scope.py` | Interactive console — slew, home, park, abort, all from one prompt. |
| `test_*.py` | Five test files, 31 tests. None need pyobs or Stellarium. |
| `config.yaml` | Your settings — gitignored, copied from the example. |
| `config.example.yaml` | The template, with placeholders. |

## Setup

*Developed on a Mac running Stellarium and VS Code, talking to pyobs on a
Linux VM hosted by a thoroughly unremarkable old-n-dusty HP. None of that matters —
Stellarium runs on Mac, Windows and Linux, pyobs needs to be somewhere
reachable over the network, or the two can be on the same machine.*

**Tested against pyobs-core 1.54.4.** pyobs 2.0 changes how modules report
their position — the getter methods this relies on are replaced by published
state — so this will need work once 2.0.0 is released. It is not expected to
run against 2.0 as it stands.

**Prerequisite: a working pyobs installation**, with its XMPP server running.
Installing and configuring pyobs is out of scope here — see the pyobs
documentation for that. What follows covers only what these four programs
need on top of a working setup.

**Python 3.11.** The pyobs ecosystem requires `>=3.11,<3.14` — pyobs-core
installs on newer, but pyobs-gui and other parts do not — and `bridge.py` uses
`int | None`, so it will not import on 3.9. Any environment manager is fine.
Check you are in the one with pyobs in it:

```bash
python -c "import pyobs; print(pyobs.__version__)"
```

**Settings.** Copy the example and put your own values in it:

```bash
cp config.example.yaml config.yaml
```

Five sections: the machine running pyobs and the bridge's account, your site
coordinates, the port Stellarium connects to, and a separate account each for
`slewto.py` and `scope.py`. All four programs read it. It is gitignored.

Every program needs its own account. `slewto.py` and `scope.py` sharing one
was enough to make them kick each other off mid-slew.

Anything left out falls back to a placeholder in `bridge.py`. Those
placeholders point at `localhost` and a site at 0°N 0°E, so without a real
`config.yaml` the programs start, warn you clearly, and reach nothing. That is
deliberate: nothing identifying anybody's observatory is committed to this
repository.

**XMPP accounts.** Every client needs its own account: two logins sharing a
JID kick each other in an endless loop. On the machine running pyobs,
once ever:

```bash
EJ=/opt/ejabberd-23.10/bin/ejabberdctl        # wherever yours is; often just `ejabberdctl`
sudo -u ejabberd $EJ register stellarium localhost <password>   # the bridge
sudo -u ejabberd $EJ register scratch localhost <password>      # slewto.py
sudo -u ejabberd $EJ register console localhost <password>      # scope.py
```

Those commands create logins on the XMPP server running alongside pyobs — the same
way you would add accounts to a mail server. You choose the name and the
password; **whatever password you pick must match `pyobs.password` in
`config.yaml`**, because that is what your programs log in with. The accounts
live in ejabberd's own database, so they are created once and survive
reboots.

Five accounts exist in total, and they are unrelated to each other:

| account | belongs to | password lives in |
|---|---|---|
| `stellarium@localhost` | `bridge.py` | `config.yaml`, where you run it |
| `scratch@localhost` | `slewto.py` | `config.yaml`, where you run it |
| `console@localhost` | `scope.py` | `config.yaml`, where you run it |
| `telescope@localhost` | the simulator | `telcam.yaml`, with pyobs |
| `camera@localhost` | the simulator | `telcam.yaml`, with pyobs |

Changing yours does not affect the simulator's. To change one afterwards use
`ejabberdctl change_password <name> localhost <new>` and update the matching
file. `gui@localhost` belongs to the pyobs GUI. Do not share any of them.

The XMPP *resource* must stay `pyobs` — pyobs addresses peers as
`<module>@<domain>/<own resource>`, so changing it makes every module
invisible.

**Stellarium, two plugins.** Both are off by default; each needs "Load at
startup" ticked and a restart before it can be configured.

- **Telescope Control** — Add → "External software or a remote computer",
  host `localhost`, port `10001`, equinox **J2000**. Tick "Start/connect at
  startup" so it reconnects on its own.
- **Remote Control** — Server enabled, port `8090`. Only needed so `slewto.py`
  can sync the view; without it slewing still works and says so.

**Start pyobs** on that machine, however you normally do. With the simulator
used to develop this:

```bash
cd ~/pyobs-sim && source venv311/bin/activate && pyobs telcam.yaml
```

## Running it

```bash
python bridge.py                  # leave running; Stellarium connects to it
python nightwatch.py                   # optional, live readout in a second terminal
python slewto.py --name Jupiter   # optional, to manually slew the telescope
python scope.py                   # optional, interactive console
```

`bridge.py` takes no arguments — it reads `config.yaml`. For `nightwatch.py`,
`--host/--port` point it at a different bridge and `--once` prints a single
frame instead of updating. `slewto.py --help` documents every accepted target
format.

## How it works

```
   Observatory machine (a VM here; anything reachable works)
   ├── ejabberd                    always on
   ├── pyobs telescope module      always on
   └── pyobs scheduler / camera    always on
                    │
                    │  XMPP
                    │
   Your desk ───────┴───────────────────────────────
   ├── bridge.py     connects out to ejabberd, listens on localhost:10001
   ├── nightwatch.py      reads position packets from the bridge
   ├── scope.py      commands the telescope directly, over XMPP
   └── Stellarium    connects to localhost:10001
```

The bridge polls the telescope's position once a second and pushes it to
Stellarium as a 24-byte packet. Stellarium's slew commands arrive as 20-byte
packets going the other way. Anything that moves the telescope — Stellarium,
`slewto.py`, the pyobs scheduler, the GUI — shows up in the reticle, because
the bridge reports position rather than tracking commands.

The observatory does not depend on any of this. Kill the bridge, close
Stellarium, shut the laptop: the mount carries on.

## Tests

```bash
python test_protocol.py    # RA/Dec scaling and packing, 10 tests
python test_slew.py        # slew retry behaviour, 4 tests
python test_stale.py       # hangs up rather than showing a stale reticle, 3
python test_motion.py      # logs motion whoever commanded it, 4
python test_scope.py       # stopping the telescope honestly, 10 tests
```

All run cold in about a second — nothing else needs to be running.

## Behaviour worth knowing

**Stale positions.** If pyobs becomes unreachable, the bridge keeps showing
the last known position for 10 seconds, then hangs up on Stellarium rather
than leaving a confident reticle pointing where the telescope used to be.
Stellarium reconnects by itself once the position is fresh again.

**Repeated gotos.** Holding Stellarium's slew shortcut auto-repeats it dozens
of times a second. The bridge collapses repeats and runs one slew at a time;
without that, pyobs raises `AcquireLockFailed` in a cascade and has to be
restarted.

**Altitude.** pyobs refuses targets below 10°, checked against **real
wall-clock time** — not the faked time in `telcam.yaml`, which only drives the
camera's sky. Pick test targets that are up now.

**Simulator speed.** `telcam.yaml` sets `speed:` in degrees/second. The
default of 20 makes slews look like teleporting; 2–3 looks like a real mount.
Simulator only — real hardware moves at whatever speed it moves.

**Running alongside a scheduler.** Watching is safe — the bridge only reads
position unless something sends it a goto, so it can sit there all night while
pyobs runs a programme and nothing notices it. Slewing is different: if you
command a mount that a scheduler is already driving, two controllers want the
same telescope. pyobs's motion lock means one wins and the other gets
`AcquireLockFailed`. That is not a bug in either — it is what happens whenever
two things command one mount. Stopping the mount does not stop the scheduler
either; it carries on making decisions.

**Drive from one or the other, never both.** Watch from anywhere you like.

**Abort does nothing on the 1.54 simulator.** `DummyTelescope.stop_motion` is
an empty method, so a simulated slew cannot be stopped. `scope.py` tries,
fails, and says so rather than claiming success. pyobs 2.0 implements it, and
real telescope drivers do too — this is a simulator limitation, not a bug.

**A parked telescope ignores slews silently.** pyobs returns from
`move_radec` without moving and without complaining when the mount is parked,
parking or initialising. Both `scope.py` and `slewto.py` check for this and
refuse rather than appearing to succeed; `scope.py`'s `init` wakes it up.

## Working on the code

None of this is needed to run it — it is for tinkering.

**VS Code.** Cmd+Shift+P → "Python: Select Interpreter" → the 3.11 environment
with pyobs in it. If it is not listed, choose "Enter interpreter path" and give
the full path to that env's `bin/python`. The status bar should read 3.11; if
it reads 3.9 you are on the system Python and nothing will import. New
terminals then activate the environment on their own.

**Tests** need nothing running — no pyobs, no Stellarium, no network:

```bash
for t in test_*.py; do python "$t"; done
```

**Debugging.** The useful breakpoints are in `StellariumBridge._handle`, where
packets arrive, and `PyobsTelescope._call`, where every remote call goes
through.

**Where things live.** `bridge.py` is in three parts: the Stellarium wire
protocol, `PyobsTelescope` (which knows nothing about Stellarium and is meant
to be reused), and `StellariumBridge` (the TCP server). If you are adding
another front end — an ASCOM Alpaca server, say — you want the middle one and
nothing else.

**Keep environments at 3.11.** `uv venv <name> --python 3.11` or
`conda create -n <name> python=3.11`. Newer Python installs pyobs-core
successfully and then fails elsewhere in the ecosystem, which is a slow way to
find out.

## Support

Provided as-is, maintained when time allows. This is a side project outside
academia, so responses may be slow.

## License

MIT — see [LICENSE](LICENSE). Use it for anything, commercial or not; just
keep the copyright notice.

pyobs is MIT, astropy BSD-3-Clause, slixmpp MIT, so nothing in the dependency
chain restricts this.

## Known limits

- **Never run against real hardware.** Every slew has been a simulator that
  always answers and never jams.
- **The slew-timeout fallback has never fired, and may never need to.**
  `move_radec` blocks for the whole move; on a timeout the bridge watches
  motion status rather than re-issuing the command. Tested with the simulator
  slowed to 0.5 deg/s: a slew blocking for **5 minutes 10 seconds** did not
  time out. So there is no short RPC timeout on that call. The code stays
  because a real driver may behave differently, but it is unexercised.
- **One user, one client, one machine.** No dome, no scheduler, no weather
  system competing for the mount.
