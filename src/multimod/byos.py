# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\byos.py
# Compiled at: 2026-11-15 21:31:47
# Size of source mod 2**32: 10482 bytes

r"""S4OM bring-your-own-Sim: a player's Sim from their Library (Tray) moves into the host's world.

A Library household is a .householdbinary file in Documents\Electronic Arts\The Sims 4\Tray:
a 16-byte header (the last 4 bytes are the message length), then a message whose field 1 is the game's AccountFamilyData - the same
thing the Gallery hands the game (matchmaking_service.create_candidate_full_sim_info loads
Gallery households into a running game exactly like this).

  - The joiner's launcher sends the file (a few KB) to the host's launcher through the room.
  - The host's game imports ONE Sim from it (fresh sim + household ids, so two players
    bringing the same Gallery Sim never collide), then living_world.move_in() puts them in
    the played household. From then on they're a normal member of that save: on later
    joins the player gets the same Sim back, with everything they did in this world.
  - Look comes from the Library, progress lives in the world.

Console (testing): mp_byos_list, mp_byos_import <file name|path> [sim #] [player name],
mp_byos_testsave (saves to a scratch slot 0xff41, never over the real save)
"""

import glob

from multimod import userdir as _userdir

import os

from multimod import network

HEADER = 16


def _varint(b, i):
    r = s = 0
    while 1:
        c = b[i]
        i += 1
        r |= (c & 127) << s
        s += 7
        if c < 128:
            return (r, i)


def _fields(b):
    """(field, wire type, value) for one protobuf message. Length-delimited values are bytes."""
    i, out = 0, []
    while i < len(b):
        (key, i) = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            (v, i) = _varint(b, i)
        elif wt == 1:
            v, i = int.from_bytes(b[i : i + 8], "little"), i + 8
        elif wt == 5:
            v, i = int.from_bytes(b[i : i + 4], "little"), i + 4
        elif wt == 2:
            (n, i) = _varint(b, i)
            v, i = b[i : i + n], i + n
        else:
            raise ValueError("not a Library household (wire type {})".format(wt))
        if i > len(b):
            raise ValueError("not a Library household (truncated)")
        out.append((f, wt, v))

    return out


def family_bytes(raw):
    """The AccountFamilyData bytes inside a .householdbinary file."""
    if len(raw) < HEADER + 2:
        raise ValueError("not a Library household (too small)")
    n = int.from_bytes(raw[12:16], "little")
    for f, wt, v in _fields(raw[HEADER : HEADER + n]):
        if f == 1:
            if wt == 2:
                return v

    raise ValueError("not a Library household (no family)")


def tray_sims(raw):
    """[{index, id, first, last}] for the Sims in a Library household - no game needed."""
    sims = []
    for f, wt, v in _fields(family_bytes(raw)):
        if f == 6:
            if wt == 2:
                s = {"index": len(sims), "id": 0, "first": "", "last": ""}
                for sf, swt, sv in _fields(v):
                    if sf == 1 and swt == 1:
                        s["id"] = sv
                    else:
                        if sf == 5 and swt == 2:
                            s["first"] = sv.decode("utf-8", "replace")
                        else:
                            if sf == 6:
                                if swt == 2:
                                    s["last"] = sv.decode("utf-8", "replace")

                sims.append(s)

    return sims


def household_name(raw):
    for f, wt, v in _fields(family_bytes(raw)):
        if f == 3 and wt == 2:
            return v.decode("utf-8", "replace")

    return ""


def tray_dir():
    return _userdir.sims_path("Tray")


def library(folder=None):
    """[{file, household, sims}] for every household in the Library. Unreadable ones are skipped."""
    out = []
    for p in sorted(glob.glob(os.path.join(folder or tray_dir(), "*.householdbinary"))):
        try:
            raw = open(p, "rb").read()
            out.append({"file": p, "household": household_name(raw), "sims": tray_sims(raw)})
        except Exception:
            continue

    return out


def _sim_proto(raw, index):
    from protocolbuffers import Sims_pb2

    fam = Sims_pb2.AccountFamilyData()
    fam.MergeFromString(family_bytes(raw))
    return (fam, fam.sim[index] if (0 <= index < len(fam.sim)) else None)


def import_sim(raw, index=0, sim_id=None, hh_id=None):
    """Create one Sim from a Library household in the running game. -> (sim_info, message).
    The host picks fresh ids; every other game imports with the host's ids (byos_import)."""
    import services, id_generator
    from protocolbuffers import Sims_pb2
    from sims.household_manager import HouseholdFixupHelper

    fam = Sims_pb2.AccountFamilyData()
    fam.MergeFromString(family_bytes(raw))
    if not 0 <= index < len(fam.sim):
        return (None, "that household has {} Sim(s)".format(len(fam.sim)))
    one = Sims_pb2.AccountFamilyData()
    one.CopyFrom(fam)
    del one.sim[:]
    sim = one.sim.add()
    sim.CopyFrom(fam.sim[index])
    sim_id, hh_id = sim_id or id_generator.generate_object_id(), hh_id or id_generator.generate_object_id()
    sim.sim_id = sim_id
    sim.household_id = hh_id
    sim.significant_other = 0
    one.familyid = hh_id
    one.familyzoneid = 0
    msg = services.get_persistence_service().add_sim_proto_buff(sim_id)
    msg.MergeFrom(sim)
    hm = services.household_manager()
    fixup = HouseholdFixupHelper()
    household = hm.load_household_from_household_proto(
        (hm.family_info_proto_to_household_proto(one)), fixup_helper=fixup
    )
    fixup.fix_shared_sim_households()
    sim_info = services.sim_info_manager().get(sim_id)
    if sim_info is None:
        return (None, "the game didn't create the Sim")
    sim_info.save_sim()
    household.save_data()
    network._log(
        "byos: imported {} {} as {} (household {})".format(
            sim_info.first_name, sim_info.last_name, sim_id, hh_id
        )
    )
    return (sim_info, "imported")


