# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\living_world.py
# Compiled at: 2026-10-27 19:11:35
# Size of source mod 2**32: 17525 bytes

r"""S4OM living world: players' Sims move into the household when they join and out when they leave.

Nothing is ever deleted. When Ani leaves Kam's game, her Sim Jerry moves OUT of the played
household into a household of his own that stays in Kam's save - so Tom and Jerry's friendship,
memories and skills are still there. When Ani comes back she gets the same Jerry back in.

  - Host-authoritative: the host decides every move and tells every player's game to make the
    same move ("world_move"), so all games agree on who's in the household.
  - Membership per save, keyed by the player's TOKEN key from the room server (not their typed
    name - nobody can claim someone else's Sim): Mods\mp_world_members.json
      {"<slot>": {"<key>": {"name": "Ani", "sim": 102, "since": 1792900000}}}
  - The game allows 8 Sims in a household; S4OM refuses a move-in past that.
  - Session start (host): members whose player isn't in the game are moved out - after a crash
    the last autosave can still have them in the household.

Console (testing): mp_world_in <sim id> [name], mp_world_out <sim id>, mp_world_list
"""

import json, os, time

from multimod import network

HOUSEHOLD_MAX = 8

_moved_out_home = {}

_cleaned_slot = None

_left = []

_joined = []

JOIN_WAIT = 12.0


def can_move_in(household_size, already_member):
    """(ok, reason). The game won't hold more than 8 Sims in one household."""
    if already_member:
        return (True, "already in the household")
    if household_size >= HOUSEHOLD_MAX:
        return (False, "the household is full (8 Sims)")
    return (True, "")


def to_move_out(members, household_ids, present_keys):
    """Session start: members whose Sim is in the household but whose player isn't here."""
    return sorted(
        (m["sim"] for k, m in members.items() if m.get("sim") in household_ids if k not in present_keys)
    )


def member_for_name(members, name, roster):
    """Who is this player? (key, member, why). The host's launcher writes the room's roster
    (name -> token key); the key decides, never the typed name:
      key is a member            -> theirs
      name matches a member, key doesn't -> someone using another player's name: nothing
      no roster (LAN, old launcher) -> match by name, as before.  Pure."""
    if roster is None:
        for k, m in members.items():
            if m.get("name") == name:
                return (k, m, "by name (no roster)")

        return (None, None, "not a member")
    key = roster.get(name)
    if key:
        if key in members:
            return (key, members[key], "by key")
    if any((m.get("name") == name for m in members.values())):
        return (None, None, "{} isn't the player who owns that name".format(name))
    return (None, None, "not a member")


def verified_present(members, names, roster):
    """Names of world members who are REALLY here. With the host's roster the token key must match
    (someone typing 'Ani' isn't Ani); without one (LAN) the name is all there is. Pure."""
    out = set()
    for key, m in (members or {}).items():
        n = m.get("name")
        if n in names and (roster is None or roster.get(n) == key):
            out.add(n)

    return out


def member_for_sim(members, sim_id):
    for k, m in members.items():
        if m.get("sim") == sim_id:
            return (k, m)

    return (None, None)


def _path():
    return os.path.join(os.path.dirname(network.CMD_PATH), "mp_world_members.json")


ROSTER_FRESH = 90.0


