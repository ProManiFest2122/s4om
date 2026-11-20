"""S4OM session watch: the game follows the launcher without mp_apply, and says how it is doing.

A small background thread (it has to work before any session exists, so it can't hang off the
session's tick):

  - every few seconds it looks at Mods/mp_launcher_config.json. A session set there that the
    game isn't in yet is applied straight away (network._apply_launcher_config - the same call
    the 30-second start-up timer and mp_apply make). So Host / Join in the launcher works
    whether the game was started before or after the click.
    A game that is already in a session is left alone: switching rooms needs a restart, and the
    status file says so instead of the launcher's click silently doing nothing.

  - it writes Mods/mp_status.json: the room, the state (hosting / joining / waiting for the
    host / connected) and who is in the room. The launcher shows it. A game that is running
    without a fresh status file is a game where the mod didn't load - which was impossible to
    tell apart from "nothing happens" before.
"""

import json, os, threading, time

from multimod import network

POLL = 3.0
FIRST_LOOK = 40.0       # after the mod loads; the start-up timer applies the config at 30 s
STATUS_FILE = "mp_status.json"
ERROR_SHOWN_FOR = 90.0  # seconds a relay refusal stays in the status file

_started = False
_seen = None            # the session signature we last acted on (or told the player about)


def signature(cfg):
    """Pure (tested). What identifies a session in the launcher config, or None for "no session".
    "at" is the launcher's click time: clicking Host / Join again is a new request even when
    the room is the same (e.g. after mp_leave)."""
    cfg = cfg or {}
    mode = cfg.get("mode")
    if mode not in ("host", "join"):
        return None
    return (mode, str(cfg.get("room_code") or "").upper(), str(cfg.get("relay") or ""),
            str(cfg.get("host") or ""), str(cfg.get("at") or ""))


def decide(wanted, seen, applied, busy):
    """Pure (tested). wanted: signature in the config file now. seen: the last one this watch
    acted on. applied: the last one the game tried to start. busy: hosting / joined / waiting?
      'apply'   - start the session from the config
      'restart' - the config names another session than the running one
      None      - nothing to do (no session asked for, or this request was handled already)"""
    if wanted is None or wanted == seen or wanted == applied:
        return None
    if busy:
        # clicking Host / Join again for the room the game is already in is not a switch
        same_room = applied is not None and wanted[:4] == applied[:4]
        return None if same_room else "restart"
    return "apply"


def session_state():
    """One word for the status file."""
    try:
        thread_alive = bool(network._network_thread and network._network_thread.is_alive())
        if network._is_host and thread_alive:
            return "hosting"
        if network._client_socket is not None:
            return "connected"
        try:
            from multimod import lobby

            if lobby._reconnect_in_progress:
                return "reconnecting"
        except Exception:
            pass
        if network._room_wait is not None:
            return "waiting for the host"
        if thread_alive or network._client_connecting:
            return "joining"
    except Exception:
        pass
    return "idle"


def status():
    out = {"at": int(time.time()), "state": session_state(), "room": "", "host": bool(network._is_host),
           "players": [], "note": "", "applied": network._applied_session, "version": network.MOD_BUILD}
    try:
        from multimod import lobby

        out["players"] = [str(m.get("name")) for m in lobby._members.values() if m.get("online", True)]
        out["room"] = lobby.ROOM_CODE or ""
    except Exception:
        pass
    if not out["room"]:
        if network._room_wait is not None:
            out["room"] = network._room_wait[0]
        elif network._relay_join is not None:
            out["room"] = network._relay_join[3]
    try:
        from multimod import world_handoff

        out["note"] = world_handoff.status
        out["handoff"] = world_handoff.handoff_status()
    except Exception:
        pass
    err = network._last_relay_error
    if err is not None and time.time() - err[1] < ERROR_SHOWN_FOR and out["state"] not in ("hosting", "connected"):
        out["error"] = err[0]      # e.g. "ERR ROOM_FULL": why joining / hosting isn't happening
    return out


def write_status(path=None, data=None):
    path = path or os.path.join(os.path.dirname(network.LAUNCHER_CONFIG_PATH), STATUS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data if data is not None else status(), f)
    os.replace(tmp, path)


def check_config():
    """Apply a session the launcher set while the game was already running. -> what was done"""
    global _seen
    wanted = signature(network._load_launcher_config())
    action = decide(wanted, _seen, network._applied_session, session_state() != "idle")
    _seen = wanted
    if action == "restart":
        network._log("watch: the launcher now asks for {} but this game is in another session - restart the game to switch".format(wanted[:2]))
        network._notify("S4OM: to switch to room {} close and start The Sims 4 again.".format(wanted[1] or "?"))
    elif action == "apply":
        (ok, desc) = network._apply_launcher_config()
        network._log("watch: launcher config changed -> {}".format(desc))
    return action


def _loop():
    loaded = time.time()
    while True:
        if time.time() - loaded >= FIRST_LOOK:      # the start-up timer has had its turn
            try:
                check_config()
            except Exception as e:
                network._log("watch: config check failed: {}".format(e))
        try:
            write_status()                          # from the first second: "the mod is loaded"
        except Exception:
            pass
        time.sleep(POLL)


def start():
    global _started
    if _started:
        return
    _started = True
    try:
        write_status()      # right away: "the mod is loaded" is the first thing a launcher wants to know
    except Exception:
        pass
    threading.Thread(target=_loop, daemon=True).start()
