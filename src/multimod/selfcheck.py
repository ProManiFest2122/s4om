# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\selfcheck.py
# Compiled at: 2026-10-11 23:21:09
# Size of source mod 2**32: 6124 bytes

r"""S4OM self-check: on load, confirm every game function S4OM relies on exists in THIS
version of The Sims 4. A patch that renames something shows up here (mp_debug.log,
Mods\mp_selfcheck.json, and in-game with mp_diag) instead of as a silent half-broken sync.

  mp_diag   connection, players, hooks, self-check results, recent errors
"""

import json, os, time

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

CHECKS = [
    ("time control", "clock", "GameClock.set_clock_speed"),
    ("unpaused menus", "clock", "GameClock.push_speed"),
    ("unpaused menus", "clock", "SpeedRequest"),
    ("pop-ups", "ui.ui_dialog_service", "UiDialogService.dialog_show"),
    ("pop-ups", "ui.ui_dialog_service", "UiDialogService.dialog_respond"),
    ("pop-ups", "protocolbuffers.Dialog_pb2", "UiDialogMessage"),
    ("pop-ups", "distributor.system", "Distributor.instance"),
    ("calendar", "drama_scheduler.drama_scheduler", "DramaScheduleService.score_and_schedule_nodes_gen"),
    ("calendar", "drama_scheduler.drama_scheduler", "DramaScheduleService.schedule_node"),
    ("calendar", "drama_scheduler.drama_scheduler", "DramaScheduleService.cancel_scheduled_node"),
    ("social events", "situations.situation_manager", "SituationManager.create_situation"),
    ("social events", "situations.situation_guest_list", "SituationGuestList"),
    ("small businesses", "business.business_manager", "BusinessManager.set_open"),
    ("small businesses", "business.business_manager", "BusinessManager.set_markup_multiplier"),
    ("action plans", "civic_policies.base_civic_policy_provider", "BaseCivicPolicyProvider.vote_by_instance"),
    ("action plans", "civic_policies.base_civic_policy_provider", "BaseCivicPolicyProvider.enact"),
    ("build mode re-sync", "build_buy", "register_build_buy_exit_callback"),
    ("build mode re-sync", "services.persistence_service", "SaveGameData"),
    (
        "re-sync load button",
        "ui.ui_dialog",
        "UiDialogResponse.UiDialogUiRequest.TRANSITION_TO_MAIN_MENU_NO_SAVE",
    ),
    ("re-sync load button", "sims4.localization", "LocalizationHelperTuning.get_raw_text"),
    ("buy mode", "objects.system", "create_object"),
    ("buy mode", "sims4.math", "angle_to_yaw_quaternion"),
    ("buy mode", "sims4.math", "yaw_quaternion_to_angle"),
    ("buy mode", "routing", "SurfaceIdentifier"),
    ("careers", "careers.career_tracker", "CareerTracker.add_career"),
    ("careers", "careers.career_base", "CareerBase.promote"),
    ("traits", "traits.trait_tracker", "TraitTracker.equipped_traits"),
    ("travel", "sims.sim_info", "SimInfo.send_travel_switch_to_zone_op"),
    ("travel", "server.client", "Client.set_active_sim_by_id"),
    ("no joiner autosave", "services.autosave_service", "AutosaveService._request_autosave"),
    ("no joiner autosave", "gameplay_options.misc_options_service", "MiscOptionsService.autosave_on_travel"),
]

_results = None


def _resolve(module, dotted):
    mod = __import__(module, fromlist=["_"])
    obj = mod
    for part in dotted.split("."):
        obj = getattr(obj, part)

    return obj


def run():
    """Returns {"ok": n, "missing": [(feature, "module:attr", error)]}."""
    global _results
    missing, ok = [], 0
    for feature, module, dotted in CHECKS:
        try:
            _resolve(module, dotted)
            ok += 1
        except Exception as e:
            missing.append((feature, "{}:{}".format(module, dotted), type(e).__name__))

    _results = {"ok": ok, "total": len(CHECKS), "missing": missing, "ts": (time.time)()}
    if missing:
        network._log(
            "self-check: {}/{} game APIs found - MISSING: {}".format(
                ok, len(CHECKS), "; ".join(("{} ({})".format(m[1], m[0]) for m in missing))
            )
        )
    else:
        network._log("self-check: all {} game APIs found".format(len(CHECKS)))
    try:
        path = os.path.join(os.path.dirname(network.LAUNCHER_CONFIG_PATH), "mp_selfcheck.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_results, f, indent=1)
    except Exception:
        pass

    return _results


def report():
    r = _results or run()
    lines = ["S4OM self-check: {}/{} game features OK".format(r["ok"], r["total"])]
    for feature, where, err in r["missing"]:
        lines.append("  NOT AVAILABLE: {} ({})".format(feature, where))

    if network._is_host:
        lines.append("Hosting - {} player(s) connected".format(len(network._clients)))
    else:
        lines.append("Connected to host" if network._client_socket is not None else "Not connected")
    try:
        rtt = network.get_rtt_ms()
        if rtt is not None:
            lines.append("Ping: {:.0f} ms".format(rtt))
    except Exception:
        pass

    hooks = []
    for name in ("menus", "dialog_router", "world_sync"):
        try:
            hooks.append(
                "{} {}".format(
                    name, "on" if __import__(("multimod." + name), fromlist=["_"])._installed else "OFF"
                )
            )
        except Exception:
            hooks.append(name + " ?")

    lines.append("Hooks: " + ", ".join(hooks))
    try:
        from multimod import desync_repair

        lines.append(desync_repair.summary())
    except Exception:
        pass

    return lines


run()

if sims4 is not None:

    @sims4.commands.Command("mp_diag", command_type=(sims4.commands.CommandType.Live))
    def mp_diag(_connection=None):
        out = sims4.commands.CheatOutput(_connection)
        for line in report():
            out(line)

    @sims4.commands.Command("mp_client", command_type=(sims4.commands.CommandType.Live))
    def mp_client(*args, _connection=None):
        """Send a command to the game engine's own console (for finding the load-save / place-lot commands)."""
        out = sims4.commands.CheatOutput(_connection)
        cmd = " ".join((str(a) for a in args)).strip()
        if not cmd:
            out("usage: mp_client <engine command>")
            return
        sims4.commands.client_cheat(cmd, _connection)
        network._log("mp_client: sent '{}'".format(cmd))
        out("sent to engine: " + cmd)
