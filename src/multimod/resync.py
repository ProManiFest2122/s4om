# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\resync.py
# Compiled at: 2026-10-27 19:10:50
# Size of source mod 2**32: 22769 bytes

"""S4OM Quick Save Re-sync - how build mode (and other big changes) reach everyone.

Walls, floors, rooms and terrain live in the game's C++ side and can't be copied over
the network piece by piece. So for changes like that the host's game is saved and the
save is sent to everyone:

  1. host saves to its current slot (the game's own save, persistence.override_save_slot)
  2. once the file is written, it's sent to every joiner (lobby's chunked transfer,
     SHA-256 checked) and stored in a dedicated slot (SYNC_SLOT) - nobody's own saves
     are touched
  3. joiners get a message: load the host's save from the main menu - S4OM reconnects
     them to the room automatically after it loads

Automatic triggers (host): leaving build/buy mode after changes, a new Sim joining the
household (birth, adoption). Manual: mp_resync, or the launcher's Re-sync button.
Turn the build-mode trigger off with launcher config "resync_after_build": false.
Build-mode trigger runs for the host with "build_share" "auto" (default) or "save"; with
"library" everyone shares builds through the Library instead (lot_share.py).
"""

import os, threading, time

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

SYNC_SLOT = "Slot_0000ff40.save"

SAVE_TIMEOUT = 120.0

COOLDOWN = 60.0

_phase = None

_started = 0.0

_file = None

_old_sig = None

_last_sig = None

_reason = ""

_display = ""

_last_done = 0.0

_hooks_installed = False


def _saves_dir():
    from multimod import lobby

    return lobby.SAVES_DIR


def _sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return


def _current_slot():
    """(slot_id, slot_name) of the save the host is playing, or (None, None)."""
    try:
        import services

        slot = services.get_persistence_service().get_save_slot_proto_buff()
        if slot is None:
            return (None, None)
        return (int(slot.slot_id), str(slot.slot_name or "S4OM game"))
    except Exception as e:
        network._log("resync: slot lookup failed: {}".format(e))
        return (None, None)


def _start_save(slot_id, slot_name):
    import services
    from services.persistence_service import SaveGameData

    ps = services.get_persistence_service()
    ps.save_using(
        (ps.save_game_gen),
        (SaveGameData(slot_id, slot_name, True, None)),
        send_save_message=True,
        check_cooldown=False,
    )


BACKUP_KEEP = 3


def backup_name(filename, now):
    """Pure (tested). 'Slot_00000005.save' -> 'Slot_00000005.save.s4om-20261026-203400.bak'."""
    return "{}.s4om-{}.bak".format(filename, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)))


def backups_to_delete(listing, filename, keep=BACKUP_KEEP):
    """Pure (tested). Oldest S4OM backups of this save beyond `keep` (names sort by time)."""
    mine = sorted((n for n in listing if n.startswith(filename + ".s4om-") and n.endswith(".bak")))
    if keep > 0:
        return mine[:-keep]
    return mine


def _backup_save(filename):
    """The re-sync makes the game save over the host's own file - keep the last few versions
    (a save is months of someone's play; this one lives at the host, never sent anywhere)."""
    try:
        d = _saves_dir()
        src = os.path.join(d, filename)
        if not os.path.exists(src):
            return
        import shutil

        shutil.copyfile(src, os.path.join(d, backup_name(filename, time.time())))
        for n in backups_to_delete(os.listdir(d), filename):
            os.remove(os.path.join(d, n))

    except Exception as e:
        network._log("resync: backup failed: {}".format(e))


