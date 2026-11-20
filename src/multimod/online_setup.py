# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\online_setup.py
# Compiled at: 2026-10-23 23:24:13
# Size of source mod 2**32: 4437 bytes

"""S4OM online play without the launcher - for Mac players (the launcher is Windows-only)
or anyone who'd rather use the cheat console.

  mp_online <token> [us|eu]   save your S4OM token (and server) - once
  mp_host_online [us|eu]      host online; prints the room code to give your friends
  mp_join_online <CODE>       join a friend's room (finds their server by itself)

Writes the same mp_launcher_config.json the launcher writes, so after loading a save the
mod reconnects on its own, exactly like a launcher player. Windows and Mac players can be
in the same room - the S4OM servers don't care what you play on.
"""

import json, os, random, socket

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

RELAYS = {"us": "relay.s4om.gg:7656", "eu": "relay-eu.s4om.gg:7656"}

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _cfg():
    return network._load_launcher_config() or {}


def _save(cfg):
    path = network.LAUNCHER_CONFIG_PATH
    os.makedirs((os.path.dirname(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def set_token(token, region="us"):
    token = str(token or "").strip()
    region = str(region or "us").lower()
    if len(token) < 8:
        return (False, "Usage: mp_online <your S4OM token> [us|eu]  - get it at s4om.gg/token")
    if region not in RELAYS:
        return (False, "Server must be us or eu")
    cfg = _cfg()
    cfg.update(relay_token=token, region=region, relay=(RELAYS[region]), auto_sync=True)
    cfg.setdefault("name", os.environ.get("USER") or os.environ.get("USERNAME") or "Player")
    _save(cfg)
    return (
        True,
        "Token saved ({} server). Now: mp_host_online  or  mp_join_online <CODE>".format(region.upper()),
    )


def find_region(code, timeout=6):
    """Ask each S4OM server whether room `code` is open there (relay FIND command)."""
    for region, addr in RELAYS.items():
        host, port = addr.split(":")
        try:
            s = socket.create_connection((host, int(port)), timeout=timeout)
            s.sendall("S4OM FIND {}\n".format(code).encode("ascii"))
            reply = s.recv(64).decode("ascii", "replace").strip()
            s.close()
            if reply.startswith("OK"):
                return region
        except OSError:
            continue

    return


def host_online(region=None):
    cfg = _cfg()
    if not cfg.get("relay_token"):
        return (False, "First save your token: mp_online <token> [us|eu]")
    region = str(region or cfg.get("region") or "us").lower()
    if region not in RELAYS:
        return (False, "Server must be us or eu")
    code = "".join((random.choice(CODE_ALPHABET) for _ in range(6)))
    cfg.update(mode="host", room_code=code, region=region, relay=(RELAYS[region]))
    cfg.pop("host", None)
    _save(cfg)
    (ok, desc) = network._apply_launcher_config()
    if not ok:
        return (False, desc)
    return (
        True,
        "Hosting online on the {} server. Room code: {}  (friends type: mp_join_online {})".format(
            region.upper(), code, code
        ),
    )


def join_online(code):
    code = str(code or "").strip().upper()
    cfg = _cfg()
    if not cfg.get("relay_token"):
        return (False, "First save your token: mp_online <token> [us|eu]")
    if len(code) < 4:
        return (False, "Usage: mp_join_online <ROOM CODE>")
    region = find_region(code) or cfg.get("region") or "us"
    cfg.update(mode="join", room_code=code, region=region, relay=(RELAYS[region]))
    cfg.pop("host", None)
    _save(cfg)
    ok, desc = network._apply_launcher_config()
    return (ok, desc if ok else "S4OM: " + desc)


if sims4 is not None:

    @sims4.commands.Command("mp_online", command_type=(sims4.commands.CommandType.Live))
    def mp_online(token=None, region="us", _connection=None):
        sims4.commands.CheatOutput(_connection)(set_token(token, region)[1])

    @sims4.commands.Command("mp_host_online", command_type=(sims4.commands.CommandType.Live))
    def mp_host_online(region=None, _connection=None):
        sims4.commands.CheatOutput(_connection)(host_online(region)[1])

    @sims4.commands.Command("mp_join_online", command_type=(sims4.commands.CommandType.Live))
    def mp_join_online(code=None, _connection=None):
        sims4.commands.CheatOutput(_connection)(join_online(code)[1])
