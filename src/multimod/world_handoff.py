"""S4OM world hand-off: a player who joins gets the host's game without anyone typing a command.

Everybody plays in ONE save - the host's. Before this module a joiner connected while still in
their own save, and it took the host typing mp_resync (and knowing they had to) to fix that.

Now the joiner tells the host which game it is in - in its hello, and again ("world_report")
each time a household finishes loading:

    world   id of the last copy of the host's game it received ("" if none)
    hh      the household it has loaded (0 if none)
    menu    it is sitting at the main menu

and the host compares with its own:

    never sent a copy, or the joiner has another one   -> send
    same copy, joiner at the main menu                 -> send (it is opened for them)
    same copy, nothing loaded yet (loading screen)     -> wait for the joiner's next report
    same copy, same household                          -> in the host's game, nothing to do
    same copy, another household                       -> send (got it but pressed "Later")

"Send" is resync.request: the same Quick Save Re-sync that build mode uses - the host's game is
saved to its own slot (the last few versions are kept as backups) and sent to everyone.

On the joiner, when the host's game arrives:
  - in a household: the existing pop-up with one button, "Load host's game" (resync.on_ready)
  - at the main menu: the save is opened straight away (autoload._load), nothing to click

If the host can't send yet (still at the main menu, or the game was never saved) the request
waits and is tried again; the host is told what is missing.
"""

import os, time

from multimod import network

SETTLE = 6.0            # seconds after a hello before the host starts saving (let the handshake finish)
RETRY = 10.0            # seconds between attempts while someone is waiting
HINT_EVERY = 90.0       # seconds between reminders to a host who has never saved the game

REASON = "is sharing their game with a new player"

world_id = ""           # host: id of the copy last sent. joiner: id of the copy last received
status = ""             # one short line for the log and for launchers that only show text
phase = ""              # where the hand-off stands, as a code a launcher can translate:
                        #   host:   need_household, need_save, sent   (saving / sending come from resync)
                        #   joiner: receiving, arrived, opening, open_manually   (in_host_game is derived)
_arrived_at = 0.0       # joiner: when the current copy arrived

_waiting = {}           # host: pid -> (player name, time of hello) - players who need the game
_sent_to = {}           # host: player name -> copy id they were sent while connected
_last_try = 0.0
_last_hint = 0.0


def household_id():
    """Id of the household loaded right now, 0 when none (main menu, loading)."""
    try:
        import services

        hh = services.active_household()
        return int(hh.id) if hh is not None else 0
    except Exception:
        return 0


def at_main_menu():
    """True only at the main menu, where the game's services aren't running (no zone, no
    client). False on loading screens, in Create a Sim and in Manage Worlds."""
    try:
        import game_services

        return game_services.service_manager is None
    except Exception:
        return False


def hello_fields():
    """Joiner: what to tell the host about the game we're in."""
    return {"world": world_id, "hh": household_id(), "menu": at_main_menu()}


def report():
    """Joiner: a household just finished loading - tell the host which one. Sent from the lot
    status check, so the host never has to guess what a loading screen will end in."""
    if network._is_host or network._client_socket is None:
        return False
    try:
        network._send_json(network._client_socket, dict(hello_fields(), type="world_report"))
        return True
    except Exception as e:
        network._log("hand-off: report failed: {}".format(e))
        return False


def needs_world(host_world, host_hh, world, hh, menu=False, sent_before=None):
    """Pure (tested). Does this player need the host's game sent?
    True: yes.  False: no, they are in it.  None: can't tell yet (nothing loaded) - wait for their report.

    world is None for a mod too old to report anything: such a player is sent each copy once
    (sent_before), never in a loop."""
    if not host_world:
        return True
    if world is None:
        return sent_before != host_world
    if world != host_world or menu:
        return True
    if not hh:
        return None
    if not host_hh:
        return False            # the host has nothing loaded to compare with; same copy is good enough
    return int(hh) != int(host_hh)


def on_player(pid, name, info):
    """Host: a player said hello, or reported the household they loaded. Queue them if they
    need the game. -> True / False / None (see needs_world)"""
    if not network._is_host:
        return False
    world = info.get("world") if "world" in info else None
    need = needs_world(world_id, household_id(), world, info.get("hh"), bool(info.get("menu")), _sent_to.get(str(name)))
    if need:
        _waiting[pid] = (str(name), time.time())
        network._log("hand-off: {} needs the host's game (copy {!r}, household {}, menu {})".format(
            name, world, info.get("hh"), bool(info.get("menu"))))
    elif need is None:
        network._log("hand-off: {} has the current copy and is still loading - waiting for their report".format(name))
    else:
        _waiting.pop(pid, None)
        network._log("hand-off: {} is already in the host's game".format(name))
    return need


