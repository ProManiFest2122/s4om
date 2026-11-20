# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\desync_repair.py
# Compiled at: 2026-10-11 20:01:59
# Size of source mod 2**32: 12692 bytes

"""S4OM desync auto-repair.

The host is the source of truth. Every CHECK_EVERY seconds it broadcasts a small
"state_check": its lot, game speed, household money, where every Sim stands, and a
checksum of the furniture on the lot. Each joiner compares that with its own game and
quietly fixes what drifted:

  - game speed       -> applied straight away (host decides the speed)
  - wrong lot        -> follows the host (same native travel as travel_follow)
  - money            -> host's value, once the difference held still for two checks
                        (a purchase in flight must not be undone)
  - a Sim in the wrong place -> snapped to the host's spot, once the Sim stood still
                        at the host for two checks (walking Sims are never touched)
  - furniture        -> checksum differs for two checks: ask the host for its object
                        list and move every object that's out of place back

Only furniture that exists on both PCs can be fixed this way; walls/floors (build mode)
aren't synced by S4OM, so those differences are just reported once.

Position keyframes (sync.py) fix the other half: every player re-sends their exact
position every few seconds, so one lost delta never leaves a Sim drifting.

Command: mp_repair  - show what was repaired this session / force a check now.
"""

import json, time, zlib

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

CHECK_EVERY = 10.0

SIM_TOLERANCE = 1.5

SIM_STILL = 0.3

FUNDS_TOLERANCE = 50

OBJ_POS_TOLERANCE = 0.05

OBJ_ROT_TOLERANCE = 0.035

OBJ_LIST_COOLDOWN = 60.0

MAX_OBJECTS = 3000

_last_check = 0.0

_seq = 0

_prev = None

_sim_strikes = {}

_funds_seen = None

_zone_strikes = 0

_obj_strikes = 0

_last_obj_req = 0.0

_reported_missing = False

_repairs = {"speed": 0, "zone": 0, "funds": 0, "sims": 0, "objects": 0, "checks": 0}


def _zone_id():
    try:
        import services

        return services.current_zone_id()
    except Exception:
        return


def _speed():
    try:
        from multimod import menus

        if menus.in_build_mode():
            return
    except Exception:
        pass

    try:
        from multimod import clock_sync

        s = clock_sync.get_current_speed()
        if s is not None:
            return int(s)
        return
    except Exception:
        return


def _funds():
    try:
        from multimod import money_sync

        return money_sync.collect_snapshot()
    except Exception:
        return


def _sims():
    try:
        from multimod import sync

        return {str(k): v for k, v in sync.collect_snapshot().items()}
    except Exception:
        return {}


def collect_objects():
    """[[obj_id, x, y, z, rot], ...] for the movable objects on this lot (buy_sync's view)."""
    out = []
    try:
        from multimod import buy_sync

        for obj in buy_sync._iter_lot_objects():
            st = buy_sync._obj_state(obj)
            oid = getattr(obj, "id", 0)
            if st is None or not oid:
                continue
            out.append([int(oid), round(st[0], 2), round(st[1], 2), round(st[2], 2), round(st[3], 3)])
            if len(out) >= MAX_OBJECTS:
                break

    except Exception:
        pass

    out.sort()
    return out


def objects_digest(objs):
    """Count + CRC over ids and coarse positions (coarse so float noise doesn't trip it)."""
    coarse = [[o[0], round(o[1], 1), round(o[2], 1), round(o[3], 1), round(o[4], 1)] for o in objs]
    return [len(objs), zlib.crc32(json.dumps(coarse, separators=(",", ":")).encode()) & 4294967295]


def host_tick(now=None):
    """Called from the main-thread housekeeping tick on the host."""
    global _last_check
    global _seq
    if not (network._is_host and network._clients):
        return
    now = now or time.time()
    if now - _last_check < CHECK_EVERY:
        return
    _last_check = now
    try:
        from multimod import lobby

        if lobby.is_travel_active():
            return
    except Exception:
        pass

    _seq += 1
    msg = {
        "type": "state_check",
        "seq": _seq,
        "ts": now,
        "zone": _zone_id(),
        "speed": _speed(),
        "funds": _funds(),
        "sims": _sims(),
        "objs": objects_digest(collect_objects()),
    }
    network._broadcast(msg)