def request(reason="sent their game to everyone", auto=False):
    """Host: save and send the save to everyone. Returns (ok, message)."""
    global _display
    global _file
    global _last_done
    global _last_sig
    global _old_sig
    global _phase
    global _reason
    global _started
    if not network._is_host:
        return (False, "Only the host can re-sync.")
    if not network._clients:
        return (False, "Nobody is connected yet.")
    if _phase is not None:
        return (False, "A re-sync is already running.")
    if auto and time.time() - _last_done < COOLDOWN:
        return (False, "Re-synced less than a minute ago.")
    (slot_id, name) = _current_slot()
    if not slot_id:
        return (False, "Save your game once (Esc > Save As) so S4OM knows which save to send.")
    _file = "Slot_{:08x}.save".format(slot_id)
    _backup_save(_file)
    _old_sig = _sig(os.path.join(_saves_dir(), _file))
    _last_sig = None
    _reason, _display = reason, name
    _phase, _started = "saving", time.time()
    network._broadcast({"type": "resync_start", "reason": reason, "name": name})
    network._notify("S4OM: saving and sending your game to everyone...")
    try:
        _start_save(slot_id, name)
    except Exception as e:
        _phase = None
        return (False, "Couldn't save the game: {}".format(e))

    network._log("resync: saving slot {} '{}' ({})".format(_file, name, reason))
    return (True, "Re-sync started")


def tick(now=None):
    """Main thread: wait for the save file to be written, then send it on a worker thread."""
    global _last_sig
    global _phase
    _install_hooks()
    _check_household()
    if _phase != "saving":
        return
    now = now or time.time()
    sig = _sig(os.path.join(_saves_dir(), _file))
    if sig is not None:
        if sig != _old_sig:
            if sig == _last_sig:
                _phase = "sending"
                threading.Thread(target=_send, daemon=True).start()
                return
    _last_sig = sig
    if now - _started > SAVE_TIMEOUT:
        _phase = None
        network._notify("S4OM: the save didn't finish in time - re-sync cancelled. Try again with mp_resync.")
        network._broadcast({"type": "resync_failed"})


def _send():
    global _last_done
    global _phase
    try:
        from multimod import lobby

        (ok, desc) = lobby.host_send_save_file(_file, save_as=SYNC_SLOT)
        if ok:
            from multimod import world_handoff

            network._broadcast(
                {"type": "resync_ready", "reason": _reason, "name": _display, "file": SYNC_SLOT,
                 "world": world_handoff.new_world()}
            )
            network._notify("S4OM: your game was sent - everyone is loading it now.")
        else:
            network._broadcast({"type": "resync_failed"})
            network._notify("S4OM: couldn't send the save: {}".format(desc))
    finally:
        _phase = None
        _last_done = time.time()


def _sync_slot_number():
    """Slot number of SYNC_SLOT ("Slot_0000ff40.save" -> 0xff40), what the game's load function takes."""
    return int(SYNC_SLOT[len("Slot_") : -len(".save")], 16)


def on_start(data):
    network._notify("S4OM: the host {} - receiving their game...".format(data.get("reason", "is re-syncing")))


def show_load_dialog(name, reason):
    """A game pop-up whose button goes straight to the main menu without saving - the game's
    own UI request TRANSITION_TO_MAIN_MENU_NO_SAVE (ui/ui_dialog.py UiDialogResponse.UiDialogUiRequest).
    Returns False if it couldn't be shown (then the notification below is all they get)."""
    try:
        import services
        from ui.ui_dialog import UiDialog, UiDialogResponse, ButtonType
        from sims4.localization import LocalizationHelperTuning

        raw = LocalizationHelperTuning.get_raw_text
        client = services.client_manager().get_first_client()
        owner = client.active_sim_info if client is not None else None
        text = "The host {}. Press Load host's game: you go to the main menu without saving and '{}' opens by itself - S4OM reconnects you automatically.".format(
            reason, name
        )
        dlg = UiDialog.TunableFactory().default(
            owner,
            text=(lambda *a, **k: raw(text)),
            title=(lambda *a, **k: raw("S4OM - the host updated the game")),
        )
        dlg.set_responses(
            (
                UiDialogResponse(
                    dialog_response_id=(ButtonType.DIALOG_RESPONSE_OK),
                    sort_order=0,
                    text=(lambda *a, **k: raw("Load host's game")),
                    ui_request=(UiDialogResponse.UiDialogUiRequest.TRANSITION_TO_MAIN_MENU_NO_SAVE),
                ),
                UiDialogResponse(
                    dialog_response_id=(ButtonType.DIALOG_RESPONSE_CANCEL),
                    sort_order=1,
                    text=(lambda *a, **k: raw("Later")),
                ),
            )
        )
        from multimod import autoload

        autoload.arm(_sync_slot_number())

        def on_response(d):
            if getattr(d, "response", None) == ButtonType.DIALOG_RESPONSE_CANCEL:
                autoload.disarm()
                network._log("resync: Later - auto-load cancelled")

        dlg.show_dialog(on_response=on_response)
        return True
    except Exception as e:
        network._log("resync: load dialog failed: {}".format(e))
        return False


