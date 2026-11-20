# -*- coding: utf-8 -*-

"""S4OM Buy mode sync - placing, moving, rotating, buying and deleting furniture.

Every second each player's mod looks at the movable objects on the lot:
  - moved or rotated       -> {type: object_pos, obj_id, x, y, z, rot, level}
  - new object (bought)    -> {type: object_new, obj_id, def_id, x, y, z, rot, level}
  - object gone (sold)     -> {type: object_del, obj_id}
Others apply it. Objects bought on another PC get a different id here, so ids are
mapped (_remote_ids: sender's id -> our id) and later moves/deletes of that object
find the right one. Walls/floors/rooms are build mode - see resync.py.

Game APIs (decompiled): objects.system.create_object(definition_id), obj.definition.id,
obj.orientation + sims4.math.yaw_quaternion_to_angle / angle_to_yaw_quaternion,
routing.SurfaceIdentifier(zone_id, level, SURFACETYPE_WORLD), obj.destroy(source, cause).
(The old code rotated with sims4.math.yaw_to_quaternion, which doesn't exist - rotations
never synced.)

Command: mp_buysync - turn Buy mode sync on/off.
"""

import threading, time

try:
    import sims4.commands
except Exception:
    sims4 = None

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 1.0       # 家具变化中等，1s 轮询

MOVE_THRESHOLD = 0.05     # 位置变化 >5cm 或旋转变化 >2° 才广播

ROT_THRESHOLD = 0.035

_IGNORED_TYPES = ("Floor", "Wall", "Stairs", "Roof", "Fence", "Column")  # 建筑类不同步

_running = False

_thread = None

_last_objs = {}           # obj_id -> (x, y, z, rot)

_remote_ids = {}

_local_to_remote = {}

_baseline_done = False

_hold = False

_lock = threading.Lock()

def hold(on):
    global _hold
    _hold = bool(on)

_zone_down = False

MASS_DEL_MIN = 25

MASS_DEL_SHARE = 0.5

def lot_down():
    """world_flow: our lot is tearing down - send nothing until the next lot has loaded."""
    global _baseline_done
    global _zone_down
    with _lock:
        _zone_down = True
        _baseline_done = False

def lot_up():
    """world_flow: a lot finished loading - its objects are the new baseline, not purchases."""
    global _baseline_done
    global _zone_down
    with _lock:
        _last_objs.clear()
        _remote_ids.clear()
        _local_to_remote.clear()
        _baseline_done = False
        _zone_down = False

def is_mass_delete(previous, events):
    """Pure (tested): most of the lot vanished in one poll = unload, never forward it."""
    dels = sum((1 for k, _, _ in events if k == "del"))
    return dels >= MASS_DEL_MIN and dels >= MASS_DEL_SHARE * max(1, len(previous))

def _zone_running():
    try:
        import services
        zone = services.current_zone()
        if zone is None:
            return False
        return bool(getattr(zone, "is_zone_running", True))
    except Exception:
        return True

def rebaseline(host_objs):
    """lot_live placed the host's lot here: every object has a new id. Learn which of ours is
    which of the host's (same type, same spot) and take the lot as it is now as the baseline.
    Returns how many were matched."""
    from multimod import lot_live
    current, local = {}, []
    for obj in _iter_lot_objects():
        st = _obj_state(obj)
        if st is not None:
            current[int(obj.id)] = st
            local.append((int(obj.id), int(st[5]), st[0], st[1], st[2]))

    pairs = lot_live.match_objects([tuple(o) for o in host_objs], local)
    with _lock:
        _last_objs.clear()
        _last_objs.update(current)
        _remote_ids.clear()
        _local_to_remote.clear()
        for hid, lid in pairs.items():
            _remote_ids[hid] = lid
            _local_to_remote[lid] = hid

    return len(pairs)

def _out(msg, _connection=None):
    try:
        if _connection is not None:
            sims4.commands.CheatOutput(_connection)(msg)
    except Exception:
        pass
    if network is not None:
        try:
            network._log(msg)
        except Exception:
            pass

