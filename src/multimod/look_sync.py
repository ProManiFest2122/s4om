# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\look_sync.py
# Compiled at: 2026-10-26 18:23:07
# Size of source mod 2**32: 4623 bytes

"""S4OM looks: outfit changes and Create-a-Sim edits made mid-game show on every PC.

Before: a player changed their Sim's outfit or edited them in CAS and only their own screen
showed it. Now the PC that plays a Sim (sim_state_sync._sims_to_send: the joiner's Sim, or the
host's Sims) checks the look every few seconds; when it changed it sends the whole look - face,
body, skin, outfits, and which outfit is on - and the other PCs apply it with the same calls
the game uses to copy Gallery Sims (sim_info_base_wrapper: facial_attributes, physique, skin,
load_outfits, set_current_outfit, resend_physical_attributes).
"""

import base64, hashlib, time

from multimod import network

CHECK_EVERY = 4.0

_sent = {}

_last = 0.0


def read_look(si):
    outfits = si.save_outfits()
    cur = si.get_current_outfit()
    look = {
        "facial": bytes(si.facial_attributes or b""),
        "physique": str(si.physique or ""),
        "skin": int(si.skin_tone or 0),
        "skin_shift": float(getattr(si, "skin_tone_val_shift", 0) or 0),
        "outfits": (outfits.SerializeToString)(),
        "current": [int(cur[0]), int(cur[1])],
    }
    return look


def fingerprint(look):
    """Pure (tested)."""
    h = hashlib.sha1()
    for k in ("facial", "physique", "skin", "skin_shift", "outfits", "current"):
        v = look.get(k)
        h.update(v if isinstance(v, bytes) else repr(v).encode())

    return h.hexdigest()


def tick(now=None):
    global _last
    now = now or time.time()
    if now - _last < CHECK_EVERY:
        return
    _last = now
    try:
        from multimod import sim_state_sync

        if not sim_state_sync._session():
            return
        for si in sim_state_sync._sims_to_send():
            look = read_look(si)
            fp = fingerprint(look)
            if _sent.get(int(si.id)) == fp:
                continue
            first = int(si.id) not in _sent
            _sent[int(si.id)] = fp
            if first:
                continue
            msg = {
                "type": "sim_look",
                "sim_id": int(si.id),
                "fp": fp,
                "physique": look["physique"],
                "skin": look["skin"],
                "skin_shift": look["skin_shift"],
                "current": look["current"],
                "facial": (base64.b64encode(look["facial"]).decode)(),
                "outfits": (base64.b64encode(look["outfits"]).decode)(),
            }
            network._broadcast(msg)
            network._log("look_sync: sent new look for sim {}".format(si.id))

    except Exception as e:
        network._log("look_sync tick error: {}".format(e))


def reset():
    """A player joined: they loaded the save as it was saved - send current looks again."""
    _sent.clear()


def process_message(data):
    try:
        import services

        si = services.sim_info_manager().get(int(data.get("sim_id", 0)))
        if si is None:
            return
        from multimod import sync

        if int(si.id) == (sync._get_my_sim_id() or 0):
            return
        if fingerprint(read_look(si)) == data.get("fp"):
            return
        si.facial_attributes = base64.b64decode(data.get("facial") or "")
        if data.get("physique"):
            si.physique = data["physique"]
        si.skin_tone = int(data.get("skin") or si.skin_tone)
        try:
            si.skin_tone_val_shift = float(data.get("skin_shift") or 0)
        except Exception:
            pass

        m = si.save_outfits()
        m.Clear()
        m.ParseFromString(base64.b64decode(data.get("outfits") or ""))
        si.load_outfits(m)
        cur = data.get("current")
        if cur:
            si.set_current_outfit((int(cur[0]), int(cur[1])))
        si.resend_physical_attributes()
        _sent[int(si.id)] = data.get("fp")
        network._log("look_sync: applied new look for sim {}".format(si.id))
    except Exception as e:
        network._log("look_sync apply error: {}".format(e))


try:
    import sims4.commands

    @sims4.commands.Command("mp_look_test", command_type=(sims4.commands.CommandType.Live))
    def mp_look_test(category="2", _connection=None):
        """Testing: put the selected Sim into another outfit (2 = athletic) like spinning into it."""
        import services

        si = services.client_manager().get_first_client().active_sim.sim_info
        ok = si.set_current_outfit((int(category), 0))
        si.resend_physical_attributes()
        network._log("look test: outfit -> ({}, 0): {}".format(category, ok))

except Exception:
    pass
