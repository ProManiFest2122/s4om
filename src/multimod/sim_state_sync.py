# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\sim_state_sync.py
# Compiled at: 2026-10-26 18:16:46
# Size of source mod 2**32: 13514 bytes

"""S4OM Sim state sync: traits, careers and satisfaction points - plus auto-solve needs.

Each player owns their own Sim. Every few seconds the mod reads that Sim's
  - traits that matter to gameplay: personality, reward-store traits, aspiration,
    lifestyles, likes/dislikes, fears, child rewards (traits.trait_type.TraitType)
  - careers and the level in each (career_tracker.careers -> career.user_level)
  - satisfaction points (rewards store currency)
and when anything changed, sends the full set. Everyone else applies it to that Sim on
their PC (the host forwards it to other joiners). Cheats that change these
(traits.equip_trait, careers.promote, sims.give_satisfaction_points, ...) sync the same way,
because the result is what's sent, not the command.

Auto-solve needs (launcher option "auto_needs", host only): Sims whose player isn't at
their PC, or household Sims nobody controls, get their needs topped up when they get low,
so nobody comes back to a starving Sim.
"""

import time

from multimod import network

SYNC_TRAIT_TYPES = (0, 1, 6, 21, 22, 23, 29, 36)

STAT_KEYWORDS = (
    "skill",
    "follower",
    "rank",
    "perk",
    "point",
    "power",
    "fame",
    "reputation",
    "talent",
    "spellcaster",
    "vampire",
    "werewolf",
    "occult",
    "mermaid",
    "alien",
)

SEND_EVERY = 3.0

NEEDS_EVERY = 10.0

NEED_LOW, NEED_REFILL = (-40.0, 30.0)

_last_sent = {}

MOTIVE_STEP = 5

_last_read = 0.0

_last_needs = 0.0

_applying = False


def _my_sim_info():
    try:
        from multimod import sync

        sim = sync._get_active_sim()
        if sim is not None:
            return sim.sim_info
        return
    except Exception:
        return


def read_state(si):
    traits = []
    try:
        for t in si.trait_tracker.equipped_traits:
            if int(getattr(t, "trait_type", -1)) in SYNC_TRAIT_TYPES:
                traits.append(int(t.guid64))

    except Exception:
        pass

    careers, at_work = [], []
    try:
        for c in si.career_tracker.careers.values():
            careers.append([int(c.guid64), int(c.user_level)])
            if c.currently_at_work:
                at_work.append(int(c.guid64))

    except Exception:
        pass

    try:
        sat = int(si.get_satisfaction_points())
    except Exception:
        sat = None

    stats, motives = {}, {}
    for tracker in (getattr(si, "statistic_tracker", None), getattr(si, "commodity_tracker", None)):
        try:
            for stat in tracker or ():
                name = type(stat).__name__.lower()
                if name.startswith("motive"):
                    motives[str(int(stat.guid64))] = int(round(stat.get_value() / MOTIVE_STEP) * MOTIVE_STEP)
                    continue
                if not any((k in name for k in STAT_KEYWORDS)):
                    continue
                stats[str(int(stat.guid64))] = int(stat.get_value())

        except Exception:
            continue

    buffs = []
    try:
        for b in si.Buffs:
            if getattr(b, "visible", True):
                if getattr(b, "commodity", None) is None:
                    buffs.append(int(b.guid64))

    except Exception:
        pass

    try:
        age = int(si.age)
    except Exception:
        age = None

    return {
        "traits": sorted(traits),
        "careers": sorted(careers),
        "sat": sat,
        "stats": stats,
        "motives": motives,
        "buffs": sorted(buffs),
        "age": age,
        "at_work": sorted(at_work),
    }


