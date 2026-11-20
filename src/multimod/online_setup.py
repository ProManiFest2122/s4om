"""S4OM online play from the cheat console - no launcher, no account, no token.

  mp_host_online            host online; shows the room code to give your friends
  mp_join_online <CODE>     join a friend's room
  mp_relay [address]        show, or change, the server that connects you (host[:port])

Both players connect out to a relay (relay/relay.py) and meet by room code, so nobody has to
forward ports. The relay comes with the build (relay_default.DEFAULT_RELAY); mp_relay points
this player at another one.

Writes the same mp_launcher_config.json the launcher writes, so after loading a save the mod
reconnects on its own, exactly like a launcher player. Works the same on Windows and Mac.
"""

import json, os, random, socket, time

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

NO_RELAY = "No online server is set. Set one with: mp_relay <address>"


def _cfg():
    return network._load_launcher_config() or {}


def _save(cfg):
    path = network.LAUNCHER_CONFIG_PATH
    os.makedirs((os.path.dirname(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _default_name():
    return os.environ.get("USER") or os.environ.get("USERNAME") or "Player"


def relay_address():
    """'host:port' of the relay in use, or '' when none is set."""
    rc = network._relay_cfg()
    return "{}:{}".format(rc[0], rc[1]) if rc else ""


def set_relay(address):
    """Point this player at a relay. '' or 'default' goes back to the one the build came with."""
    address = str(address or "").strip()
    cfg = _cfg()
    if address.lower() in ("", "default"):
        cfg.pop("relay", None)
        _save(cfg)
        now = relay_address()
        return (True, "Using the built-in server: {}".format(now) if now else NO_RELAY)
    host, _, port = address.partition(":")
    if not host or " " in address or (port and not port.isdigit()):
        return (False, "Usage: mp_relay <address>   e.g. mp_relay play.example.org  or  mp_relay 203.0.113.7:7656")
    cfg["relay"] = address
    _save(cfg)
    return (True, "Online server set to {}".format(relay_address()))


def find_room(code, timeout=6):
    """Ask the relay whether room `code` is open. True / False, or None when the relay can't be reached."""
    rc = network._relay_cfg()
    if rc is None:
        return
    try:
        s = socket.create_connection((rc[0], rc[1]), timeout=timeout)
        try:
            s.sendall("S4OM FIND {}\n".format(code).encode("ascii"))
            reply = s.recv(64).decode("ascii", "replace").strip()
        finally:
            s.close()
    except OSError:
        return
    return reply.startswith("OK")


def new_code():
    return "".join((random.choice(CODE_ALPHABET) for _ in range(6)))


def host_online():
    if not relay_address():
        return (False, NO_RELAY)
    code = new_code()
    cfg = _cfg()
    cfg.update(mode="host", room_code=code, auto_sync=True, at=int(time.time()))
    cfg.setdefault("name", _default_name())
    cfg.pop("host", None)
    _save(cfg)
    (ok, desc) = network._apply_launcher_config()
    if not ok:
        return (False, desc)
    return (True, "Hosting online. Room code: {}  (friends type: mp_join_online {})".format(code, code))


def join_online(code):
    code = str(code or "").strip().upper()
    if len(code) < 4:
        return (False, "Usage: mp_join_online <ROOM CODE>")
    if not relay_address():
        return (False, NO_RELAY)
    found = find_room(code)
    if found is None:
        return (False, "S4OM: can't reach the online server ({}) - check your internet".format(relay_address()))
    cfg = _cfg()
    cfg.update(mode="join", room_code=code, auto_sync=True, at=int(time.time()))
    cfg.setdefault("name", _default_name())
    cfg.pop("host", None)
    _save(cfg)
    ok, desc = network._apply_launcher_config()
    if ok and not found:
        # not an error any more: the game keeps asking and joins when the host opens the room
        return (True, "Room {} isn't open yet - waiting for the host. You'll be connected as soon as they're in the game.".format(code))
    return (ok, desc if ok else "S4OM: " + desc)


if sims4 is not None:

    @sims4.commands.Command("mp_host_online", command_type=(sims4.commands.CommandType.Live))
    def mp_host_online(_connection=None):
        sims4.commands.CheatOutput(_connection)(host_online()[1])

    @sims4.commands.Command("mp_join_online", command_type=(sims4.commands.CommandType.Live))
    def mp_join_online(code=None, _connection=None):
        sims4.commands.CheatOutput(_connection)(join_online(code)[1])

    @sims4.commands.Command("mp_relay", command_type=(sims4.commands.CommandType.Live))
    def mp_relay(address=None, _connection=None):
        out = sims4.commands.CheatOutput(_connection)
        if address is None:
            out("Online server: {}".format(relay_address() or "none - set one with mp_relay <address>"))
        else:
            out(set_relay(address)[1])
