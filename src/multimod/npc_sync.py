# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\npc_sync.py
# Compiled at: 2026-11-15 17:19:38
# Size of source mod 2**32: 11450 bytes

"""S4OM visitors & walk-bys: joiners show exactly the Sims the host's game has on the lot.

Each PC used to spawn its own NPCs (walk-bys, visitors, neighbours), so the host saw a neighbour
the joiner didn't, and the joiner's own walk-bys never existed for the host. The host already
reports every Sim it controls (sync.py: position + walk every 5 s at least). On a joiner:
  - a reported Sim that isn't on the lot here -> spawned at that spot (if the Sim exists in
    this save - a brand-new generated NPC doesn't yet; logged)
  - an NPC on the lot here that the host hasn't reported for STALE seconds -> removed
Household Sims and the Sim this player plays are never touched.
"""

import time

from multimod import network

STALE = 20.0

RETRY = 10.0

_reported = {}

_tried = {}

_missing_logged = set()

_last_tick = 0.0


def to_remove(instanced, household, mine, reported, now, joined_at, stale=STALE):
    """NPC ids on our lot the host no longer has. Nothing until we've been in the session a
    while (the host's first reports need time to arrive)."""
    if now - joined_at < stale:
        return []
    return sorted(
        (s for s in instanced if s not in household if s != mine if now - reported.get(s, 0) > stale)
    )


MAX_BACKOFF = 300.0

_attempts = {}


def retry_after(attempts, retry=RETRY, cap=MAX_BACKOFF):
    """Pure (tested): 10 s, 20 s, 40 s ... up to 5 min between tries for the same Sim."""
    if attempts:
        return min(cap, retry * 2 ** max(0, attempts - 1))
    return retry


def should_spawn(sim_id, now, tried, retry=RETRY, attempts=None):
    n = (attempts or {}).get(sim_id, 0)
    return now - tried.get(sim_id, 0) >= retry_after(n, retry)


def reported(sim_id, pos):
    """Host reported this Sim. Spawn it here if it isn't on our lot."""
    global _allow
    if network._is_host:
        return
    sid = int(sim_id)
    now = time.time()
    _reported[sid] = now
    try:
        import services

        si = services.sim_info_manager().get(sid)
        if si is None:
            if sid not in _missing_logged:
                _missing_logged.add(sid)
                network._log(
                    "npc_sync: host has Sim {} that isn't in this save yet - asking for them".format(sid)
                )
                network._send_json(network._client_socket, {"type": "npc_req", "sim_id": sid})
            return
        if si.get_sim_instance() is not None:
            _attempts.pop(sid, None)
            return
        if not should_spawn(sid, now, _tried, attempts=_attempts):
            return
        _tried[sid] = now
        n = _attempts[sid] = _attempts.get(sid, 0) + 1
        from sims.sim_spawner import SimSpawner
        from sims4.math import Vector3

        _allow = True
        try:
            ok = SimSpawner.spawn_sim(
                si,
                sim_position=(Vector3(float(pos[0]), float(pos[1]), float(pos[2]))),
                spawn_at_lot=False,
                use_fgl=True,
            )
        finally:
            _allow = False

        if ok is False:
            network._log(
                "npc_sync: game refused to spawn {} {} (try {}, next in {:.0f}s)".format(
                    si.first_name, sid, n, retry_after(n)
                )
            )
        else:
            if n == 1 or n in (3, 6):
                network._log(
                    "npc_sync: spawned {} {} like on the host{}".format(
                        si.first_name,
                        sid,
                        ""
                        if n == 1
                        else " (try {}, they keep leaving - next in {:.0f}s)".format(n, retry_after(n)),
                    )
                )
    except Exception as e:
        network._log("npc_sync spawn error: {}".format(e))


_joined_at = None

_allow = False

_blocked = 0


def should_block(is_host, in_session, from_load, selectable, in_household, allowed):
    """Pure (tested): refuse the joiner's own NPC spawns (walk-bys, visitors, service Sims) -
    the host's game decides who is on the lot and npc_sync spawns them here."""
    return (
        not is_host and in_session and not from_load and not selectable and not in_household and not allowed
    )


def install():
    """Wrap SimSpawner.spawn_sim (sims/sim_spawner.py). The game has its own switch that refuses
    non-selectable spawns (disable_spawning_non_selectable_sims), so callers handle False."""
    try:
        from sims.sim_spawner import SimSpawner

        if getattr(SimSpawner.spawn_sim, "_s4om", False):
            return True
        orig = SimSpawner.spawn_sim.__func__

        def spawn_sim(cls, sim_info, *args, **kwargs):
            global _blocked
            try:
                import services

                hh = services.active_household()
                block = should_block(
                    network._is_host,
                    network._client_socket is not None,
                    bool(kwargs.get("from_load")),
                    bool(getattr(sim_info, "is_selectable", False)),
                    hh is not None and sim_info.household is hh,
                    _allow,
                )
            except Exception:
                block = False

            if block:
                _blocked += 1
                if _blocked <= 20 or _blocked % 50 == 0:
                    network._log(
                        "npc_sync: kept {} off the lot (the host decides who's here) #{}".format(
                            getattr(sim_info, "first_name", "?"), _blocked
                        )
                    )
                return False
            return orig(cls, sim_info, *args, **kwargs)

        spawn_sim._s4om = True
        SimSpawner.spawn_sim = classmethod(spawn_sim)
        network._log("npc_sync: spawn guard installed")
        return True
    except Exception as e:
        network._log("npc_sync: spawn guard failed: {}".format(e))
        return False