def on_obj_list_req(sender_pid):
    if not network._is_host or sender_pid not in network._clients:
        return
    objs = collect_objects()
    network._send_json(
        network._clients[sender_pid][0], {"type": "obj_list", "objs": objs, "ts": (time.time)()}
    )
    network._log("desync: sent object list ({} objects) to pid {}".format(len(objs), sender_pid))


def _dist(a, b):
    return sum(((float(a[i]) - float(b[i])) ** 2 for i in range(3))) ** 0.5


def _my_sim_id():
    try:
        from multimod import sync

        sid = sync._get_my_sim_id()
        if sid is not None:
            return str(sid)
        return
    except Exception:
        return


def _snap_sim(sid, pos):
    try:
        from multimod import sync

        sync.process_snapshot({sid: pos})
        sync._delta_base[int(sid)] = list(pos)
        sync._remote_targets.pop(int(sid), None)
    except Exception as e:
        network._log("desync: snap sim {} failed: {}".format(sid, e))


def on_state_check(data, local=None):
    """Compare the host's state with ours and repair. `local` lets tests inject our state."""
    global _funds_seen
    global _last_obj_req
    global _obj_strikes
    global _prev
    global _zone_strikes
    if network._is_host:
        return []
    try:
        from multimod import lobby

        if lobby.is_travel_active():
            _prev = None
            return []
    except Exception:
        pass

    _repairs["checks"] += 1
    loc = local or {
        "zone": _zone_id(),
        "speed": _speed(),
        "funds": _funds(),
        "sims": _sims(),
        "objs": None,
        "me": _my_sim_id(),
    }
    fixed = []
    hz, lz = data.get("zone"), loc.get("zone")
    if hz is not None and lz is not None and hz != lz:
        _zone_strikes += 1
        if _zone_strikes >= 2:
            _zone_strikes = 0
            _repairs["zone"] += 1
            fixed.append("zone")
            network._log("desync: on zone {} but host is on {} - following".format(lz, hz))
            try:
                from multimod import lobby

                lobby.on_travel_follow({"zone_id": hz})
            except Exception as e:
                network._log("desync: follow failed: {}".format(e))

        _prev = data
        return fixed
    _zone_strikes = 0
    hs, ls = data.get("speed"), loc.get("speed")
    if hs is not None and ls is not None and hs != ls:
        try:
            from multimod import clock_sync

            clock_sync.apply_remote_clock(int(hs))
        except Exception:
            pass

        _repairs["speed"] += 1
        fixed.append("speed")
    hf, lf = data.get("funds"), loc.get("funds")
    if isinstance(hf, int) and isinstance(lf, int) and abs(hf - lf) > FUNDS_TOLERANCE:
        if _funds_seen == (hf, lf):
            try:
                from multimod import money_sync

                money_sync.process_message({"type": "money_sync", "funds": hf})
            except Exception:
                pass

            _repairs["funds"] += 1
            fixed.append("funds")
            _funds_seen = None
        else:
            _funds_seen = (hf, lf)
    else:
        _funds_seen = None
    prev_sims = (_prev or {}).get("sims") or {}
    me = loc.get("me")
    for sid, hpos in (data.get("sims") or {}).items():
        lpos = (loc.get("sims") or {}).get(sid)
        if sid == me or lpos is None or hpos is None:
            _sim_strikes.pop(sid, None)
            continue
        still = sid in prev_sims and _dist(prev_sims[sid], hpos) < SIM_STILL
        if still and _dist(hpos, lpos) > SIM_TOLERANCE:
            _sim_strikes[sid] = _sim_strikes.get(sid, 0) + 1
            if _sim_strikes[sid] >= 2:
                _snap_sim(sid, hpos)
                _sim_strikes.pop(sid, None)
                _repairs["sims"] += 1
                fixed.append("sim:" + sid)
                network._log(
                    "desync: sim {} was {:.1f}m off - snapped to host".format(sid, _dist(hpos, lpos))
                )
        else:
            _sim_strikes.pop(sid, None)

    hd = data.get("objs")
    ld = loc.get("objs") if loc.get("objs") is not None else objects_digest(collect_objects())
    if hd and ld and list(hd) != list(ld):
        _obj_strikes += 1
        now = time.time()
        if _obj_strikes >= 2:
            if now - _last_obj_req > OBJ_LIST_COOLDOWN and network._client_socket is not None:
                _obj_strikes = 0
                _last_obj_req = now
                network._send_json(network._client_socket, {"type": "obj_list_req", "ts": now})
                fixed.append("objects?")
    else:
        _obj_strikes = 0
    if any((f in ("zone", "funds") or f.startswith("sim:") for f in fixed)):
        network._log("desync: repaired {}".format(", ".join(fixed)))
    _prev = data
    return fixed


