# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\walk.py
# Compiled at: 2026-10-26 14:40:27
# Size of source mod 2**32: 8262 bytes

"""S4OM walking: another player's Sim WALKS to where they are instead of sliding there.

sync.py receives each remote Sim's position. Before this, the receiving game slid the Sim
along the ground (set location in small steps) - no walk animation, it looked like gliding.
Now, when the Sim is more than a step away, its game pushes the game's own "Go Here"
(terrain-GoHere) at that spot, so it routes around furniture with the normal walk. Short
corrections still slide; a Sim that's very far away (or not getting anywhere) still jumps.

Runs on the game's main thread (called from sync.process_message).
"""

import time

from multimod import network

WALK_MIN = 1.0

TELEPORT_DIST = 30.0

REPUSH_MOVE = 1.0

REPUSH_EVERY = 1.0

STUCK_AFTER = 8.0

_pushed = {}

_go_here = None

_lookup_failed = False


def _d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def decide(cur, target, pushed, now):
    """What to do with a remote Sim: 'slide', 'teleport', 'walk' (push Go Here) or 'walking'
    (already on the way - leave them alone). pushed = (target, time) of the last Go Here or None."""
    dist = _d(cur, target)
    if dist < WALK_MIN:
        return "slide"
    if dist > TELEPORT_DIST:
        return "teleport"
    if pushed is not None:
        p_target, p_at = pushed
        if _d(p_target, target) < REPUSH_MOVE:
            if now - p_at > STUCK_AFTER:
                return "slide"
            return "walking"
        if now - p_at < REPUSH_EVERY:
            return "walking"
    return "walk"


def pick_go_here(affs):
    """The plain adult Go Here among the game's Go Here interactions (pure, tested):
    not the toddler/bike/ocean/pool/rally/debug variants; shortest name wins."""
    bad = (
        "toddler",
        "bike",
        "ocean",
        "pool",
        "rally",
        "debug",
        "swim",
        "horse",
        "child",
        "pet",
        "dog",
        "cat",
        "fade",
        "canoe",
        "snow",
        "moveaway",
    )
    ok = [a for a in affs if not any((b in getattr(a, "__name__", "").lower() for b in bad))]
    exact = [a for a in ok if getattr(a, "__name__", "").lower() in ("terrain-gohere", "terrain_gohere")]
    pool = exact or ok
    if pool:
        return min(pool, key=(lambda a: len(getattr(a, "__name__", ""))))


def _affordance():
    global _go_here
    global _lookup_failed
    if _go_here is not None or _lookup_failed:
        return _go_here
    try:
        import services, sims4.resources

        terrain = services.terrain_service.terrain_object()
        found = [a for a in terrain.super_affordances() if "gohere" in getattr(a, "__name__", "").lower()]
        if not found:
            mgr = services.get_instance_manager(sims4.resources.Types.INTERACTION)
            found = [a for a in mgr.types.values() if "gohere" in getattr(a, "__name__", "").lower()]
        network._log(
            "walk: Go Here candidates: {}".format(sorted((getattr(a, "__name__", "?") for a in found))[:20])
        )
        _go_here = pick_go_here(found)
        network._log("walk: using {}".format(getattr(_go_here, "__name__", None)))
    except Exception as e:
        network._log("walk: Go Here lookup failed: {}".format(e))

    if _go_here is None:
        _lookup_failed = True
        network._log("walk: no Go Here interaction found - remote Sims slide instead")
    return _go_here


def push_go_here(sim, pos):
    """Make this Sim walk to pos with the game's own Go Here. Returns True if it was queued."""
    aff = _affordance()
    if aff is None:
        return False
    try:
        from sims4.math import Vector3
        from objects.terrain import TerrainPoint
        from interactions.context import InteractionContext, QueueInsertStrategy
        from interactions.priority import Priority
        from server.pick_info import PickInfo, PickType

        v = Vector3(float(pos[0]), float(pos[1]), float(pos[2]))
        surface = sim.routing_surface
        target = TerrainPoint.create_for_position_and_orientation(position=v, routing_surface=surface)
        pick = PickInfo(pick_type=(PickType.PICK_TERRAIN), target=target, location=v, routing_surface=surface)
        ctx = InteractionContext(
            sim,
            (InteractionContext.SOURCE_SCRIPT),
            (Priority.High),
            insert_strategy=(QueueInsertStrategy.NEXT),
            pick=pick,
        )
        return bool(sim.push_super_affordance(aff, target, ctx))
    except Exception as e:
        network._log("walk: Go Here failed: {}".format(e))
        return False


_routes = {}


def route_action(current, dest, has_dest, now, cur_pos, pos):
    """Pure (tested). The owner's game says where its Sim's walk ends ("dest") the moment the
    walk starts, so we walk the same route at the same time instead of chasing old positions.
    Returns 'go' (push Go Here to dest), 'follow' (on that walk - ignore in-between positions),
    'stop' (owner stopped walking - forget the route) or None (no route info, old behaviour)."""
    if has_dest and dest is None:
        if current is not None:
            return "stop"
        return
    if dest is not None:
        d = (float(dest[0]), float(dest[1]), float(dest[2]))
        if current is None or _d(current[0], d) > 0.5:
            return "go"
        if now - current[1] < 30.0 and _d(cur_pos, pos) < TELEPORT_DIST:
            return "follow"
        return
    if current is not None and now - current[1] < 30.0 and _d(cur_pos, pos) < TELEPORT_DIST:
        return "follow"


def handle(sim, sim_id, pos, dest=None, has_dest=False):
    """Called for each remote position. Returns True if the Sim is walking (caller must NOT also
    slide it), False to let the caller slide/jump as before."""
    try:
        c = sim.position
        cur = (c.x, c.y, c.z)
    except Exception:
        return False
    target = (float(pos[0]), float(pos[1]), float(pos[2]))
    now = time.time()
    r = route_action(_routes.get(sim_id), dest, has_dest, now, cur, target)
    if r == "go":
        d = (float(dest[0]), float(dest[1]), float(dest[2]))
        ok = push_go_here(sim, d)
        network._log("walk: sim {} walking to {:.1f},{:.1f} -> {}".format(sim_id, d[0], d[2], ok))
        if ok:
            _routes[sim_id] = (d, now)
            return True
    elif r == "follow":
        return True
    elif r == "stop":
        _routes.pop(sim_id, None)
    what = decide(cur, target, _pushed.get(sim_id), now)
    try:
        from multimod import interaction_sync

        busy = interaction_sync.recently_applied(sim_id)
    except Exception:
        busy = False

    if busy and what != "teleport":
        return True
    if what == "walking":
        return True
    if what == "walk":
        if push_go_here(sim, target):
            _pushed[sim_id] = (target, now)
            return True
    _pushed.pop(sim_id, None)
    return False


try:
    import sims4.commands

    @sims4.commands.Command("mp_walk_test", command_type=(sims4.commands.CommandType.Live))
    def mp_walk_test(dx="4", dz="0", _connection=None):
        """Send the selected Sim walking dx/dz metres with the same call remote Sims get."""
        import services

        out = sims4.commands.CheatOutput(_connection)
        sim = services.client_manager().get_first_client().active_sim
        p = sim.position
        ok = push_go_here(sim, (p.x + float(dx), p.y, p.z + float(dz)))
        out("walk test: {}".format(ok))
        network._log("walk test: {} from {:.1f},{:.1f} by {},{}".format(ok, p.x, p.z, dx, dz))

except Exception:
    pass
