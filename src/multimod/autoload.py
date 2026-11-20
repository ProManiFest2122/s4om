# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\autoload.py
# Compiled at: 2026-10-29 22:00:37
# Size of source mod 2**32: 6701 bytes

"""Auto-load: the game opens straight into the session's save - no main menu clicks.

The launcher writes "autoload_slot" (the save slot number, e.g. 2 for Slot_00000002.save)
and "autoload_id" (a fresh id per Start) into mp_launcher_config.json before starting the
game. When the game reaches the main menu, this calls the game's own load-save function
(the one the Load Game screen calls) with that slot. Each autoload_id is used once, so going
back to the main menu later doesn't reload anything.

The function is native code inside TS4_x64.exe, called through ctypes (see native.py). Its
address is only trusted after checking its first bytes, so a game patch that moves it just
turns auto-load off (you load the save yourself) instead of crashing.
"""

import json, os, time

from multimod import network

KNOWN = {
    "1.128.90.1030": (5370409712, "405355565741564883ec4048c7442430feffffff448bf133ff893da161780240"),
    "1.129.39.1030": (5370410864, "405355565741564883ec4048c7442430feffffff448bf133ff893d216d780240"),
}

_done_ids = set()

_hooked = False

last = {}

_pending = None

PENDING_MINUTES = 20


def _state_path():
    return os.path.join(os.path.dirname(network.CMD_PATH), "mp_autoload_done.json")


def _game_version():
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(network.CMD_PATH)), "GameVersion.txt")
        import re

        with open(path, "rb") as f:
            m = re.search(b"\\d+\\.\\d+\\.\\d+\\.\\d+", f.read(200))
        if m:
            return m.group(0).decode()
        return ""
    except OSError:
        return ""


def _load_fn(version=None):
    """ctypes function for the game's load-save-slot, or (None, reason)."""
    version = version or _game_version()
    known = KNOWN.get(version)
    if not known:
        return (None, "game version {} not supported yet".format(version or "?"))
    (addr, sig) = known
    from multimod import native

    (ctypes, _how) = native.load_ctypes()
    got = ctypes.string_at(addr, len(sig) // 2).hex()
    if got != sig:
        return (None, "load function not where expected (bytes {})".format(got[:16]))
    return (ctypes.CFUNCTYPE(None, ctypes.c_int)(addr), "ok")


def _used(aid):
    if aid in _done_ids:
        return True
    try:
        with open((_state_path()), "r", encoding="utf-8") as f:
            return json.load(f).get("id") == aid
    except (OSError, ValueError):
        return False


def _mark(aid):
    _done_ids.add(aid)
    try:
        with open((_state_path()), "w", encoding="utf-8") as f:
            json.dump({"id": aid, "at": int(time.time())}, f)
    except OSError:
        pass


def arm(slot, minutes=PENDING_MINUTES):
    """Re-sync (joiner): open `slot` the next time the main menu shows, within `minutes`.
    The Load host's game button goes to the main menu; this makes the save open by itself."""
    global _pending
    _pending = {"slot": int(slot), "until": (time.time()) + (minutes * 60)}
    network._log("autoload: armed slot {} for the next main menu".format(slot))


def disarm():
    global _pending
    _pending = None


def maybe_autoload(cfg=None):
    """Called on reaching the main menu. Returns a short status string."""
    global _pending
    if _pending is not None:
        slot, until = _pending["slot"], _pending["until"]
        _pending = None
        if time.time() <= until:
            return _load(slot, "re-sync")
        network._log("autoload: re-sync load for slot {} expired".format(slot))
    cfg = cfg if cfg is not None else network._load_launcher_config() or {}
    slot, aid = cfg.get("autoload_slot"), str(cfg.get("autoload_id") or "")
    if slot in (None, "") or not aid:
        return "off"
    if _used(aid):
        return "already used"
    _mark(aid)
    return _load(slot, "id " + aid)


def _load(slot, label):
    global last
    fn, why = _load_fn()
    if fn is None:
        network._log("autoload: skipped - " + why)
        network._notify(
            "S4OM: couldn't open the save automatically ({}). Load it from Load Game.".format(why)
        )
        last = {"slot": slot, "ok": False, "why": why}
        return "skipped: " + why
    network._log("autoload: loading slot {} ({})".format(slot, label))
    fn(int(slot))
    last = {"slot": slot, "ok": True, "at": (time.time)()}
    _watch_arrival(slot)
    return "loading"


def _watch_arrival(slot):
    """Log whether the save opened in the household. 2026-10-22 the host's save opened on the world
    map instead, and nothing in the log said so - this line is what to look for in Send logs."""
    import threading

    def watch():
        for _ in range(60):
            time.sleep(5)
            if network._has_active_sim():
                network._log(
                    "autoload: slot {} opened in the household ({:.0f}s)".format(
                        slot, time.time() - last.get("at", 0)
                    )
                )
                return

        network._log(
            "autoload: slot {} - no household Sim 5 min after loading: it probably opened on the world map (Manage Worlds). Was the save last saved from there?".format(
                slot
            )
        )

    threading.Thread(target=watch, daemon=True).start()


def _on_main_menu(orig):
    def wrapper(*args, **kwargs):
        res = orig(*args, **kwargs)
        try:
            network._log("autoload: main menu -> {}".format(maybe_autoload()))
        except Exception as e:
            network._log("autoload error: {}".format(e))

        return res

    wrapper._s4om_patched = True
    return wrapper


def install():
    global _hooked
    if _hooked:
        return
    import services

    if not getattr(services.on_enter_main_menu, "_s4om_patched", False):
        services.on_enter_main_menu = _on_main_menu(services.on_enter_main_menu)
    _hooked = True
    network._log("autoload: hooked main menu")


try:
    install()
except Exception as _e:
    network._log("autoload: hook failed: {}".format(_e))

try:
    import sims4.commands

    @sims4.commands.Command("mp_autoload", command_type=(sims4.commands.CommandType.Live))
    def mp_autoload(_connection=None):
        (fn, why) = _load_fn()
        sims4.commands.CheatOutput(_connection)("autoload: function {} | last {}".format(why, last))

except Exception:
    pass
