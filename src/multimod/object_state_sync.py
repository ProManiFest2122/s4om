# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\object_state_sync.py
# Compiled at: 2026-10-26 18:32:47
# Size of source mod 2**32: 4068 bytes

"""S4OM object states: broken, dirty, repaired, burnt, spoiled... look the same on every PC.

The host's game is the truth (it runs the household's world). Every few seconds it reads the
states that players notice on lot objects (objects/components/state.py: state_component.items()
-> {ObjectState: ObjectStateValue}) and sends the ones that changed; joiners set the same value
on their copy (buy_sync maps ids of objects bought during play).
"""

import time

from multimod import network

CHECK_EVERY = 3.0

KEYWORDS = ("broken", "dirt", "clean", "burn", "spoil", "fresh", "fire", "repair", "wear", "puddle", "rot")

_sent = {}

_last = 0.0


def interesting(name):
    """Pure (tested): states players can see (not the hundreds of internal ones)."""
    n = (name or "").lower()
    return any((k in n for k in KEYWORDS))


def read_states(obj):
    out = {}
    comp = getattr(obj, "state_component", None)
    if comp is None:
        return out
    for state, value in comp.items():
        try:
            if interesting(getattr(state, "__name__", "")):
                out[str(int(state.guid64))] = int(value.guid64)
        except Exception:
            continue

    return out


def tick(now=None):
    global _last
    now = now or time.time()
    if not network._is_host or not network._clients or now - _last < CHECK_EVERY:
        return
    _last = now
    try:
        from multimod import buy_sync

        for obj in buy_sync._iter_lot_objects():
            st = read_states(obj)
            oid = int(obj.id)
            prev = _sent.get(oid)
            _sent[oid] = st
            if prev is None or prev == st:
                continue
            changed = {k: v for k, v in st.items() if prev.get(k) != v}
            network._broadcast(
                {"type": "object_state", "obj_id": (buy_sync._wire_id)(oid), "states": changed}
            )
            network._log("object_state: {} {}".format(oid, changed))

    except Exception as e:
        network._log("object_state tick error: {}".format(e))


def reset():
    _sent.clear()


def process_message(data):
    if network._is_host:
        return
    try:
        import services, sims4.resources
        from multimod import buy_sync

        obj = buy_sync._local_obj(int(data.get("obj_id", 0)))
        if obj is None or getattr(obj, "state_component", None) is None:
            return
        mgr = services.get_instance_manager(sims4.resources.Types.OBJECT_STATE)
        for sg, vg in (data.get("states") or {}).items():
            state, value = mgr.get(int(sg)), mgr.get(int(vg))
            if state is not None:
                if value is not None:
                    if obj.has_state(state):
                        obj.set_state(state, value, from_sync=True)

        network._log("object_state: applied {} on {}".format(data.get("states"), obj.id))
    except Exception as e:
        network._log("object_state apply error: {}".format(e))


try:
    import sims4.commands

    @sims4.commands.Command("mp_state_test", command_type=(sims4.commands.CommandType.Live))
    def mp_state_test(_connection=None):
        """Testing: flip the first visible state (broken/dirty...) of a lot object to another value."""
        from multimod import buy_sync

        for obj in buy_sync._iter_lot_objects():
            comp = getattr(obj, "state_component", None)
            if comp is None:
                continue
            for state, value in list(comp.items()):
                vals = list(getattr(state, "values", ()) or ())
                if interesting(getattr(state, "__name__", "")):
                    if len(vals) > 1:
                        new = next((v for v in vals if v is not value))
                        obj.set_state(state, new)
                        network._log(
                            "state test: {} {} {} -> {}".format(
                                obj.id, state.__name__, value.__name__, new.__name__
                            )
                        )
                        return

        network._log("state test: no object with a visible state")

except Exception:
    pass
