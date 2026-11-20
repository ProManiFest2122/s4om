# S4OM - online multiplayer for The Sims 4

S4OM is a script mod that lets several players share one household in The Sims 4,
each controlling their own Sim, live. Website: https://s4om.gg - Discord: https://discord.gg/XgJe2T5MBc

> **Status:** in testing, not released. Not affiliated with or endorsed by Electronic Arts or Maxis.

## What syncs

Game clock and speed, Sim positions, chat, household money, moods and needs,
relationships, inventory, skills and stats, buy-mode purchases, group travel.

## How it connects

- **LAN / direct IP (free):** the host listens on TCP 7655 and friends connect to it.
- **Online:** host and friends both connect out to a relay and meet by room code, so
  nobody needs to forward ports. The relay only pipes bytes; the game protocol is
  unchanged. On this branch the relay is an open one you run yourself
  ([relay/](relay/README.md)): no account and no token, a player only types the room code.
  It does not use the s4om.gg servers.

In-game cheat-console commands:

| Command | Does |
|---|---|
| `mp_host` | Host a room (also opens it on the relay when one is configured) |
| `mp_join <ip>` | Join a host on your network |
| `mp_host_online` | Host online and show the room code |
| `mp_join_online <CODE>` | Join an online room by code (checks the room is open first) |
| `mp_relay [address]` | Show or change the relay this player uses (`host[:port]`, `default` to reset) |
| `mp_join_code <CODE>` | Join an online room by code, without the check |
| `mp_say <text>` | Chat |
| `mp_status` | Connection status |

### Playing without the console

[online_launcher.py](online_launcher.py) (built as `S4OM-Online.exe`) is the one file a player needs:

- **Host:** click **Host a game**, give the code to friends, play. The code stays the same.
- **Join:** type the code, click **Join**, start The Sims 4 (before or after the click, before or
  after the host - it does not matter).

Everything else happens by itself:

| What | Who does it |
|---|---|
| The mod is put into `Mods` (or updated), misnamed copies are moved aside | launcher |
| "Script Mods Allowed" is switched on in the game's `Options.ini` (backup kept) | launcher |
| The game picks up Host / Join while it is running - no `mp_apply` | mod (`session_watch.py`) |
| A joiner who is early waits for the host's room to open | mod (`network._join_when_open`) |
| The host's game is saved and sent to a player who is not in it yet - no `mp_resync` | mod (`world_handoff.py`) |
| A joiner at the main menu has the host's save opened for them; in a household they get one button, "Load host's game" | mod (`world_handoff.py`, `resync.py`) |
| The window shows what the game is really doing: hosting / waiting for the host / connected, and who is in the room | mod writes `Mods/mp_status.json`, launcher shows it |

Everybody plays in one save, the host's. A joiner's hello says which copy of the host's game it
is in (`world`, the id of the last copy received, and `hh`, the household loaded); the host sends
its game only when those do not match its own, so reconnecting from inside the host's game sends
nothing. Opening the save automatically needs a game version the mod knows (`autoload.KNOWN`);
on other versions the player is told to open it from Load Game.

The launcher writes `Documents\Electronic Arts\The Sims 4\Mods\mp_launcher_config.json`
(`mode`, `name`, `relay`, `room_code`, `at`).

The relay address is built in: `build.py --relay host:port` writes it to
`src/multimod/relay_default.py`. A `relay` entry in the config (or `mp_relay`) overrides it.

## Build

Requires **Python 3.7** - The Sims 4 runs 3.7 and the `.pyc` magic number must match.

```
"C:\Program Files\Python37\python.exe" build.py            # -> build/S4OM.ts4script
"C:\Program Files\Python37\python.exe" build.py --deploy   # also copy into your Mods folder
"C:\Program Files\Python37\python.exe" build.py --relay play.example.org:7656   # set the online server, then build
```

The launcher as a single .exe with the mod inside (any recent Python with PyInstaller; build the
mod first, and give `--add-data` an absolute path):

```
pyinstaller --onefile --windowed --name S4OM-Online --paths src/multimod --add-data "C:\path\to\build\S4OM.ts4script;." online_launcher.py
```

## Tests

```
set PYTHONUTF8=1
"C:\Program Files\Python37\python.exe" tools\two_process_test.py lan     # host + 2 clients, real TCP
"C:\Program Files\Python37\python.exe" tools\two_process_test.py relay   # same, through a local relay
"C:\Program Files\Python37\python.exe" tools\test_online_setup.py          # relay protocol, online commands, launcher logic
"C:\Program Files\Python37\python.exe" tools\test_world_handoff.py         # joiner starts first, host's game sent and opened by itself
"C:\Program Files\Python37\python.exe" tools\stress_test.py                # 12 players through the relay
```

The tools that import the mod keep its files in a throw-away folder (`S4OM_USERDIR`), not in
your real Mods folder. The older `virtual_test_*` suites write to
`~/Documents/Electronic Arts/The Sims 4/Mods` directly - run those with `HOME`/`USERPROFILE`
pointed somewhere else.

`tools/run_all_virtual_tests.py` runs the original SimSync suites.

## Credits and licence

S4OM is a fork of **SimSync / Sims4Multiplayer** by sora and k
(https://github.com/mo9652962-ai/sims4-multiplayer-dev). The original README is in
[README.zh-CN.md](README.zh-CN.md). Licensed under the **GNU GPL v3.0** - see [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).

Fixes made in this fork include: a handshake race that dropped the host's initial world
state, client chat not reaching other clients in rooms of 3+, chat sender names,
launcher-started hosts never becoming host, and reconnects that silently broke the
session key.

The Sims is a trademark of Electronic Arts Inc.