def on_ready(data):
    name = data.get("name") or "the host's save"
    try:
        from multimod import world_handoff

        if world_handoff.on_world(data, _sync_slot_number()):
            network._log("resync: ready - opening '{}' from the main menu".format(name))
            return
    except Exception as e:
        network._log("resync: hand-off error: {}".format(e))
    show_load_dialog(name, data.get("reason", "re-synced"))
    network._notify(
        "S4OM: the host {}. Press Load host's game (or Esc > Main Menu, no need to save) and '{}' opens by itself. You'll rejoin the room automatically.".format(
            data.get("reason", "re-synced"), name
        )
    )
    network._log("resync: ready - load '{}' ({})".format(name, data.get("file")))


def on_failed(data):
    network._notify("S4OM: the host's re-sync didn't go through - keep playing, they can try again.")


def _cfg_on(key, default=True):
    try:
        v = (network._load_launcher_config() or {}).get(key, default)
        return v is not False
    except Exception:
        return default


_build_snapshot = None


def _pt(v):
    try:
        return (round(float(v.x), 1), round(float(v.z), 1))
    except Exception:
        return tuple((round(float(c), 1) for c in v))


def _structure(objs_gen=None):
    """What only a re-sync can carry (the game keeps it in C++): the lot's value without
    furniture (walls, floors, paint, roofs, stairs, pools all cost money), the room outlines
    (walls), and objects that sit in walls/fences (doors, windows, gates). None = unreadable."""
    out = {"value": None, "rooms": None, "wall_objs": None}
    try:
        import services, build_buy

        out["value"] = int(build_buy.get_lot_value(services.current_zone_id(), False)[0])
    except Exception:
        pass

    try:
        import services, build_buy

        try:
            plex_id = services.get_plex_service().get_active_zone_plex_id() or 0
        except Exception:
            plex_id = 0

        rooms = []
        for polys, level in build_buy.get_all_block_polygons(plex_id).values():
            rooms.append((int(level), tuple(sorted((tuple((_pt(v) for v in p)) for p in polys)))))

        out["rooms"] = tuple(sorted(rooms))
    except Exception:
        pass

    try:
        import build_buy
        from multimod import buy_sync

        wall = build_buy.PlacementFlags.WALL_GRAPH_PLACEMENT
        objs = []
        for o in objs_gen if objs_gen is not None else buy_sync._iter_lot_objects():
            if build_buy.get_object_placement_flags(o.definition.id) & wall:
                objs.append((int(o.id),) + tuple((round(v, 2) for v in (buy_sync._obj_state(o) or ())[:3])))

        out["wall_objs"] = tuple(sorted(objs))
    except Exception:
        pass

    return out


def _lot_snapshot():
    """(household funds, objects on the lot with where they are, structure) - what building changes."""
    (funds, objs) = (None, ())
    try:
        import services

        hh = services.active_household()
        funds = int(hh.funds.money) if hh is not None else None
    except Exception:
        pass

    try:
        from multimod import buy_sync

        objs = tuple(
            sorted(
                (
                    (int(o.id),) + tuple((round(v, 2) for v in (buy_sync._obj_state(o) or ())[:4]))
                    for o in buy_sync._iter_lot_objects()
                )
            )
        )
    except Exception:
        pass

    return (funds, objs, _structure())