def tick(now=None):
    """Main thread: send our Sim's state when it changed; host: auto-solve needs."""
    global _last_read
    global _last_sent
    now = now or time.time()
    if now - _last_read >= SEND_EVERY:
        if _session():
            _last_read = now
            for si in _sims_to_send():
                state = read_state(si)
                if state != _last_sent.get(int(si.id)):
                    _last_sent[int(si.id)] = state
                    msg = dict(state, type="sim_state", sim_id=(int(si.id)), ts=now)
                    if network._is_host:
                        network._broadcast(msg)
                    elif network._client_socket is not None:
                        network._send_json(network._client_socket, msg)

    _auto_needs(now)


def _session():
    if network._is_host:
        return bool(network._clients)
    return network._client_socket is not None


def _sims_to_send():
    """Joiner: the Sim they play. Host: its own Sim + every household Sim no joiner plays
    (fork 2026-10-26 - before, only the host's selected Sim was ever sent)."""
    mine = _my_sim_info()
    out = [mine] if mine is not None else []
    if network._is_host:
        try:
            import services
            from multimod import dialog_router

            owners = dict(dialog_router._owners)
            for si in services.active_household().sim_info_gen():
                if si is not mine:
                    if owners.get(int(si.id)) in (None, 0):
                        if si.is_instanced():
                            out.append(si)

        except Exception:
            pass

    return out


def _manager(kind):
    import services, sims4.resources

    return services.get_instance_manager(getattr(sims4.resources.Types, kind))


def plan(local, remote):
    """What to change locally to match `remote`. Pure - tested without the game."""
    lt, rt = set(local["traits"]), set(remote.get("traits") or [])
    lc = {g: lvl for g, lvl in local["careers"]}
    rc = {int(g): int(lvl) for g, lvl in remote.get("careers") or []}
    out = {
        "add_traits": sorted(rt - lt),
        "remove_traits": sorted(lt - rt),
        "join": sorted(((g, l) for (g, l) in rc.items() if g not in lc)),
        "quit": sorted((g for g in lc if g not in rc)),
        "level": sorted(((g, rc[g] - lc[g]) for g in rc if g in lc and rc[g] != lc[g])),
        "sat_delta": 0,
        "go_to_work": sorted(set(remote.get("at_work") or []) - set(local.get("at_work") or [])),
        "back_from_work": sorted(set(local.get("at_work") or []) - set(remote.get("at_work") or []))
        if ("at_work" in remote)
        else [],
        "stats": {
            g: v
            for g, v in (remote.get("stats") or {}).items()
            if g not in (local.get("stats") or {}) or abs(int(v) - int(local["stats"][g])) >= 1
        },
        "age_up": (isinstance(remote.get("age"), int))
        and (isinstance(local.get("age"), int) and remote["age"] > local["age"]),
        "add_buffs": sorted(set(remote.get("buffs") or []) - set(local.get("buffs") or []))
        if ("buffs" in remote)
        else [],
        "remove_buffs": sorted(set(local.get("buffs") or []) - set(remote.get("buffs") or []))
        if ("buffs" in remote)
        else [],
        "motives": {
            g: v
            for g, v in (remote.get("motives") or {}).items()
            if g not in (local.get("motives") or {}) or abs(int(v) - int(local["motives"][g])) >= MOTIVE_STEP
        },
    }
    if isinstance(remote.get("sat"), int) and isinstance(local.get("sat"), int):
        out["sat_delta"] = remote["sat"] - local["sat"]
    return out


