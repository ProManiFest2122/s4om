# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\control.py
# Compiled at: 2026-10-27 19:11:35
# Size of source mod 2**32: 10188 bytes

"""S4OM Sim control: the host decides which household Sims each player may control.

Set in the launcher's lobby ("Who controls which Sim"), e.g. Kam -> Tom + Jerry, Ani -> Jerry.
The host's launcher hands the rules to the host's game (mp_cmd "mp_control"); the host's game
sends them to everyone ("control_rules"). Each player's game then:
  - keeps only their allowed Sims in the Sim portraits (client.selectable_sims) - the others
    can't be clicked, selected or given orders on that PC;
  - refuses a switch to a Sim that isn't theirs (client.set_active_sim_info), even if the game
    tries on its own after a travel or a birth.
A player with no rule (or a rule naming no Sim in the household) controls everyone - nobody
gets locked out of the game by a stale setting.
"""

import time

from multimod import network

REAPPLY_EVERY = 5.0

_rules = {}

_last_apply = 0.0

_last_send = 0.0

_patched = False

_told = set()

_cfg_seen = None

_limited = False


def _my_name():
    try:
        return str((network._load_launcher_config() or {}).get("name") or "")[:16]
    except Exception:
        return ""


def clean(rules):
    """{name: [ids]} with bad entries dropped. Pure."""
    out = {}
    for name, ids in (rules or {}).items():
        try:
            ids = sorted({int(i) for i in ids})
        except (TypeError, ValueError):
            continue

        if str(name).strip():
            if ids:
                out[str(name).strip()[:16]] = ids

    return out


def allowed_for(name, household_ids, rules=None):
    """The Sim ids `name` may control, or None for "all". Pure.
    Only ids still in the household count; if none are left, no restriction."""
    global _rules
    rules = _rules if rules is None else rules
    ids = rules.get(name)
    if not ids:
        return
    keep = [i for i in ids if i in set(household_ids)]
    return keep or None


def players_allowed(sim_id, members=None):
    """Player ids whose rules include this Sim (host's lobby member map: pid -> {"name"})."""
    if not _rules:
        return []
    if members is None:
        try:
            from multimod import lobby

            members = dict(lobby._members)
        except Exception:
            members = {}

    return sorted(
        (pid for pid, m in members.items() if int(sim_id) in _rules.get(str(m.get("name", "")), []))
    )


def _client():
    import services

    return services.client_manager().get_first_client()


def _household_ids():
    import services

    hh = services.active_household()
    if hh is not None:
        return [int(si.sim_id) for si in hh.sim_info_gen()]
    return []


def _notify_once(key, text):
    if key not in _told:
        _told.add(key)
        network._notify(text)


def restore():
    """Session over (or rules cleared): every household Sim is selectable again. The game doesn't
    save the selectable list, but a player who stays in the game after the host leaves - or the
    host playing the same save solo later - must get everyone back without a reload."""
    global _limited
    client = _client()
    if client is None:
        return
    have = {int(si.sim_id) for si in client.selectable_sims}
    for sid in _household_ids():
        if sid not in have:
            client.add_selectable_sim_by_id(sid)

    _limited = False
    network._log("control: all household Sims selectable again")


def apply():
    """Trim this player's selectable Sims to their allowed ones. Returns a short status.
    Only during a multiplayer session - rules left in the config never limit a solo game."""
    global _limited
    if not in_session():
        if _limited:
            restore()
        return "not in a session"
    client = _client()
    if client is None:
        return "no client"
    allowed = allowed_for(_my_name(), _household_ids())
    if allowed is None:
        if _limited:
            restore()
        return "all"
    _limited = True
    have = {int(si.sim_id) for si in client.selectable_sims}
    for sid in allowed:
        if sid not in have:
            client.add_selectable_sim_by_id(sid)

    for si in list(client.selectable_sims):
        if int(si.sim_id) not in allowed:
            client.remove_selectable_sim_info(si)

    active = client.active_sim_info
    if active is None or int(active.sim_id) not in allowed:
        client.set_active_sim_by_id(allowed[0])
    return "limited to {}".format(allowed)