def lot_changed(before, after):
    """Pure (tested). -> "structure" (walls/floors/paint... only a re-sync carries them),
    "objects" (bought/moved/sold furniture - buy_sync + money_sync already sent it live) or None.
    No snapshot from entering (mod loaded mid-build) -> assume structure."""
    if before is None:
        return "structure"
    objects = before[0] != after[0] or before[1] != after[1]
    sb = before[2] if len(before) > 2 else {}
    sa = after[2] if len(after) > 2 else {}
    readable = [k for k in ("value", "rooms", "wall_objs") if sb.get(k) is not None if sa.get(k) is not None]
    if not readable:
        if objects:
            return "structure"
        return
    if any((sb[k] != sa[k] for k in readable)):
        return "structure"
    if objects:
        return "objects"


def _on_build_buy_enter(*args, **kwargs):
    global _build_snapshot
    try:
        _build_snapshot = _lot_snapshot()
    except Exception:
        _build_snapshot = None


def _on_build_buy_exit(*args, **kwargs):
    if not (network._is_host and network._clients and _cfg_on("resync_after_build")):
        network._log("resync: build exit - not hosting with players / option off")
        return
    from multimod import lot_share

    if lot_share.share_mode() == "library":
        network._log("resync: build exit - Library share mode")
        return
    after = _lot_snapshot()
    what = lot_changed(_build_snapshot, after)
    if what is None:
        network._log("resync: build exit - nothing changed")
        return
    if what == "objects":
        network._log("resync: build exit - only furniture changed, already synced live (no reload)")
        return
    try:
        sb = _build_snapshot[2] if _build_snapshot and (len(_build_snapshot) > 2) else ({})
        sa = after[2]
        network._log(
            "resync: build exit - the lot's structure changed ({})".format(
                ", ".join((k for k in ("value", "rooms", "wall_objs") if sb.get(k) != sa.get(k))) or "unknown"
            )
        )
    except Exception:
        network._log("resync: build exit - the lot changed")

    if _cfg_on("live_build"):
        try:
            from multimod import lot_live

            if lot_live.send():
                network._log("resync: build exit -> sent live")
                return
        except Exception as e:
            network._log("resync: live build failed to start: {}".format(e))

    (ok, msg) = request("changed the lot in build mode", auto=True)
    network._log("resync: build mode exit -> {}".format(msg))


def _install_hooks():
    global _hooks_installed
    if _hooks_installed:
        return
    try:
        import build_buy

        build_buy.register_build_buy_exit_callback(_on_build_buy_exit)
        build_buy.register_build_buy_enter_callback(_on_build_buy_enter)
        _hooks_installed = True
        network._log("resync: build mode hook installed")
    except Exception:
        pass


_household_ids = None

_household_dead = None

_expected = set()


def expect(sim_id):
    """living_world.move_in: this Sim joining the household is already on everyone's PC
    (world_move / byos_import), so it is NOT a reason to re-sync. Before this, a player
    reconnecting got their Sim moved back in -> 'new Sim' -> a second full load."""
    _expected.add(int(sim_id))


def needs_resync(old_ids, new_ids, expected):
    """Pure (tested): Sims that appeared in the household that no other game knows about."""
    if old_ids is None:
        return set()
    return set(new_ids) - set(old_ids) - set(expected)


_expected_out = set()


def expect_out(sim_id):
    """living_world.move_out: this Sim leaving the household is already on everyone's PC (world_move)."""
    _expected_out.add(int(sim_id))


def lost_sims(old_ids, new_ids, expected_out):
    """Pure (tested): Sims that left the household on the host only. A death takes the Sim out of the
    household (urn / ghost in a hidden household) and the old check only looked for NEW Sims, so a
    death never reached the joiners unless the dead Sim happened to still be listed."""
    if old_ids is None:
        return set()
    return set(old_ids) - set(new_ids) - set(expected_out)