def _iter_lot_objects():
    """Movable objects on the current lot (no Sims, no build-mode pieces)."""
    try:
        import services
        mgr = services.object_manager()
        if mgr is None:
            return []
        objs = []
        for obj in mgr.get_valid_objects_gen():
            if obj is None or getattr(obj, "is_sim", False):
                continue
            try:
                if not obj.is_on_active_lot():
                    continue
            except Exception:
                continue

            if any((t in obj.__class__.__name__ for t in _IGNORED_TYPES)):
                continue
            objs.append(obj)

        return objs
    except Exception as e:
        if network is not None:
            network._log("buy_sync: can't list lot objects: {}".format(e))
        return []

def _obj_state(obj):
    """(x, y, z, rot, level, def_id) or None."""
    try:
        pos = getattr(obj, "position", None)
        if pos is None:
            return
        rot = 0.0
        try:
            import sims4.math
            rot = float(sims4.math.yaw_quaternion_to_angle(obj.orientation))
        except Exception:
            rot = 0.0

        level = 0
        try:
            level = int(obj.routing_surface.secondary_id)
        except Exception:
            level = 0

        def_id = 0
        try:
            def_id = int(obj.definition.id)
        except Exception:
            def_id = 0

        return (
         float(pos.x), float(pos.y), float(pos.z), rot, level, def_id)
    except Exception:
        return

def _moved(prev, cur):
    dx = abs(prev[0] - cur[0]) + abs(prev[1] - cur[1]) + abs(prev[2] - cur[2])
    level_changed = len(prev) > 4 and len(cur) > 4 and prev[4] != cur[4]
    return dx >= MOVE_THRESHOLD or abs(prev[3] - cur[3]) >= ROT_THRESHOLD or level_changed

def diff(previous, current, baseline_done=True):
    """Events from two snapshots {obj_id: state}. Pure - tested without the game."""
    events = []
    for (oid, st) in current.items():
        prev = previous.get(oid)
        if prev is None:
            if baseline_done:
                events.append(("new", oid, st))
        else:
            if _moved(prev, st):
                events.append(("pos", oid, st))

    for oid in previous:
        if oid not in current:
            events.append(("del", oid, None))

    return events

def _send(msg):
    if network is None:
        return
    try:
        if network._is_host:
            network._broadcast(msg)
        else:
            if network._client_socket is not None:
                network._send_json(network._client_socket, msg)
    except Exception:
        pass

def _wire_id(local_id):
    """The id the other PCs know this object by."""
    return _local_to_remote.get(local_id, local_id)

def poll_once():
    global _baseline_done
    if _zone_down or not _zone_running():
        return
    current = {}
    for obj in _iter_lot_objects():
        st = _obj_state(obj)
        if st is not None:
            current[int(getattr(obj, "id", 0))] = st

    with _lock:
        if _zone_down:
            return
        events = [] if _hold else diff(_last_objs, current, _baseline_done)
        if events:
            if is_mass_delete(_last_objs, events):
                if network is not None:
                    network._log("buy_sync: {} of {} objects gone at once - lot unloading, not sending".format(sum((1 for (k, _, _) in events if k == "del")), len(_last_objs)))
                _remote_ids.clear()
                _local_to_remote.clear()
                _last_objs.clear()
                _baseline_done = False
                return
        _last_objs.clear()
        _last_objs.update(current)
        _baseline_done = bool(current) or _baseline_done
    for (kind, oid, st) in events:
        wid = _wire_id(oid)
        if kind == "pos":
            _send({'type':"object_pos",  'obj_id':wid,  'x':st[0],  'y':st[1],  'z':st[2],  'rot':st[3],  'level':st[4], 
             'ts':(time.time)()})
        elif kind == "new" and st[5]:
            _send({'type':"object_new",  'obj_id':wid,  'def_id':st[5],  'x':st[0],  'y':st[1],  'z':st[2],  'rot':st[3], 
             'level':st[4],  'ts':(time.time)()})
        elif kind == "del":
            _send({'type':"object_del",  'obj_id':wid,  'ts':(time.time)()})
            _local_to_remote.pop(oid, None)

def _broadcast_loop():
    global _running
    while _running:
        try:
            poll_once()
        except Exception as e:
            if network is not None:
                if str(e) != getattr(_broadcast_loop, "_err", None):
                    _broadcast_loop._err = str(e)
                    network._log("{} loop error: {!r}".format(__name__.split(".")[-1], e))

        for _ in range(int(SYNC_INTERVAL / 0.05)):
            if not _running:
                return
            else:
                time.sleep(0.05)