def on_report(pid, info):
    """Host: 'world_report' from a joiner."""
    name = pid
    try:
        from multimod import lobby

        name = (lobby._members.get(pid) or {}).get("name", pid)
    except Exception:
        pass
    return on_player(pid, name, info)


def new_world():
    """Host: a copy of the game was just sent to everyone connected. -> its id"""
    global phase, status, world_id
    world_id = os.urandom(6).hex()
    phase = "sent"
    try:
        from multimod import lobby

        for m in list(lobby._members.values()):
            if not m.get("is_host"):
                _sent_to[str(m.get("name"))] = world_id
    except Exception:
        pass
    _waiting.clear()
    status = "game sent to the players"
    return world_id


def tick(now=None):
    """Host, main thread: send the game to whoever is waiting, as soon as that is possible."""
    global _last_hint, _last_try, phase, status
    if not network._is_host or not _waiting:
        return
    now = now or time.time()
    for pid in list(_waiting):
        if pid not in network._clients:
            del _waiting[pid]
    if not _waiting:
        return
    if now - max(since for (_name, since) in _waiting.values()) < SETTLE or now - _last_try < RETRY:
        return
    _last_try = now
    names = ", ".join(sorted(name for (name, _since) in _waiting.values()))
    if not network._has_active_sim():
        status = "waiting: load your household so {} can get your game".format(names)
        phase = "need_household"
        return
    from multimod import resync

    (ok, msg) = resync.request(REASON, auto=True)
    if ok:
        phase = ""
        status = "saving and sending the game to {}".format(names)
        network._log("hand-off: sending the game to {}".format(names))
        return
    network._log("hand-off: not yet ({})".format(msg))
    if not resync._current_slot()[0]:
        phase = "need_save"
        status = "waiting: save your game once (Esc > Save As) so {} can get it".format(names)
        if now - _last_hint > HINT_EVERY:
            _last_hint = now
            network._notify(
                "S4OM: {} joined but can't get your game yet - save it once (Esc > Save As) and it is sent automatically.".format(names)
            )


def on_world(data, slot):
    """Joiner: the host's game has arrived (resync_ready). Remember which copy it is. At the
    main menu open it now - there is no household to show the "Load host's game" pop-up in.
    -> True if the save is being opened (the caller then skips the pop-up)."""
    global _arrived_at, phase, status, world_id
    world_id = str(data.get("world") or "")
    _arrived_at = time.time()
    phase = "arrived"
    status = "the host's game arrived"
    if not at_main_menu():
        return False
    try:
        from multimod import autoload

        res = autoload._load(slot, "joined from the main menu")
        network._log("hand-off: at the main menu, opening the host's game -> {}".format(res))
        if res == "loading":
            phase = "opening"
            status = "opening the host's game"
            return True
        phase = "open_manually"
        status = "the host's game arrived - open it from Load Game ({})".format(res)
    except Exception as e:
        network._log("hand-off: couldn't open the host's game from the main menu: {}".format(e))
    return False


def receiving():
    """Joiner: the host started saving its game for us (resync_start)."""
    global phase, status
    phase = "receiving"
    status = "the host is saving and sending their game"


def _receive_progress():
    """Joiner: percent of the host's save received so far, None when nothing is on its way."""
    try:
        from multimod import lobby, resync

        cache = lobby._recv_save_cache.get(resync.SYNC_SLOT)
        if not cache or not cache.get("total"):
            return None
        return int(100 * len(cache.get("chunks") or ()) / cache["total"])
    except Exception:
        return None


def _opened_since_arrival():
    """Joiner: has the sync slot been opened (by autoload) since this copy arrived?"""
    try:
        from multimod import autoload, resync

        last = autoload.last
        return bool(last.get("ok")) and last.get("slot") == resync._sync_slot_number() and last.get("at", 0) >= _arrived_at
    except Exception:
        return False


def handoff_status():
    """{phase, names, progress} for Mods/mp_status.json - what the launcher's "host's game" row shows.
    Host phases:   need_household, need_save, saving, sending, sent, "" (nobody waiting)
    Joiner phases: receiving (+ progress %), arrived (pop-up waiting for a click), opening,
                   in_host_game, open_manually, "" (nothing happened yet)"""
    names = sorted(name for (name, _since) in _waiting.values())
    current, progress = phase, None
    if network._is_host:
        try:
            from multimod import resync

            if resync._phase in ("saving", "sending"):
                current = resync._phase
            elif not _waiting and current in ("need_household", "need_save"):
                current = ""
        except Exception:
            pass
    else:
        if current == "receiving":
            progress = _receive_progress()
        if current in ("arrived", "opening") and (current == "opening" or _opened_since_arrival()):
            current = "in_host_game" if household_id() else "opening"
    return {"phase": current, "names": names, "progress": progress}