def load_roster(path=None, now=None):
    r"""Mods\mp_roster.json from the host's launcher: {name: token key}, or None when there is none
    or it's stale (nobody refreshing it = the launcher isn't running or this isn't a relay room)."""
    path = path or os.path.join(os.path.dirname(network.CMD_PATH), "mp_roster.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return
    if (now if now is not None else time.time()) - float(data.get("at") or 0) > ROSTER_FRESH:
        return
    return {str(k): str(v) for k, v in (data.get("players") or {}).items()}


def _slot():
    try:
        import services

        slot = services.get_persistence_service().get_save_slot_proto_buff()
        if slot is not None:
            return str(int(slot.slot_id))
        return
    except Exception:
        return


def load_members(slot=None, path=None):
    slot = slot or _slot()
    try:
        data = json.load(open(path or _path(), encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    return (data.get(slot) or {}) if slot else {}


def save_members(members, slot=None, path=None):
    slot = slot or _slot()
    if not slot:
        return
    path = path or _path()
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    data[slot] = members
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _sim_info(sim_id):
    import services

    return services.sim_info_manager().get(int(sim_id))


def _active_household():
    import services

    return services.active_household()


def _household_ids():
    hh = _active_household()
    if hh is not None:
        return [int(si.sim_id) for si in hh.sim_info_gen()]
    return []


def _despawn(sim_info):
    sim = sim_info.get_sim_instance()
    if sim is not None:
        try:
            sim.schedule_destroy_asap(source=sim, cause="S4OM: player left the game")
        except TypeError:
            sim.schedule_destroy_asap()


def _spawn(sim_info):
    if sim_info.get_sim_instance() is not None:
        return
    from sims.sim_spawner import SimSpawner

    SimSpawner.spawn_sim(sim_info, spawn_at_lot=True)


def do_move_in(sim_id):
    """Move a Sim that lives elsewhere in this world into the played household, and onto the lot."""
    import services

    si = _sim_info(sim_id)
    hh = _active_household()
    if si is None or hh is None:
        return (False, "Sim or household not found")
    if si.household is hh:
        _spawn(si)
        return (True, "already in the household")
    (ok, why) = can_move_in(len(list(hh.sim_info_gen())), False)
    if not ok:
        return (False, why)
    services.household_manager().switch_sim_from_household_to_target_household(
        si, (si.household), hh, destroy_if_empty_household=True
    )
    _spawn(si)
    _moved_out_home.pop(int(sim_id), None)
    return (True, "moved in")


def do_move_out(sim_id, home_id=None):
    """Move a Sim out of the played household into a household of their own (kept in the save,
    protected from the game's population clean-up), and off the lot."""
    import services

    si = _sim_info(sim_id)
    hh = _active_household()
    if si is None or hh is None or si.household is not hh:
        return (False, "not in the household")
    if len(list(hh.sim_info_gen())) <= 1:
        return (False, "the household's last Sim can't leave")
    hm = services.household_manager()
    known = home_id or _moved_out_home.get(int(sim_id))
    home = hm.get(int(known)) if known else None
    if home is None:
        home = hm.create_household(hh.account)
        try:
            home.name = "{}'s place".format(getattr(si, "first_name", "") or "S4OM")
        except Exception:
            pass

    hm.switch_sim_from_household_to_target_household(si, hh, home, destroy_if_empty_household=False)
    try:
        home.set_played_household(True)
    except Exception as e:
        network._log("living_world: couldn't protect household: {}".format(e))

    _moved_out_home[int(sim_id)] = int(home.id)
    _despawn(si)
    return (True, "moved out")


def move_in(sim_id, name="", key=""):
    """Host: a player joined as this Sim."""
    try:
        from multimod import resync

        resync.expect(sim_id)
    except Exception:
        pass

    (ok, msg) = do_move_in(sim_id)
    network._log("living_world: move in {} for {} -> {} ({})".format(sim_id, name or "?", ok, msg))
    if ok and network._is_host:
        if key:
            members = load_members()
            entry = members.get(key) or {"since": (int(time.time()))}
            entry.update(name=(str(name)[:32]), sim=(int(sim_id)))
            members[key] = entry
            save_members(members)
        network._broadcast({"type": "world_move", "dir": "in", "sim": int(sim_id)})
    return (ok, msg)


def move_out(sim_id, reason="left"):
    members = load_members() if network._is_host else {}
    key, member = member_for_sim(members, int(sim_id))
    try:
        from multimod import resync

        resync.expect_out(sim_id)
    except Exception:
        pass

    ok, msg = do_move_out(sim_id, (member or {}).get("home"))
    if not ok:
        try:
            resync._expected_out.discard(int(sim_id))
        except Exception:
            pass

    if ok:
        if key:
            if _moved_out_home.get(int(sim_id)):
                member["home"] = _moved_out_home[int(sim_id)]
                save_members(members)
    network._log("living_world: move out {} ({}) -> {} ({})".format(sim_id, reason, ok, msg))
    if ok:
        if network._is_host:
            network._broadcast({"type": "world_move", "dir": "out", "sim": int(sim_id)})
    return (ok, msg)


def on_world_move(data):
    """Joiner: make the host's move in this game too."""
    try:
        sim_id = int(data.get("sim"))
    except (TypeError, ValueError):
        return
    else:
        if data.get("dir") == "in":
            ok, msg = do_move_in(sim_id)
        else:
            ok, msg = do_move_out(sim_id)
        network._log("living_world: host said {} {} -> {} ({})".format(data.get("dir"), sim_id, ok, msg))


def on_player_left(key=None, name=None):
    """Host: a player left - their Sim moves out (kept in the world)."""
    members = load_members()
    for k, m in members.items():
        if (key and k == key) or (not key and name and m.get("name") == name):
            if m.get("sim") in _household_ids():
                move_out((m["sim"]), reason=("{} left".format(m.get("name"))))
            return


def player_left(name):
    """Any thread (network): remember it; tick() moves their Sim out on the main thread."""
    if name:
        _left.append(str(name))


def player_joined(name):
    if name:
        _joined.append([str(name), time.time() + JOIN_WAIT])


def to_move_out_by_name(members, household_ids, present_names):
    """Session start, matched by player name (the game side doesn't see tokens). Pure."""
    return sorted(
        (
            m["sim"]
            for m in members.values()
            if m.get("sim") in household_ids
            if m.get("name") not in present_names
        )
    )


def present_members(members=None):
    """Names of members who are here for real (see verified_present)."""
    members = load_members() if members is None else members
    return verified_present(members, _present_names(), load_roster())


def _present_names():
    names = set()
    try:
        from multimod import lobby

        names |= {str(m.get("name", "")) for m in lobby._members.values()}
    except Exception:
        pass

    try:
        names |= {str(n) for n in (network._load_launcher_config() or {}).get("expect_players") or []}
    except Exception:
        pass

    return names


_session_written = 0.0


def write_session(path=None):
    r"""Host: who is playing which Sim right now -> Mods\mp_session.json, for the launcher's
    drop-in list (a Sim someone is on can't be picked). {"active": [sim ids], "household": [ids]}"""
    try:
        from multimod import dialog_router, sync

        active = set(dialog_router._owners.keys())
        mine = sync._get_my_sim_id()
        if mine is not None:
            active.add(int(mine))
    except Exception:
        active = set()

    data = {"active": sorted(active), "household": sorted(_household_ids()), "at": int(time.time())}
    path = path or os.path.join(os.path.dirname(network.CMD_PATH), "mp_session.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def tick():
    """Main thread (host): session-start clean-up once per save, members who connected get their
    Sim back in, players who left -> their Sims move out."""
    global _cleaned_slot
    global _session_written
    if not network._is_host:
        return
    if time.time() - _session_written > 5:
        _session_written = time.time()
        try:
            write_session()
        except Exception as e:
            network._log("living_world: session file: {}".format(e))

    slot = _slot()
    if slot and slot != _cleaned_slot and _household_ids():
        _cleaned_slot = slot
        members = load_members(slot)
        for sid in to_move_out_by_name(members, set(_household_ids()), present_members(members)):
            move_out(sid, reason="player not here at session start")

    for entry in list(_joined):
        (name, give_up) = entry
        roster = load_roster()
        (key, m, why) = member_for_name(load_members(), name, roster)
        if roster is not None and name not in roster and time.time() < give_up:
            continue
        _joined.remove(entry)
        if m is None:
            if "isn't the player" in why:
                network._log("living_world: {}".format(why))
                network._notify(
                    "S4OM: a player called {} joined, but they aren't the {} who owns that Sim - the Sim stays where it is.".format(
                        name, name
                    )
                )
            continue
        if m.get("sim") not in _household_ids():
            try:
                move_in(m["sim"], name, key)
            except Exception as e:
                network._log("living_world: move in for {} failed: {}".format(name, e))

    while _left:
        name = _left.pop(0)
        try:
            on_player_left(name=name)
        except Exception as e:
            network._log("living_world: move out for {} failed: {}".format(name, e))


def session_cleanup(present_keys):
    """Host, once per save per session: members still in the household whose player isn't here."""
    global _cleaned_slot
    slot = _slot()
    if not slot or slot == _cleaned_slot:
        return []
    _cleaned_slot = slot
    out = to_move_out(load_members(slot), set(_household_ids()), set(present_keys))
    for sid in out:
        move_out(sid, reason="player not here")

    return out


try:
    import sims4.commands

    @sims4.commands.Command("mp_world_in", command_type=(sims4.commands.CommandType.Live))
    def mp_world_in(sim_id=None, name="", _connection=None):
        out = sims4.commands.CheatOutput(_connection)
        if not sim_id:
            out("usage: mp_world_in <sim id> [player name]")
            return
        out("move in: {}".format(move_in(int(sim_id), name)))

    @sims4.commands.Command("mp_world_out", command_type=(sims4.commands.CommandType.Live))
    def mp_world_out(sim_id=None, _connection=None):
        out = sims4.commands.CheatOutput(_connection)
        if not sim_id:
            out("usage: mp_world_out <sim id>")
            return
        out("move out: {}".format(move_out((int(sim_id)), reason="console")))

    @sims4.commands.Command("mp_world_list", command_type=(sims4.commands.CommandType.Live))
    def mp_world_list(_connection=None):
        out = sims4.commands.CheatOutput(_connection)
        import services

        hh = services.active_household()
        out("household: " + ", ".join(("{} {}".format(si.first_name, si.sim_id) for si in hh.sim_info_gen())))
        others = [si for si in services.sim_info_manager().objects if si.household is not hh][:12]
        out("others: " + ", ".join(("{} {}".format(si.first_name, si.sim_id) for si in others)))
        network._log(
            "living_world list: household {} | others {}".format(
                [(si.first_name, si.sim_id) for si in hh.sim_info_gen()],
                [(si.first_name, si.sim_id) for si in others],
            )
        )

except Exception:
    pass