def on_obj_list(data, local=None, apply=None):
    """Host's object list arrived: move every shared object that's out of place."""
    global _reported_missing
    if network._is_host:
        return 0
    host = {o[0]: o for o in (data.get("objs") or [])[:MAX_OBJECTS] if isinstance(o, list) if len(o) == 5}
    mine = {o[0]: o for o in (local if local is not None else collect_objects())}
    if apply is None:
        from multimod import buy_sync

        apply = buy_sync.process_message
    moved = 0
    for oid, h in host.items():
        m = mine.get(oid)
        if m is None:
            continue
        off = _dist(h[1:4], m[1:4]) > OBJ_POS_TOLERANCE or abs(h[4] - m[4]) > OBJ_ROT_TOLERANCE
        if off:
            apply({"type": "object_pos", "obj_id": oid, "x": h[1], "y": h[2], "z": h[3], "rot": h[4]})
            moved += 1

    only_host = len(set(host) - set(mine))
    only_me = len(set(mine) - set(host))
    if moved:
        _repairs["objects"] += moved
        network._notify(
            "S4OM fixed {} piece{} of furniture that had drifted out of sync".format(
                moved, "" if moved == 1 else "s"
            )
        )
    if (only_host or only_me) and not _reported_missing:
        _reported_missing = True
        network._log(
            "desync: {} objects only at the host, {} only here (new purchases / build mode aren't synced - reload the host's save to match)".format(
                only_host, only_me
            )
        )
    network._log("desync: object repair moved {} (host {}, here {})".format(moved, len(host), len(mine)))
    return moved


def summary():
    r = _repairs
    return (
        "Desync checks: {checks}. Repaired - speed {speed}, lot {zone}, money {funds}, Sims {sims}, furniture {objects}.".format
    )(**r)


def reset():
    global _funds_seen
    global _last_check
    global _last_obj_req
    global _obj_strikes
    global _prev
    global _reported_missing
    global _seq
    global _zone_strikes
    (_last_check, _seq, _prev, _funds_seen, _zone_strikes, _obj_strikes, _last_obj_req) = (
        0.0,
        0,
        None,
        None,
        0,
        0,
        0.0,
    )
    _reported_missing = False
    _sim_strikes.clear()
    for k in _repairs:
        _repairs[k] = 0


if sims4 is not None:

    @sims4.commands.Command("mp_repair", command_type=(sims4.commands.CommandType.Live))
    def mp_repair(_connection=None):
        global _last_check
        out = sims4.commands.CheatOutput(_connection)
        if network._is_host:
            _last_check = 0.0
            host_tick()
            out("Sent a desync check to everyone. " + summary())
        else:
            if network._client_socket is not None:
                network._send_json(network._client_socket, {"type": "obj_list_req", "ts": (time.time)()})
            out("Asked the host for a full furniture check. " + summary())