def process_message(data):
    global _applying
    try:
        import services

        si = services.sim_info_manager().get(int(data.get("sim_id", 0)))
    except Exception:
        return
    else:
        if si is None:
            return
        mine = _my_sim_info()
        if mine is not None:
            if int(mine.id) == int(si.id):
                return
        p = plan(read_state(si), data)
        _applying = True
        errors = []

        def step(name, fn):
            try:
                fn()
            except Exception as e:
                errors.append("{}: {}".format(name, e))

        def do_traits():
            traits = _manager("TRAIT")
            for g in p["remove_traits"]:
                t = traits.get(g)
                if t is not None:
                    si.remove_trait(t)

            for g in p["add_traits"]:
                t = traits.get(g)
                if t is not None:
                    si.add_trait(t)

        def do_careers():
            careers = _manager("CAREER")
            for g, lvl in p["join"]:
                ct = careers.get(g)
                if ct is not None:
                    si.career_tracker.add_career(
                        (ct(si)),
                        show_confirmation_dialog=False,
                        user_level_override=(max(1, lvl)),
                        give_skipped_rewards=False,
                        show_join_msg=False,
                        show_icon_override_picker=False,
                    )

            for g in p["quit"]:
                si.career_tracker.remove_career(g, post_quit_msg=False)

            for g, diff in p["level"]:
                c = si.career_tracker.careers.get(g)
                if c is not None:
                    c.promote(levels_to_promote=diff) if diff > 0 else c.demote(levels_to_demote=(-diff))

            for g in p["go_to_work"]:
                c = si.career_tracker.careers.get(g)
                if c is not None:
                    c.put_sim_in_career_rabbit_hole()

            for g in p["back_from_work"]:
                c = si.career_tracker.careers.get(g)
                if c is not None:
                    c.remove_sim_from_career_rabbit_hole(suppress_error=True)

        def do_sat():
            if p["sat_delta"]:
                si.apply_satisfaction_points_delta(p["sat_delta"], 0)

        def do_stats():
            if p["stats"] or p["motives"]:
                stat_mgr = _manager("STATISTIC")
                for g, v in list(p["stats"].items()) + list(p["motives"].items()):
                    st = stat_mgr.get(int(g))
                    tracker = si.get_tracker(st) if st is not None else None
                    if tracker is not None:
                        tracker.set_value(st, int(v))

        def do_buffs():
            if p["add_buffs"] or p["remove_buffs"]:
                mgr = _manager("BUFF")
                for g in p["add_buffs"]:
                    bt = mgr.get(int(g))
                    if bt is not None:
                        si.Buffs.add_buff_from_op(bt)

                for g in p["remove_buffs"]:
                    bt = mgr.get(int(g))
                    if bt is not None:
                        si.Buffs.remove_buff_by_type(bt)

        def do_age():
            for _ in range(3):
                if not isinstance(data.get("age"), int) or int(si.age) >= int(data["age"]):
                    break
                si.advance_age()

        try:
            for name, fn in (
                ("age", do_age),
                ("traits", do_traits),
                ("careers", do_careers),
                ("reward points", do_sat),
                ("skills/needs", do_stats),
                ("moodlets", do_buffs),
            ):
                step(name, fn)

            changed = [k for k, v in p.items() if v]
            if changed:
                network._log("sim_state: sim {} synced {}".format(si.id, ", ".join(changed)))
            if errors:
                network._log("sim_state apply errors: {}".format("; ".join(errors)))
        finally:
            _applying = False


def _auto_needs(now):
    global _last_needs
    if not network._is_host or now - _last_needs < NEEDS_EVERY:
        return
    _last_needs = now
    try:
        if (network._load_launcher_config() or {}).get("auto_needs", False) is not True:
            return
        import services

        hh = services.active_household()
        mine = _my_sim_info()
        for si in hh.sim_info_gen() if hh is not None else ():
            if mine is not None and si.id == mine.id:
                continue
            top_up(si)

    except Exception as e:
        network._log("auto needs error: {}".format(e))


def top_up(si, low=NEED_LOW, refill=NEED_REFILL):
    """Raise motives (hunger, energy, bladder, hygiene, fun, social...) that dropped below `low`."""
    fixed = 0
    tracker = getattr(si, "commodity_tracker", None)
    if tracker is None:
        return 0
    for stat in list(tracker):
        name = type(stat).__name__.lower()
        if not name.startswith("motive_") and "_motive_" not in name:
            continue
        try:
            if stat.get_value() < low:
                stat.set_value(refill)
                fixed += 1
        except Exception:
            continue

    return fixed
