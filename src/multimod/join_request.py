# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\join_request.py
# Compiled at: 2026-10-25 23:03:52
# Size of source mod 2**32: 6032 bytes

r"""S4OM drop-in: someone asks to join a game that's already running (host's game).

The host's launcher sees the request in the room (relay/rooms.py) and hands it to the game:
mp_cmd "mp_join_request" {rid, name, sim, sim_name}. The game pauses and asks the host:

    "Ani wants to join as Jerry.  [Let in] [No]"

Let in -> Jerry moves into the household (living_world.py) FIRST, then the game saves - so the
save Ani loads already has Jerry in the household - and time stays paused until Ani's Sim is in
the lot (start_hold.hold_for). Bring your own Sim: the request carries "byos_file" (their
Library household, saved by the launcher) + "index" instead of one of the host's Sims; Let in
imports that Sim (byos.py) and then moves them in the same way. The answer goes back to the launcher in Mods\mp_join_answer.json
{rid, allow, at, save, sim}; the launcher tells the room (approve) and uploads the fresh save.
"""

import json, os, time

from multimod import network

_asked = set()


def _answer_path():
    return os.path.join(os.path.dirname(network.CMD_PATH), "mp_join_answer.json")


def write_answer(rid, allow, save=None, path=None, sim=None):
    path = path or _answer_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "rid": str(rid),
                "allow": bool(allow),
                "at": (time.time)(),
                "save": save,
                "sim": int(sim) if sim else None,
            },
            f,
        )
    os.replace(tmp, path)


def text_for(name, sim_name, warnings=None, own=False):
    """The question the host sees. Pure."""
    if own:
        t = "{} wants to join your game with their own Sim{}.".format(
            name or "Someone", " " + sim_name if sim_name else ""
        )
    else:
        t = "{} wants to join your game as {}.".format(name or "Someone", sim_name or "one of your Sims")
    if warnings:
        t += " " + " ".join(warnings)
    t += " Let them in? The game saves and stays paused until they've loaded in."
    return t


def _pause_now():
    try:
        from clock import ClockSpeedMode
        from multimod import clock_sync

        gcs = clock_sync._get_game_clock()
        if gcs is not None:
            gcs.set_clock_speed(ClockSpeedMode.PAUSED)
        network._send_clock_broadcast(0)
    except Exception as e:
        network._log("join request: pause error: {}".format(e))


def let_in(rid, name, sim, byos_file=None, index=0):
    """Host said yes: move their Sim in (importing it first if they brought their own), save,
    hold until they're here."""
    from multimod import living_world, resync, start_hold

    if byos_file:
        from multimod import byos

        inbox = os.path.realpath(os.path.join(os.path.dirname(network.CMD_PATH), "mp_byos"))
        if not os.path.realpath(byos_file).startswith(inbox + os.sep):
            byos_file = os.path.join(inbox, "missing")
        try:
            with open(byos_file, "rb") as f:
                ok, msg, sim = byos.bring(f.read(), int(index or 0), name, str(rid))
        except Exception as e:
            ok, msg = False, "their Sim couldn't be loaded ({})".format(e)

    else:
        ok, msg = living_world.move_in(int(sim), name, str(rid))
    if not ok:
        network._notify("S4OM: couldn't let {} in - {}.".format(name, msg))
        write_answer(rid, False)
        return False
    slot_id, slot_name = resync._current_slot()
    save = None
    if slot_id:
        try:
            resync._start_save(slot_id, slot_name)
            save = "Slot_{:08x}.save".format(slot_id)
        except Exception as e:
            network._log("join request: save failed: {}".format(e))

    start_hold.hold_for([name], "is loading in")
    write_answer(rid, True, save, sim=sim)
    network._log(
        "join request: let {} in as {} (save {}{})".format(name, sim, save, ", own Sim" if byos_file else "")
    )
    return True


def on_request(data):
    """mp_cmd from the host's launcher: show the question once per request."""
    rid = str(data.get("rid") or "")
    if not rid or rid in _asked or not network._is_host:
        return
    _asked.add(rid)
    name, sim, sim_name = (
        str(data.get("name") or "")[:32],
        data.get("sim"),
        str(data.get("sim_name") or "")[:40],
    )
    byos_file, index = data.get("byos_file"), data.get("index") or 0
    _pause_now()
    try:
        import services
        from ui.ui_dialog import UiDialog, UiDialogResponse, ButtonType
        from sims4.localization import LocalizationHelperTuning

        raw = LocalizationHelperTuning.get_raw_text
        client = services.client_manager().get_first_client()
        owner = client.active_sim_info if client is not None else None
        text = text_for(name, sim_name, (data.get("warnings") or []), own=(bool(byos_file)))
        dlg = UiDialog.TunableFactory().default(
            owner,
            text=(lambda *a, **k: raw(text)),
            title=(lambda *a, **k: raw("S4OM - someone wants to join")),
        )
        dlg.set_responses(
            (
                UiDialogResponse(
                    dialog_response_id=(ButtonType.DIALOG_RESPONSE_OK),
                    sort_order=0,
                    text=(lambda *a, **k: raw("Let in")),
                ),
                UiDialogResponse(
                    dialog_response_id=(ButtonType.DIALOG_RESPONSE_CANCEL),
                    sort_order=1,
                    text=(lambda *a, **k: raw("No")),
                ),
            )
        )

        def on_response(d):
            if getattr(d, "response", None) == ButtonType.DIALOG_RESPONSE_OK:
                let_in(rid, name, sim, byos_file, index)
            else:
                write_answer(rid, False)
                network._notify("S4OM: you didn't let {} in.".format(name))

        dlg.show_dialog(on_response=on_response)
    except Exception as e:
        network._log("join request dialog failed: {}".format(e))
        network._notify(
            "S4OM: {} wants to join as {} - answer in the S4OM lobby window.".format(
                name, sim_name or "their own Sim"
            )
        )
