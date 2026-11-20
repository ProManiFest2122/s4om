# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\start_hold.py
# Compiled at: 2026-10-25 18:57:25
# Size of source mod 2**32: 7152 bytes

"""Start hold: the host's game stays paused until everyone from the lobby has loaded in.

The launcher writes "expect_players" (names of the other players in the lobby when the host
pressed Start) and "expect_id" (the Start's id) into mp_launcher_config.json. When the host's
household loads, time is paused and stays paused - pressing play shows who we're still waiting
for. Once every expected player is in their lot, everyone gets "Everyone has joined" and the
host unpauses. The host can press play twice to start without the missing players, and after
10 minutes the hold lifts by itself.

Each expect_id is held once, so travelling or reloading later in the session doesn't hold again.
"""

import json, os, threading, time

from multimod import network

TIMEOUT = 600

SETTLE = 5.0

CONFIRM = 20.0

_holding = False

_since = 0.0

_expect = []

_hold_id = ""

_last_press = 0.0

_timer = None


def _state_path():
    return os.path.join(os.path.dirname(network.CMD_PATH), "mp_start_hold.json")


def _done(hid):
    try:
        with open((_state_path()), "r", encoding="utf-8") as f:
            return json.load(f).get("id") == hid
    except (OSError, ValueError):
        return False


def _mark_done(hid):
    try:
        with open((_state_path()), "w", encoding="utf-8") as f:
            json.dump({"id": hid, "at": int(time.time())}, f)
    except OSError:
        pass


def holding():
    global _holding
    return _holding


def _arrived():
    """Names of the other players whose Sims are in their lot."""
    from multimod import lobby

    return [m.get("name", "") for m in lobby._members.values() if not m.get("is_host") if m.get("in_lot")]


def _missing():
    global _expect
    arrived = _arrived()
    left = list(arrived)
    missing = []
    for name in _expect:
        if name in left:
            left.remove(name)
        else:
            missing.append(name)

    need = max(0, len(_expect) - len(arrived))
    if len(missing) >= need:
        return missing[:need]
    return missing + ["1 more player"] * (need - len(missing))


def _pause():
    try:
        from clock import ClockSpeedMode
        from multimod import clock_sync

        gcs = clock_sync._get_game_clock()
        if gcs is not None:
            if int(gcs.clock_speed) != 0:
                gcs.set_clock_speed(ClockSpeedMode.PAUSED)
    except Exception as e:
        network._log("start hold: pause error: {}".format(e))

    try:
        network._send_clock_broadcast(0)
    except Exception:
        pass


def _announce(text):
    network._notify(text)
    try:
        network._broadcast({"type": "start_hold", "text": text})
    except Exception as e:
        network._log("start hold: broadcast error: {}".format(e))


def on_host_in_lot():
    """Host's household finished loading. Start the hold if this Start expects other players."""
    global _expect
    global _hold_id
    global _holding
    global _last_press
    global _since
    global _timer
    if not network._is_host or _holding:
        return False
    cfg = network._load_launcher_config() or {}
    expect = [str(n) for n in cfg.get("expect_players") or [] if str(n).strip()]
    hid = str(cfg.get("expect_id") or "")
    if not expect or not hid or _done(hid):
        return False
    _expect, _hold_id = expect, hid
    if not _missing():
        _mark_done(hid)
        _announce("✅ Everyone has joined - unpause to play!")
        return False
    _holding, _since, _last_press = True, time.time(), 0.0
    _pause()
    names = ", ".join(_missing())
    network._log("start hold: waiting for {}".format(names))
    _announce("⏸ Waiting for everyone to load in: {}. Time stays paused until they're here.".format(names))
    gen = _since
    _timer = threading.Timer(TIMEOUT, lambda: _timeout(gen))
    _timer.daemon = True
    _timer.start()
    return True


def hold_for(names, why="to load in"):
    """Mid-session hold (drop-in): pause until these players' Sims are in the lot too. Expects
    everyone already here PLUS them - the count decides, so it can't release before they arrive."""
    global _expect
    global _hold_id
    global _holding
    global _last_press
    global _since
    global _timer
    if not network._is_host:
        return False
    names = [str(n) for n in names if str(n).strip()]
    if not names:
        return False
    if _holding:
        _expect = list(_expect) + names
    else:
        _expect = _arrived() + names
        _hold_id = "dropin-{}".format(int(time.time() * 1000))
        _holding, _since, _last_press = True, time.time(), 0.0
        gen = _since
        _timer = threading.Timer(TIMEOUT, lambda: _timeout(gen))
        _timer.daemon = True
        _timer.start()
    _pause()
    network._log("start hold: drop-in, waiting for {}".format(names))
    _announce("⏸ {} {} - time stays paused until they're here.".format(", ".join(names), why))
    return True


def check():
    """A player's lot status changed (lobby.on_in_lot). Lift the hold once everyone is in."""
    if _holding:
        if not _missing():
            _release("✅ Everyone has joined - unpause to play!")


def block_unpause(speed):
    """clock_sync asks before the host's game changes speed. True = keep it paused."""
    global _last_press
    if not _holding or int(speed) == 0:
        return False
    else:
        now = time.time()
        if now - _since < SETTLE:
            return True
        if _last_press and now - _last_press <= CONFIRM:
            _release(
                "▶ The host started without {}. They can still join when they load in.".format(
                    ", ".join(_missing()) or "everyone"
                )
            )
            return False
    _last_press = now
    network._notify(
        "Still waiting for {}. Press play again to start without them.".format(", ".join(_missing()))
    )
    return True


def _timeout(gen):
    if _holding:
        if gen == _since:
            _release("▶ Waited 10 minutes - starting without {}.".format(", ".join(_missing())))


def _release(text):
    global _holding
    global _timer
    if not _holding:
        return
    _holding = False
    if _timer is not None:
        _timer.cancel()
        _timer = None
    _mark_done(_hold_id)
    network._log("start hold: released - " + text)
    _announce(text)


def on_message(data):
    """Joiner: the host's hold notice."""
    text = str(data.get("text") or "")[:200]
    if text:
        network._notify(text)


try:
    import sims4.commands

    @sims4.commands.Command("mp_start", command_type=(sims4.commands.CommandType.Live))
    def mp_start(_connection=None):
        """Host: stop waiting and start without the players who haven't loaded in."""
        if _holding:
            _release("▶ The host started without {}.".format(", ".join(_missing()) or "everyone"))
        sims4.commands.CheatOutput(_connection)(
            "Start hold: {}".format("waiting for " + ", ".join(_missing()) if _holding else "not holding")
        )

except Exception:
    pass