def _install_guard():
    """Refuse switching to a Sim this player doesn't control."""
    global _patched
    if _patched:
        return
    try:
        from server.client import Client
    except Exception:
        return
    else:
        if getattr(Client.set_active_sim_info, "_s4om_control", False):
            _patched = True
            return
        orig = Client.set_active_sim_info

        def set_active_sim_info(self, sim_info):
            try:
                if sim_info is not None:
                    if _rules:
                        if in_session():
                            allowed = allowed_for(_my_name(), _household_ids())
                            if allowed is not None:
                                if int(sim_info.sim_id) not in allowed:
                                    _notify_once(
                                        ("deny", int(sim_info.sim_id)),
                                        "S4OM: the host gave {} to another player - you can't control them in this game.".format(
                                            getattr(sim_info, "first_name", "that Sim")
                                        ),
                                    )
                                    return False
            except Exception as e:
                network._log("control guard error: {}".format(e))

            return orig(self, sim_info)

        set_active_sim_info._s4om_control = True
        Client.set_active_sim_info = set_active_sim_info
        _patched = True
        network._log("control: switch guard installed")


def in_session():
    if network._is_host:
        return bool(network._clients)
    return network._client_socket is not None


def with_members(base, members, present):
    """Pure (tested). The host's rules plus, for every connected player who has a Sim in this
    world (living_world members: brought from their Library, or let in as one), "plays that Sim"
    - unless the host set a rule for them. Live test 2026-10-26: the launcher's rule for a
    drop-in player got lost and they could control everyone."""
    out = {k: list(v) for k, v in (base or {}).items()}
    for m in (members or {}).values():
        name, sim = str(m.get("name") or ""), m.get("sim")
        if name and sim and name in present and name not in out:
            out[name] = [int(sim)]

    return clean(out)


_base = None

_last_members = 0.0


def _member_rules(now):
    global _base
    global _last_members
    if now - _last_members < 5.0:
        return
    _last_members = now
    try:
        from multimod import living_world

        members = living_world.load_members()
        return with_members(
            _base if _base is not None else _rules, members, living_world.present_members(members)
        )
    except Exception as e:
        network._log("control: member rules failed: {}".format(e))
        return


def set_rules(rules):
    """Host (from the launcher): new rules - keep, send to everyone, apply here."""
    global _last_send
    global _rules
    _rules = clean(rules)
    _told.clear()
    network._log("control: rules {}".format(_rules))
    if network._is_host:
        network._broadcast({"type": "control_rules", "rules": _rules})
        _last_send = time.time()
    try:
        network._log("control: {}".format(apply()))
    except Exception as e:
        network._log("control apply failed: {}".format(e))


def on_rules(data):
    """Joiner: rules from the host."""
    global _rules
    new = clean(data.get("rules"))
    if new != _rules:
        _rules = new
        _told.clear()
        network._log("control: rules from host {}".format(_rules))
        try:
            network._log("control: {}".format(apply()))
        except Exception as e:
            network._log("control apply failed: {}".format(e))


def tick(now=None):
    """Main thread, every frame-ish: keep the guard on, re-apply after travel/births, and let
    late joiners have the rules (host re-sends every 10 s while any rule is set)."""
    global _base
    global _cfg_seen
    global _last_apply
    global _last_send
    now = time.time() if now is None else now
    _install_guard()
    if network._is_host:
        try:
            want = clean((network._load_launcher_config() or {}).get("control_rules") or {})
        except Exception:
            want = _cfg_seen

        if want != _cfg_seen:
            first = _cfg_seen is None
            _cfg_seen = want
            if not first or want:
                _base = want
            if want != _rules:
                if not first or want:
                    set_rules(want)
        eff = _member_rules(now)
        if eff is not None:
            if eff != _rules:
                set_rules(eff)
    if not _rules:
        if _limited:
            restore()
        return
    if not in_session():
        if _limited:
            restore()
        return
    if network._is_host:
        if network._clients:
            if now - _last_send > 10:
                _last_send = now
                network._broadcast({"type": "control_rules", "rules": _rules})
    if now - _last_apply >= REAPPLY_EVERY:
        _last_apply = now
        try:
            apply()
        except Exception as e:
            network._log("control re-apply failed: {}".format(e))
