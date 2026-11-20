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

[online_launcher.py](online_launcher.py) is a small window that does the same without the
console: **Host a game** shows a code, **Join a game** takes one. It writes
`Documents\Electronic Arts\The Sims 4\Mods\mp_launcher_config.json`, which the mod reads on load
(`mode`, `name`, `relay`, `room_code`, `host`, `port`); in a running game `mp_apply` reads it again.

The relay address is built in: `build.py --relay host:port` writes it to
`src/multimod/relay_default.py`. A `relay` entry in the config (or `mp_relay`) overrides it.

## Build

Requires **Python 3.7** - The Sims 4 runs 3.7 and the `.pyc` magic number must match.

```
"C:\Program Files\Python37\python.exe" build.py            # -> build/S4OM.ts4script
"C:\Program Files\Python37\python.exe" build.py --deploy   # also copy into your Mods folder
"C:\Program Files\Python37\python.exe" build.py --relay play.example.org:7656   # set the online server, then build
```

The launcher as a single .exe (any recent Python with PyInstaller):

```
pyinstaller --onefile --windowed --name S4OM-Online --paths src/multimod online_launcher.py
```

## Tests

```
set PYTHONUTF8=1
"C:\Program Files\Python37\python.exe" tools\two_process_test.py lan     # host + 2 clients, real TCP
"C:\Program Files\Python37\python.exe" tools\two_process_test.py relay   # same, through a local relay
"C:\Program Files\Python37\python.exe" tools\test_online_setup.py          # relay protocol, online commands, launcher logic
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
