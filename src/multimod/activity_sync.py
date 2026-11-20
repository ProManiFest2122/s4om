# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\activity_sync.py
# Compiled at: 2026-10-26 22:55:11
# Size of source mod 2**32: 5802 bytes

"""S4OM activity snapshots: the PC that plays a Sim is the truth for what it is doing NOW.

interaction_sync sends each new action once. If one message is late, refused or lost, the
other PC's copy of the Sim drifts (live test 2026-10-26: a Sim stood in the bed, cooking
didn't show). Here, every second, the owner sends the set of actions each of its Sims is
running; the other PCs reconcile their copy:
  - an action the owner's Sim runs that ours doesn't -> start it (same path as interaction_sync,
    incl. retries, cooking via recipe, finding the right object)
  - an action ours runs that the owner's doesn't, two snapshots in a row -> cancel it
Standing/idle and recipe steps are left out (the recipe's own chain runs those).
"""

import time

from multimod import network

SEND_EVERY = 1.0

KEYFRAME = 5.0

REPUSH_AFTER = 6.0

_IGNORE = ("stand", "idle", "sim-stand", "reaction_", "carry_holdobject")

_last_send = 0.0

_sent = {}

_pushed = {}

_extra_seen = {}


def ignorable(name):
    n = (name or "").lower()
    return any((k in n for k in _IGNORE))


def reconcile(owner, local, pushed, extra_seen, sim_id, now, repush_after=REPUSH_AFTER):
    """owner/local: {guid: target_id}. -> (to_start [(guid, target)], to_cancel [guid]) and
    updates extra_seen (cancel only on the 2nd snapshot in a row)."""
    start = [
        (g, t) for g, t in owner.items() if g not in local if now - pushed.get((sim_id, g), 0) > repush_after
    ]
    cancel = []
    for g in local:
        key = (sim_id, g)
        if g in owner:
            extra_seen.pop(key, None)
            continue
        extra_seen[key] = extra_seen.get(key, 0) + 1
        if extra_seen[key] >= 2:
            cancel.append(g)
            extra_seen.pop(key, None)

    return (start, cancel)


def running(sim):
    """{guid: target id} of the actions this Sim is running (not idle/standing, not recipe steps)."""
    out = {}
    try:
        from multimod import interaction_sync

        for si in sim.si_state.sis_actor_gen():
            aff = getattr(si, "affordance", None)
            if aff is None or ignorable(getattr(aff, "__name__", "")):
                continue
            if interaction_sync._kind(si) == "craft_step":
                continue
            g = getattr(aff, "guid64", None)
            if g:
                out[int(g)] = int(getattr(getattr(si, "target", None), "id", 0) or 0)

    except Exception:
        pass

    return out


def tick(now=None):
    global _last_send
    now = now or time.time()
    if now - _last_send < SEND_EVERY:
        return
    _last_send = now
    try:
        from multimod import interaction_sync, dialog_router, sim_state_sync

        if not sim_state_sync._session():
            return
        owners = dict(dialog_router._owners)
        me = interaction_sync._get_my_sim()
        my_id = getattr(getattr(me, "sim_info", None), "id", None)
        for sim in interaction_sync._iter_sims():
            sid = int(sim.sim_info.id)
            if not interaction_sync.owns(sid, network._is_host, owners, my_id):
                continue
            acts = running(sim)
            prev = _sent.get(sid)
            if prev is not None:
                if prev[0] == frozenset(acts):
                    if now - prev[1] < KEYFRAME:
                        continue
            _sent[sid] = (frozenset(acts), now)
            network._broadcast(
                {"type": "activity", "sim_id": sid, "acts": {str(g): t for g, t in acts.items()}, "ts": now}
            )

    except Exception as e:
        network._log("activity tick error: {}".format(e))


def reset():
    _sent.clear()


def process_message(data):
    try:
        import services
        from multimod import interaction_sync, sync
        from interactions.interaction_finisher import FinishingType

        sid = int(data.get("sim_id", 0))
        if sid == (sync._get_my_sim_id() or 0):
            return
        si_ = services.sim_info_manager().get(sid)
        sim = si_.get_sim_instance() if si_ is not None else None
        if sim is None:
            return
        owner = {int(g): int(t) for g, t in (data.get("acts") or {}).items()}
        local = running(sim)
        now = time.time()
        (start, cancel) = reconcile(owner, local, _pushed, _extra_seen, sid, now)
        if interaction_sync.recently_applied(sid, within=REPUSH_AFTER):
            start = []
        for g, t in start:
            _pushed[(sid, g)] = now
            interaction_sync.process_message(
                {"type": "interaction", "sim_id": sid, "affordance": g, "target_id": t, "target_def": 0}
            )

        if cancel:
            for si in list(sim.si_state.sis_actor_gen()):
                aff = getattr(si, "affordance", None)
                if aff is not None:
                    if int(getattr(aff, "guid64", 0) or 0) in cancel:
                        si.cancel(FinishingType.USER_CANCEL, "S4OM: not happening on the owner's PC")

        if start or cancel:
            network._log("activity: sim {} start {} cancel {}".format(sid, [g for g, _ in start], cancel))
    except Exception as e:
        network._log("activity apply error: {}".format(e))