def _local_obj(remote_id):
    import services
    om = services.object_manager()
    oid = _remote_ids.get(remote_id, remote_id)
    if om is not None:
        return om.get(oid)

def _location(x, y, z, rot, level):
    import services, routing, sims4.math
    surface = routing.SurfaceIdentifier(services.current_zone_id(), int(level or 0), routing.SurfaceType.SURFACETYPE_WORLD)
    return sims4.math.Location(sims4.math.Transform(sims4.math.Vector3(x, y, z), sims4.math.angle_to_yaw_quaternion(rot)), surface)

def _remember(obj):
    st = _obj_state(obj)
    if st is not None:
        with _lock:
            _last_objs[int(obj.id)] = st

def process_message(data):
    """object_pos / object_new / object_del from another player."""
    try:
        t = data.get("type", "object_pos")
        rid = int(data.get("obj_id", 0))
        if not rid:
            return
        if t == "object_del":
            obj = _local_obj(rid)
            if obj is not None:
                with _lock:
                    _last_objs.pop(int(obj.id), None)
                oid = int(obj.id)
                obj.destroy(source=obj, cause="S4OM: removed by another player")
                network._log("buy_sync: removed {}".format(oid))
            _remote_ids.pop(rid, None)
            return
        x, y, z = float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("z", 0))
        rot, level = float(data.get("rot", 0)), int(data.get("level", 0) or 0)
        obj = _local_obj(rid)
        if t == "object_new":
            if obj is None:
                from objects.system import create_object
                obj = create_object(int(data["def_id"]))
                if obj is None:
                    return
                _remote_ids[rid] = int(obj.id)
                _local_to_remote[int(obj.id)] = rid
                try:
                    import build_buy
                    obj.location = _location(x, y, z, rot, level)
                    build_buy.add_object_to_buildbuy_system(obj.id)
                except Exception as e:
                    network._log("buy_sync place error: {}".format(e))

                _remember(obj)
                network._log("buy_sync: placed def {} as {} at {:.1f},{:.1f}".format(data.get("def_id"), int(obj.id), x, z))
                return
        if obj is None:
            return
        obj.location = _location(x, y, z, rot, level)
        _remember(obj)
        network._log("buy_sync: moved {} to {:.1f},{:.1f}".format(int(obj.id), x, z))
    except Exception as e:
        if network is not None:
            try:
                network._log("buy_sync apply error: {}".format(e))
            except Exception:
                pass

if sims4 is not None:

    @sims4.commands.Command("mp_buysync", command_type=(sims4.commands.CommandType.Live))
    def mp_buysync(*args, _connection=None):
        if _running:
            stop()
        else:
            start()
        _out("mp_buysync: buy-mode furniture sync {}".format("on" if _running else "off"), _connection)

if sims4 is not None:

    @sims4.commands.Command("mp_buy_test", command_type=(sims4.commands.CommandType.Live))
    def mp_buy_test(action='list', n=0, _connection=None):
        """Testing: 'list' logs the lot's furniture; 'copy <n>' places a copy of item n 1 m away
        (same as buying it); 'move <n>' nudges item n 1 m; 'del <n>' deletes item n."""
        objs = sorted((o for o in _iter_lot_objects()), key=(lambda o: o.id))
        if action == "list":
            for (i, o) in enumerate(objs[:40]):
                st = _obj_state(o)
                network._log("buy test: #{} id {} def {} at {}".format(i, o.id, st[5] if st else "?", st[:3] if st else "?"))

            _out("buy test: {} objects listed in the log".format(len(objs)), _connection)
            return
        o = objs[int(n)]
        st = _obj_state(o)
        if action == "copy":
            import objects.system
            new = objects.system.create_object(st[5])
            new.location = _location(st[0] + 1.0, st[1], st[2], st[3], st[4])
            network._log("buy test: copied def {} -> new id {}".format(st[5], new.id))
        elif action == "move":
            o.location = _location(st[0] + 1.0, st[1], st[2], st[3], st[4])
            network._log("buy test: moved id {}".format(o.id))
        elif action == "del":
            o.destroy(source=o, cause="S4OM buy test")
            network._log("buy test: deleted id {}".format(o.id))

def start():
    global _baseline_done
    global _running
    global _thread
    if _running:
        return
    _running = True
    _baseline_done = False
    _thread = threading.Thread(target=_broadcast_loop, daemon=True)
    _thread.start()

def stop():
    global _running
    _running = False