def _check_household():
    """A new Sim in the household (newborn, adoption) only exists on the host's PC -> re-sync."""
    global _household_dead
    global _household_ids
    if not network._is_host:
        return
    try:
        import services

        hh = services.active_household()
        ids = {int(si.sim_id) for si in hh.sim_info_gen()} if hh is not None else None
        dead = {int(si.sim_id) for si in hh.sim_info_gen() if si.is_dead} if hh is not None else set()
    except Exception:
        return
    if ids is None:
        return
    died = dead - _household_dead if _household_dead is not None else set()
    _household_dead = dead
    if died:
        if network._clients:
            (ok, msg) = request("lost a Sim", auto=True)
            network._log("resync: Sim(s) {} died: {}".format(sorted(died), msg))
    gone = lost_sims(_household_ids, ids, _expected_out)
    if gone and not died and network._clients:
        (ok, msg) = request("lost a Sim from the household", auto=True)
        network._log("resync: Sim(s) {} left the household (death or move-out): {}".format(sorted(gone), msg))
    _expected_out.difference_update(gone | set(_household_ids or ()) - ids)
    new = needs_resync(_household_ids, ids, _expected)
    if new:
        if network._clients:
            (ok, msg) = request("has a new Sim in the household", auto=True)
            network._log("resync: new Sim(s) {} in the household: {}".format(sorted(new), msg))
    _expected.difference_update(ids - set(_household_ids or ()))
    _household_ids = ids


if sims4 is not None:

    @sims4.commands.Command("mp_resync", command_type=(sims4.commands.CommandType.Live))
    def mp_resync(_connection=None):
        (ok, msg) = request()
        sims4.commands.CheatOutput(_connection)(msg)

    @sims4.commands.Command("mp_life_test", command_type=(sims4.commands.CommandType.Live))
    def mp_life_test(what="show", _connection=None):
        """Testing: 'baby' = a newborn joins the household, 'death' = a household Sim (not the played
        one) dies off the lot the way the game does it (leaves the household). The host's re-sync
        check (_check_household) must notice both."""
        out = sims4.commands.CheatOutput(_connection)
        import services

        hh = services.active_household()
        client = services.client_manager().get_first_client()
        me = client.active_sim_info if client is not None else None
        if what == "baby":
            from sims.sim_spawner import SimSpawner
            from sims.sim_info_types import Age, Gender

            try:
                from sims.sim_spawner import SimCreator
            except ImportError:
                from sims.sim_creator import SimCreator

            (infos, _hh) = SimSpawner.create_sim_infos(
                (SimCreator(age=(Age.BABY), gender=(Gender.FEMALE)),),
                household=hh,
                creation_source="S4OM life test",
            )
            network._log("life test: a baby {} joined the household".format(infos[0].sim_id))
            out("baby {}".format(infos[0].sim_id))
        elif what == "death":
            from interactions.utils.death_enums import DeathType

            victims = [si for si in hh.sim_info_gen() if me is None or si.sim_id != me.sim_id]
            if not victims:
                out("no one but the played Sim")
                return
            si = victims[0]
            si.death_tracker.set_death_type(
                (DeathType.get_random_death_type()), is_off_lot_death=True, process_inventory=False
            )
            network._log("life test: {} {} died".format(si.first_name, si.sim_id))
            out("died {}".format(si.sim_id))
        else:
            out("usage: mp_life_test baby|death")

    @sims4.commands.Command("mp_build_probe", command_type=(sims4.commands.CommandType.Live))
    def mp_build_probe(what="show", _connection=None):
        """Testing: 'mark' notes the lot like entering build mode, 'check' compares like leaving it."""
        global _build_snapshot
        snap = _lot_snapshot()
        s = snap[2]
        network._log(
            "build probe {}: value={} rooms={} wall_objs={} objs={} funds={}".format(
                what,
                s["value"],
                None if s["rooms"] is None else len(s["rooms"]),
                None if s["wall_objs"] is None else len(s["wall_objs"]),
                len(snap[1]),
                snap[0],
            )
        )
        if what == "mark":
            _build_snapshot = snap
        elif what == "check":
            network._log("build probe: lot_changed -> {}".format(lot_changed(_build_snapshot, snap)))