def apply_look(sim_info, raw, index=0):
    """A returning player: keep their Sim's life in this world, take the look from their Library
    (face, body, skin, voice, outfits) - same calls the game uses for Gallery Sims."""
    from sims.sim_info_base_wrapper import SimInfoBaseWrapper

    _fam, proto = _sim_proto(raw, index)
    if proto is None:
        return False
    base = SimInfoBaseWrapper(first_name=(proto.first_name), sim_id=(proto.sim_id))
    base.load_sim_info(proto)
    SimInfoBaseWrapper.copy_physical_attributes(sim_info, base)
    sim_info.load_outfits(base.save_outfits())
    sim_info.resend_physical_attributes()
    network._log("byos: refreshed {}'s look from their Library".format(sim_info.first_name))
    return True


def bring(raw, index=0, name="", key=""):
    """Host: import the Sim (or, for a returning player, refresh their Sim's look), tell every
    game to do the same, then move them into the played household. -> (ok, msg, sim id)."""
    import base64, services
    from multimod import living_world

    member = living_world.load_members().get(key) if key else None
    sim_info = services.sim_info_manager().get(int(member["sim"])) if (member and member.get("sim")) else None
    if sim_info is not None:
        try:
            apply_look(sim_info, raw, index)
        except Exception as e:
            network._log("byos: look refresh failed: {!r}".format(e))

        look_only = True
    else:
        sim_info, msg = import_sim(raw, index)
        if sim_info is None:
            return (False, msg, 0)
        look_only = False
    network._broadcast(
        {
            "type": "byos_import",
            "household": (base64.b64encode(raw).decode)(),
            "index": int(index),
            "sim": int(sim_info.sim_id),
            "household_id": int(sim_info.household_id),
            "look_only": look_only,
        }
    )
    ok, msg = living_world.move_in(sim_info.sim_id, name, key)
    return (ok, msg, sim_info.sim_id)


def on_byos_import(data):
    """Every other game: the host imported a player's own Sim - import it with the same ids, so
    the move-in that follows finds them here too."""
    import base64, services

    if network._is_host:
        return
    raw = base64.b64decode(str(data.get("household") or ""))
    index, sim_id = int(data.get("index") or 0), int(data.get("sim") or 0)
    existing = services.sim_info_manager().get(sim_id)
    if existing is not None:
        apply_look(existing, raw, index)
    else:
        if not data.get("look_only"):
            import_sim(raw, index, sim_id, int(data.get("household_id") or 0) or None)


def _resolve(arg):
    if os.path.isfile(arg):
        return arg
    hits = [
        p
        for p in glob.glob(os.path.join(tray_dir(), "*.householdbinary"))
        if arg.lower() in os.path.basename(p).lower()
    ]
    if hits:
        return hits[0]


try:
    import sims4.commands

    @sims4.commands.Command("mp_byos_list", command_type=(sims4.commands.CommandType.Live))
    def mp_byos_list(_connection=None):
        out = sims4.commands.CheatOutput(_connection)
        for h in library():
            line = "{} [{}]: {}".format(
                h["household"],
                os.path.basename(h["file"]),
                ", ".join(("#{} {} {}".format(s["index"], s["first"], s["last"]) for s in h["sims"])),
            )
            out(line)
            network._log("byos list: " + line)

    @sims4.commands.Command("mp_byos_import", command_type=(sims4.commands.CommandType.Live))
    def mp_byos_import(arg=None, index=0, name="", _connection=None):
        out = sims4.commands.CheatOutput(_connection)
        p = _resolve(str(arg or ""))
        if not p:
            out("usage: mp_byos_import <file name|part of it> [sim #] [player name]")
            return
        try:
            with open(p, "rb") as f:
                res = bring(f.read(), int(index), name, "console:" + name if name else "")
        except Exception as e:
            res = (False, "error: {}".format(e), 0)
            network._log("byos import failed: {!r}".format(e))

        out("byos: {}".format(res))
        network._log("byos import {} -> {}".format(os.path.basename(p), res))

    @sims4.commands.Command("mp_byos_testsave", command_type=(sims4.commands.CommandType.Live))
    def mp_byos_testsave(_connection=None):
        from multimod import resync

        resync._start_save(65345, "S4OM byos test")
        sims4.commands.CheatOutput(_connection)("byos: saving to Slot_0000ff41.save")

except Exception:
    pass
