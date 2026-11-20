# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\save_index.py
# Compiled at: 2026-10-25 17:49:11
# Size of source mod 2**32: 2933 bytes

r"""Save names for the launcher's save picker.

.save files don't say which household is in them without unpacking the whole save, so while
you play, the mod notes the save's name, household and Sims in Mods\mp_saves.json:

  {"3": {"slot": "Slot_00000003.save", "name": "Willow Creek", "household": "Goth",
         "sims": ["Bella", "Mortimer"], "funds": 18350, "at": 1760000000}}

Written at most every 60 s and only when something changed. The launcher shows these names;
saves never played with S4OM fall back to date + size.
"""

import json, os, time

from multimod import network

INTERVAL = 60.0

MAX_SAVES = 200

_last = 0.0

_last_entry = None


def _path():
    return os.path.join(os.path.dirname(network.CMD_PATH), "mp_saves.json")


def current_entry():
    """Info about the save being played, or None (main menu / loading)."""
    import services

    slot = services.get_persistence_service().get_save_slot_proto_buff()
    hh = services.active_household()
    if slot is None or hh is None:
        return
    sims, detail = [], []
    for si in hh.sim_info_gen():
        name = str(getattr(si, "first_name", "") or "")
        if name:
            sims.append(name[:24])
            last = str(getattr(si, "last_name", "") or "")
            detail.append({"id": int(si.sim_id), "name": (name + (" " + last if last else ""))[:40]})

    sid = int(slot.slot_id)
    return (
        str(sid),
        {
            "slot": ("Slot_{:08x}.save".format)(sid),
            "name": str(slot.slot_name or "")[:60],
            "household": str(getattr(hh, "name", "") or "")[:40],
            "sims": sims[:8],
            "sim_list": detail[:8],
            "funds": int(getattr(getattr(hh, "funds", None), "money", 0) or 0),
        },
    )


def write(key, entry, path=None):
    path = path or _path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    data[key] = dict(entry, at=(int(time.time())))
    if len(data) > MAX_SAVES:
        for k in sorted(data, key=(lambda k: data[k].get("at", 0)))[: len(data) - MAX_SAVES]:
            data.pop(k, None)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def tick(now=None):
    global _last
    global _last_entry
    now = time.time() if now is None else now
    if now - _last < INTERVAL:
        return
    _last = now
    try:
        cur = current_entry()
    except Exception:
        return
    else:
        if cur is None or cur == _last_entry:
            return
        try:
            write(*cur)
            _last_entry = cur
        except Exception as e:
            network._log("save_index: {}".format(e))
