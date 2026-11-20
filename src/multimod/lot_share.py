# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\lot_share.py
# Compiled at: 2026-11-15 21:31:47
# Size of source mod 2**32: 7878 bytes

"""S4OM Lot Share - build mode changes reach everyone through the game's own Library.

Walls, floors, roofs and terrain live in the game's C++ side, so they can't be copied over
the network piece by piece. What the game CAN do is save a lot to My Library and place a
Library lot onto the current lot with Sims still on it. So:

  1. a player leaves build mode after changing the lot -> if they didn't click
     "Save Lot to My Library", they're reminded to
  2. the S4OM launcher sees the new Library lot (Tray folder) and sends it to the room
  3. everyone else's launcher puts it in their Tray and tells the game (mp_cmd.json
     "mp_lot_ready") -> notification: Build mode > Library > Place Lot

Build lock: entering build mode tells the others "<name> is building" so two people don't
change the same lot at once. It's a warning, nobody is kicked out of build mode.

Launcher config "build_share": "auto" (default - the host's changes go out as a re-sync, see
resync.py; joiners use the Library), "library" (everyone uses the Library) or "save" (host only).
"""

import os

from multimod import userdir as _userdir

import time

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

LOCK_STALE = 2700

_hooks_installed = False

_enter_at = 0.0

_lock = None

def _tray_dir():
    try:
        from multimod import lobby

        return os.path.join(os.path.dirname(lobby.SAVES_DIR), "Tray")
    except Exception:
        return _userdir.sims_path("Tray")

def _in_session():
    return bool(network._clients) or network._client_socket is not None

def _my_name():
    try:
        cfg = network._load_launcher_config() or {}
        return str(cfg.get("name") or "A player")[:16]
    except Exception:
        return "A player"

def _zone_id():
    try:
        import services

        return int(services.current_zone_id() or 0)
    except Exception:
        return 0

def share_mode():
    """"auto" (default): the host's build changes go out as a re-sync the moment they leave build
    mode (resync.py - one click for everyone to load), a joiner's go through the Library.
    "library": everyone uses the Library. "save": host only, re-sync (the old behaviour)."""
    try:
        v = (network._load_launcher_config() or {}).get("build_share", "auto")
        if v in ('auto', 'library', 'save'):
            return v
        return "auto"
    except Exception:
        return "auto"

def uses_library():
    """Whether THIS player's build changes go through the Library."""
    mode = share_mode()
    return mode == "library" or mode == "auto" and not network._is_host

def library_saved_since(since, tray=None):
    """True if a lot (.blueprint) was written to the Library after `since`."""
    tray = tray or _tray_dir()
    try:
        for f in os.listdir(tray):
            if f.lower().endswith(".blueprint") and os.path.getmtime(os.path.join(tray, f)) >= since - 2:
                return True

    except OSError:
        pass

    return False

def _send(msg):
    if network._is_host:
        network._broadcast(msg)
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, msg)

def lock_text(lock, now=None):
    """What to tell a player entering build mode, or None."""
    now = time.time() if now is None else now
    if not lock or now - lock.get("at", 0) > LOCK_STALE:
        return
    return "S4OM: {} is building on this lot right now. Changes you make won't reach them - wait until they share their lot.".format(
        lock["name"]
    )

def _live_placing():
    """S4OM itself is in build mode placing the host's lot (lot_live.py) - that isn't the player
    building: no reminders, no 'is building' notice to the room."""
    try:
        from multimod import lot_live

        return bool(lot_live._busy)
    except Exception:
        return False

def _on_enter(*args, **kwargs):
    global _enter_at
    global _lock
    _enter_at = time.time()
    if _live_placing():
        return
    if not _in_session() or share_mode() == "save":
        return
    if not uses_library():
        network._notify(
            "S4OM: build away - when you leave build mode your changes go to everyone automatically."
        )
        _send({"type": "build_lock", "on": True, "name": _my_name(), "zone": _zone_id()})
        return
    text = lock_text(_lock if _lock and (_lock.get("zone") in (0, _zone_id())) else None)
    if text:
        network._notify(text)
    else:
        network._notify(
            "S4OM: when you're done, click 'Save Lot to My Library' (top right in build mode) before you leave - S4OM sends it to everyone."
        )
    _send({"type": "build_lock", "on": True, "name": _my_name(), "zone": _zone_id()})

def _on_exit(*args, **kwargs):
    if _live_placing():
        return
    if not _in_session() or share_mode() == "save":
        return
    _send({"type": "build_lock", "on": False, "name": _my_name(), "zone": _zone_id()})
    if not uses_library():
        return
    changed = True
    try:
        import services

        changed = bool(services.get_zone_manager().build_buy_zones_changed)
    except Exception:
        pass

    if not changed:
        return
    if library_saved_since(_enter_at):
        network._notify(
            "S4OM: lot saved to your Library - the launcher is sending it to everyone. Keep the S4OM launcher open."
        )
    else:
        network._notify(
            "S4OM: you changed the lot but didn't save it to your Library, so the others don't have it. Go back to build mode and click 'Save Lot to My Library' (top right)."
        )
    network._log(
        "lot_share: build exit changed={} library_saved={}".format(changed, library_saved_since(_enter_at))
    )

def on_lock(data):
    """build_lock from another player (host forwards joiners' locks to everyone)."""
    global _lock
    name = str(data.get("name", "Someone"))[:16]
    if data.get("on"):
        _lock = {"name": name, "zone": int(data.get("zone") or 0), "at": (time.time)()}
        network._notify("S4OM: {} is building - wait for their lot before you change anything.".format(name))
    elif _lock:
        if _lock.get("name") == name:
            _lock = None

def on_lot_ready(data):
    """mp_lot_ready from the launcher: a lot arrived in our Tray."""
    by = str(data.get("by") or "Another player")[:32]
    name = str(data.get("name") or "")[:60]
    network._notify(
        "S4OM: {} updated the lot{}. Build mode > Library (My Library, newest first) > pick it > Place Lot. Your Sims stay - they just step outside for a moment.".format(
            by, " ('{}')".format(name) if name else ""
        )
    )
    network._log("lot_share: lot ready from {} '{}'".format(by, name))

def _install_hooks():
    global _hooks_installed
    if _hooks_installed:
        return
    try:
        import build_buy

        build_buy.register_build_buy_enter_callback(_on_enter)
        build_buy.register_build_buy_exit_callback(_on_exit)
        _hooks_installed = True
        network._log("lot_share: build mode hooks installed")
    except Exception:
        pass

def tick(now=None):
    _install_hooks()

if sims4 is not None:

    @sims4.commands.Command("mp_lot_status", command_type=(sims4.commands.CommandType.Live))
    def mp_lot_status(_connection=None):
        out = sims4.commands.CheatOutput(_connection)
        out(
            "build share: {} | hooks: {} | lock: {}".format(
                share_mode(), _hooks_installed, lock_text(_lock) or "none"
            )
        )