def tick():
    """Joiner, every few seconds: remove NPCs the host doesn't have."""
    global _joined_at
    global _last_tick
    now = time.time()
    if network._is_host or network._client_socket is None:
        _joined_at = None
        return
    if _joined_at is None:
        _joined_at = now
    if now - _last_tick < 3.0:
        return
    _last_tick = now
    install()
    try:
        import services
        from multimod import sync

        hh = services.active_household()
        household = {int(s.id) for s in hh.sim_info_gen()} if hh is not None else set()
        inst = {int(s.sim_info.id): s for s in services.sim_info_manager().instanced_sims_gen()}
        for sid in to_remove(set(inst), household, sync._get_my_sim_id(), _reported, now, _joined_at):
            sim = inst[sid]
            sim.schedule_destroy_asap(source=sim, cause="S4OM: not on the host's lot")
            network._log(
                "npc_sync: removed {} {} (host doesn't have them)".format(sim.sim_info.first_name, sid)
            )
            _reported[sid] = now

    except Exception as e:
        network._log("npc_sync tick error: {}".format(e))


def on_request(data, sender_pid=None):
    """Host: a joiner doesn't have this Sim (generated during play) - send the Sim and their
    household as the game saves them (persistence protos), so the joiner can load them."""
    if not network._is_host:
        return
    try:
        import base64, services

        sid = int(data.get("sim_id", 0))
        si = services.sim_info_manager().get(sid)
        if si is None or si.household is None:
            return
        hh = si.household
        ps = services.get_persistence_service()
        sims = []
        for member in hh.sim_info_gen():
            member.save_sim()
            proto = ps.get_sim_proto_buff(member.sim_id)
            if proto is not None:
                sims.append(base64.b64encode(proto.SerializeToString()).decode())

        hh.save_data()
        hproto = ps.get_household_proto_buff(hh.id)
        if hproto is None:
            return
        network._broadcast(
            {
                "type": "npc_data",
                "sim_id": sid,
                "household_id": int(hh.id),
                "sims": sims,
                "household": (base64.b64encode(hproto.SerializeToString()).decode)(),
            }
        )
        network._log(
            "npc_sync: sent {} {} (+{} household) to joiners".format(si.first_name, sid, len(sims) - 1)
        )
    except Exception as e:
        network._log("npc_sync: send npc failed: {}".format(e))


def on_data(data):
    """Joiner: load a Sim (and household) the host made during play into this game."""
    if network._is_host:
        return
    try:
        import base64, services
        from protocolbuffers import FileSerialization_pb2, Sims_pb2
        from sims.household_manager import HouseholdFixupHelper

        sim_cls = type(Sims_pb2.AccountFamilyData().sim.add())
        hm = services.household_manager()
        if hm.get(int(data.get("household_id", 0))) is not None:
            return
        ps = services.get_persistence_service()
        for b in data.get("sims") or []:
            raw = base64.b64decode(b)
            probe = sim_cls()
            probe.ParseFromString(raw)
            if ps.get_sim_proto_buff(probe.sim_id) is None:
                ps.add_sim_proto_buff(probe.sim_id).MergeFrom(probe)

        hproto = FileSerialization_pb2.HouseholdData()
        hproto.ParseFromString(base64.b64decode(data.get("household") or ""))
        fixup = HouseholdFixupHelper()
        hm.load_household_from_household_proto(hproto, fixup_helper=fixup)
        fixup.fix_shared_sim_households()
        _missing_logged.discard(int(data.get("sim_id", 0)))
        network._log("npc_sync: loaded new Sim {} from the host".format(data.get("sim_id")))
    except Exception as e:
        network._log("npc_sync: load npc failed: {}".format(e))


try:
    import sims4.commands

    @sims4.commands.Command("mp_npc_test", command_type=(sims4.commands.CommandType.Live))
    def mp_npc_test(_connection=None):
        """Testing: make a brand-new Sim (new household) and put them on the lot near the selected Sim."""
        import services
        from sims.sim_spawner import SimSpawner
        from sims.sim_info_types import Age, Gender

        try:
            from sims.sim_spawner import SimCreator
        except ImportError:
            from sims.sim_creator import SimCreator

        (infos, hh) = SimSpawner.create_sim_infos(
            (SimCreator(age=(Age.ADULT), gender=(Gender.FEMALE)),), creation_source="S4OM npc test"
        )
        si = infos[0]
        me = services.client_manager().get_first_client().active_sim
        SimSpawner.spawn_sim(si, sim_position=(me.position), spawn_at_lot=False)
        network._log("npc test: new Sim {} {} (household {})".format(si.first_name, si.sim_id, hh.id))

except Exception:
    pass
