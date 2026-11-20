# -*- coding: utf-8 -*-

"""Sims4Multiplayer M3b - 同家庭多 Sim 位置同步 + AI 控制（S4MP 对齐版）

功能目标（对齐 S4MP 联机模型）:
- 同存档 + 同一家庭（household）+ 各控制不同 sim
- 每台电脑广播自己控制的 sim 位置（带 sim_id）
- 接收方按 sim_id 查找对方 sim 实例并移动它（不是移动自己的 active_sim！）
- 效果：A 屏幕上看到 B 控制的小人在动，B 屏幕上看到 A 控制的小人在动

API（反编译确认，2026-08-04）:
- 自己的 sim: services.client_manager().get_first_client().active_sim
- sim_id: sim.sim_info.id
- 按 id 查 sim: services.sim_info_manager().get(sim_id).get_sim_instance()
- 读取位置: sim.position (Vector3)
- 设置位置: sim.location = sim.location.clone(translation=Vector3(x,y,z))

命令:
- mp_sync: 开始/停止广播自己的 sim 位置（带 sim_id）
- mp_self: 显示自己的 sim_id（调试用）
"""

import json, time, sims4.commands

from multimod import network

# ============ 配置 ============

SYNC_INTERVAL = 0.25  # 位置广播间隔（秒） fork: was 0.5 - smoother movement, still tiny traffic

POS_THRESHOLD = 0.3  # 位置变化超过该距离才广播（米，减少网络流量）

def _quantize(v):
    """v9.12: 位置量化（研究: Gaffer 4096 values/meter）——0.01m 精度整数

    把 float 坐标 ×100 取整，传输时用整数（更小 pickle 体积），
    接收端 /100 还原。0.01m 精度远小于人物可见位移，无感知损失。
    """
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0

# ============ v7.0: 快照插值（研究: Gaffer on Games snapshot interpolation） ============
# 接收端不再直接 set location 瞬移，而是缓存目标位置，每 tick 线性插值逼近

INTERP_STEP = 0.1       # 插值 tick（秒，alarm 500ms 内跑 5 步）

INTERP_SPEED = 3.0      # 插值速度（米/秒，接近人物步行速度，太慢会拖尾）

_remote_targets = {}    # sim_id -> (target Vector3, last_update_ts)

_interp_running = False

def _interp_loop():
    """插值循环：每 INTERP_STEP 秒把远端 sim 向目标位置移动一小步"""
    global _interp_running
    while _interp_running:
        try:
            for sim_id, (target, ts) in list(_remote_targets.items()):
                # 目标太旧（>5s）说明已断开，移除
                if time.time() - ts > 5.0:
                    _remote_targets.pop(sim_id, None)
                    continue
                sim = _get_sim_by_id(sim_id)
                if sim is None:
                    continue
                try:
                    cur = sim.position
                    if cur is None:
                        continue
                    dx = target.x - cur.x
                    dy = target.y - cur.y
                    dz = target.z - cur.z
                    dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                    if dist < 0.05:
                        # 已到达（小容差），直接贴合
                        _set_location_lerp(sim, target, None)
                        _remote_targets.pop(sim_id, None)
                        continue
                    step = min(1.0, INTERP_SPEED * INTERP_STEP / dist)
                    _set_location_lerp(sim, target, step)
                except Exception:
                    pass
        except Exception as e:
            network._log("interp error: {}".format(e))
        time.sleep(INTERP_STEP)

def _set_location_lerp(sim, target, step):
    """按比例移动 sim 到目标（step=None 直接到位）"""
    try:
        from sims4.math import Vector3
        if step is None:
            sim.location = sim.location.clone(translation=target)
        else:
            cur = sim.position
            new_x = cur.x + (target.x - cur.x) * step
            new_y = cur.y + (target.y - cur.y) * step
            new_z = cur.z + (target.z - cur.z) * step
            sim.location = sim.location.clone(translation=Vector3(new_x, new_y, new_z))
    except Exception as e:
        network._log("lerp error: {}".format(e))

# ============ M3b: AI 控制（对齐 S4MP autonomy_overrides）============
# 已禁用 AI 的远端 sim 集合（避免重复设置）

_disabled_autonomy_sims = set()

def _disable_sim_autonomy(sim):
    """关闭 sim 自主行为（LIMITED_ONLY = 只做必要交互不乱跑，S4MP 同款）"""
    try:
        from autonomy.settings import AutonomyState
        sim.autonomy_settings.set_setting(AutonomyState.LIMITED_ONLY, sim.get_autonomy_settings_group())
        return True
    except Exception as e:
        network._log("disable autonomy error: {}".format(e))
        return False

def _enable_sim_autonomy(sim):
    """恢复 sim 自主行为（UNDEFINED = 恢复默认）"""
    try:
        from autonomy.settings import AutonomyState
        sim.autonomy_settings.set_setting(AutonomyState.UNDEFINED, sim.get_autonomy_settings_group())
        return True
    except Exception as e:
        network._log("enable autonomy error: {}".format(e))
        return False

def _get_household_sims():
    """获取当前玩家庭的全部 sim 实例（含 active）

    API 反编译确认（sim_info_manager.pyc 内部用法）:
    - services.active_household() → 当前玩家庭
    - household.sim_info_gen() → 遍历 sim_info（不是 .sim_infos！）
    ⚠️ Zone 没有 household_manager 属性（v5.4 实测踩坑）
    """
    try:
        import services
        household = services.active_household()
        if household is None:
            return []
        result = []
        for sim_info in household.sim_info_gen():
            sim = sim_info.get_sim_instance()
            if sim is not None:
                result.append(sim)
        return result
    except Exception as e:
        network._log("get_household error: {}".format(e))
        return []


# ============ 全局状态 ============

_sync_running = False

_last_broadcast_pos = None

_last_dest = None

def _route_dest(sim):
    """[x, y, z] where this Sim's current walk ends, or None when it isn't walking."""
    try:
        path = sim.routing_component.current_path
        loc = path.final_location if path is not None else None
        if loc is None:
            return
        p = getattr(loc, "position", None) or loc.transform.translation
        return [
         p.x, p.y, p.z]
    except Exception:
        return

def _seated(sim):
    """True while the Sim sits, lies, sleeps... (not a standing/walking posture). Its spot then
    belongs to the action (bed, chair), not to position updates - moving it by position put
    Sims STANDING in the bed (live test 2026-10-26)."""
    try:
        p = sim.posture
        return p is not None and not getattr(p, "mobile", True)
    except Exception:
        return False

def dest_changed(old, new, tol=0.5):
    """Pure (tested): did our Sim start a new walk / stop walking?"""
    if (old is None) != (new is None):
        return True
    if old is None:
        return False
    return max((abs(a - b) for (a, b) in zip(old, new))) > tol

_pos_seq = 0  # v7.0: 位置广播序号（丢包检测）

_delta_base = {}  # v8.1: delta 压缩基准（sim_id → 绝对坐标）

def reset_pos_baseline(reason=''):
    """v9.21 P1: 重置位置广播基准，强制下一包发绝对坐标。

    修复「新客机/重连客机永远看不到别人移动」：
    发送端只在 `_pos_seq <= 1` 时发绝对坐标，之后一直发 delta；
    而接收端没有基准时会丢弃 delta（"delta without base"）。
    于是任何在会话中途接入的客机都拿不到基准 → 位置永久不同步，
    且发送端永不自愈（`_last_broadcast_pos` 不会自己清空）。

    在「有新成员加入」「本机重连成功」时调用即可让下一包重新建立基准。
    """
    global _delta_base
    global _last_broadcast_pos
    global _pos_seq
    _last_broadcast_pos = None
    _pos_seq = 0
    _delta_base = {}
    _others.clear()
    for (mod, fields) in (('sim_state_sync', ('_last_sent',)), ('inventory_sync', ('_last_items',)),
                          ('relationship_sync', ('_last_scores', '_last_bits')),
                          ('interaction_sync', ('_seen_interactions',))):
        try:
            m = __import__(("multimod." + mod), fromlist=[mod])
            for f in fields:
                getattr(m, f).clear()

        except Exception:
            pass

    try:
        from multimod import look_sync, object_state_sync, activity_sync
        look_sync.reset()
        object_state_sync.reset()
        activity_sync.reset()
    except Exception:
        pass

    for (mod, field) in (('money_sync', '_last_funds'), ('mood_sync', '_last_broadcast_mood')):
        try:
            setattr(__import__(("multimod." + mod), fromlist=[mod]), field, None)
        except Exception:
            pass

    try:
        network._log("sync: pos baseline reset ({})".format(reason or "manual"))
    except Exception:
        pass

def _get_active_sim():
    """获取当前控制的 sim"""
    try:
        import services
        cm = services.client_manager()
        if cm is None:
            return None
        client = cm.get_first_client()
        if client is None:
            return None
        return client.active_sim  # 反编译确认: active_sim 返回 Sim 实例
    except Exception as e:
        if _get_active_sim.last_err != str(e):
            _get_active_sim.last_err = str(e)
            network._log("get_active_sim error: {}".format(e))
        return None

_get_active_sim.last_err = None

def _get_my_sim_id():
    """获取自己控制的 sim 的 id（用于广播标识）"""
    sim = _get_active_sim()
    if sim is None:
        return None
    try:
        return int(sim.sim_info.id)
    except Exception as e:
        network._log("get_sim_id error: {}".format(e))
        return None

def _get_sim_by_id(sim_id):
    """按 sim_id 查找 sim 实例（S4MP 同款 API）"""
    try:
        import services
        sim_info = services.sim_info_manager().get(int(sim_id))
        if sim_info is None:
            return None
        return sim_info.get_sim_instance()
    except Exception as e:
        network._log("get_sim_by_id error: {}".format(e))
        return None

def _get_sim_position(sim):
    """获取 sim 位置 (Vector3)"""
    try:
        return sim.position
    except Exception as e:
        network._log("get_position error: {}".format(e))
        return None

def _move_sim_to(sim, pos_list):
    """把 sim 移动到目标位置（快速同步，直接 set location）"""
    try:
        from sims4.math import Vector3
        x, y, z = float(pos_list[0]), float(pos_list[1]), float(pos_list[2])
        target = Vector3(x, y, z)
        # 检查是否已在该位置（避免反复设置）
        cur = sim.position
        if cur is not None and (abs(cur.x - x) < POS_THRESHOLD and abs(cur.z - z) < POS_THRESHOLD):
            return
        # 用 clone 保持 routing_surface/楼层（move_to_landing_strip 同款模式）
        sim.location = sim.location.clone(translation=target)
    except Exception as e:
        network._log("move_sim error: {}".format(e))

KEYFRAME_EVERY = 5.0

_last_keyframe = 0.0

OTHERS_KEYFRAME = 5.0

_others = {}

def others_to_send(sims, owners, my_id, last, now, keyframe=OTHERS_KEYFRAME, move=0.3):
    """Pure (tested). sims: [(id, pos, dest)]. Which of the host's other Sims need a position
    report now: moved, started/stopped/changed a walk, or the periodic keyframe."""
    out = []
    for (sid, pos, dest) in sims:
        if sid == my_id or owners.get(sid) not in (None, 0):
            continue
        prev = last.get(sid)
        if prev is None or now - prev[2] >= keyframe or dest_changed(prev[1], dest) or max((abs(a - b) for (a, b) in zip(prev[0], pos))) > move:
            out.append((sid, pos, dest))

    return out

def _send_others(my_id):
    if not (network._is_host and network._clients):
        return
    try:
        import services
        from multimod import dialog_router
        owners = dict(dialog_router._owners)
        sims = []
        for sim in services.sim_info_manager().instanced_sims_gen():
            try:
                p = sim.position
                sims.append((int(sim.sim_info.id), [p.x, p.y, p.z], _route_dest(sim), _seated(sim)))
            except Exception:
                continue

    except Exception:
        return
    else:
        now = time.time()
        seated = {s[0]: s[3] for s in sims}
        for (sid, pos, dest) in others_to_send([s[:3] for s in sims], owners, my_id, _others, now):
            _others[sid] = (
             pos, dest, now)
            network._broadcast({'type':"sim_pos",  'sim_id':sid,  'position':pos,  'dest':dest,  'still':(seated.get)(sid, False), 
             'ts':now,  'seq':0})

def _sync_loop():
    """位置广播循环：每 SYNC_INTERVAL 秒发送一次本机 sim 位置（带 sim_id）

    v8.0: 自适应 tick（研究: Netcode adaptive tick rate）——
    移动快（距离变化>1.5m）用 0.2s，静止用 0.5s，平衡流畅与带宽
    """
    global _last_broadcast_pos
    global _last_dest
    global _last_keyframe
    global _pos_seq
    global _sync_running
    network._log("sync loop started")
    while _sync_running:
        interval = SYNC_INTERVAL
        try:
            try:
                from multimod import lobby
                if lobby.is_travel_active():
                    time.sleep(SYNC_INTERVAL)
                    continue
            except Exception:
                pass

            try:
                rtt = network.get_rtt_ms()
                if rtt is not None:
                    if rtt > 400:
                        interval = SYNC_INTERVAL * 2.0
                    elif rtt > 200:
                        interval = SYNC_INTERVAL * 1.5
            except Exception:
                pass

            sim_id = _get_my_sim_id()
            sim = _get_active_sim()
            try:
                _send_others(sim_id)
            except Exception as e:
                network._log("send others error: {}".format(e))

            if sim is not None:
                if sim_id is not None:
                    if network._is_host or network._client_socket is not None:
                        pos = _get_sim_position(sim)
                        if pos is not None:
                            pos_list = [
                             pos.x, pos.y, pos.z]
                            moved = False
                            if _last_broadcast_pos is not None:
                                dist = ((pos_list[0] - _last_broadcast_pos[0]) ** 2 + (pos_list[1] - _last_broadcast_pos[1]) ** 2 + (pos_list[2] - _last_broadcast_pos[2]) ** 2) ** 0.5
                                if dist > 1.5:
                                    interval = 0.2
                            dest = _route_dest(sim)
                            new_route = dest_changed(_last_dest, dest)
                            keyframe = time.time() - _last_keyframe >= KEYFRAME_EVERY or new_route
                            if keyframe or _last_broadcast_pos is None or abs(pos_list[0] - _last_broadcast_pos[0]) > POS_THRESHOLD or abs(pos_list[1] - _last_broadcast_pos[1]) > POS_THRESHOLD or abs(pos_list[2] - _last_broadcast_pos[2]) > POS_THRESHOLD:
                                _pos_seq += 1
                                if _last_broadcast_pos is not None and _pos_seq > 1 and not keyframe:
                                    payload = {'type':"sim_pos", 
                                     'sim_id':sim_id, 
                                     'delta_q':[
                                      _quantize(pos_list[0] - _last_broadcast_pos[0]),
                                      _quantize(pos_list[1] - _last_broadcast_pos[1]),
                                      _quantize(pos_list[2] - _last_broadcast_pos[2])], 
                                     'ts':(time.time)(), 
                                     'seq':_pos_seq, 
                                     'still':_seated(sim)}
                                else:
                                    _last_keyframe = time.time()
                                    payload = {'type':"sim_pos", 
                                     'sim_id':sim_id, 
                                     'position':pos_list, 
                                     'dest':dest, 
                                     'ts':(time.time)(), 
                                     'seq':_pos_seq, 
                                     'still':_seated(sim)}
                                    _last_dest = dest
                                if network._is_host:
                                    network._broadcast(payload)
                                else:
                                    network._send_json((network._client_socket), payload, prio=2)
                                _last_broadcast_pos = pos_list
                                moved = True
        except Exception as e:
            network._log("sync loop error: {}".format(e))

        time.sleep(interval)

def _ensure_interp():
    """确保插值线程已启动（幂等）"""
    global _interp_running
    try:
        if _interp_running:
            return
        _interp_running = True
        import threading
        threading.Thread(target=_interp_loop, daemon=True).start()
        network._log("interp loop started")
    except Exception as e:
        network._log("interp start error: {}".format(e))

def process_message(data):
    """处理 sim_pos 类型消息（由 network._process_incoming 分发调用）

    M3a: 按 sim_id 查找对方 sim 并移动（不是移动自己的 active_sim！）
    """
    if data.get("type") == "sim_pos":
        sim_id = data.get("sim_id")
        if not sim_id:
            return
        if int(sim_id) == (_get_my_sim_id() or 0):
            return
        if "delta_q" in data:
            base = _delta_base.get(sim_id)
            if base is None:
                network._log("delta without base for sim {}".format(sim_id))
                return
            dq = data["delta_q"]
            pos = [base[0] + dq[0] / 100.0,
             base[1] + dq[1] / 100.0,
             base[2] + dq[2] / 100.0]
            _delta_base[sim_id] = pos
        elif "delta" in data:
            base = _delta_base.get(sim_id)
            if base is None:
                network._log("delta without base for sim {}".format(sim_id))
                return
            pos = [base[0] + data["delta"][0],
             base[1] + data["delta"][1],
             base[2] + data["delta"][2]]
            _delta_base[sim_id] = pos
        elif "position" in data:
            pos = data["position"]
            _delta_base[sim_id] = pos
        else:
            return
        sim = _get_sim_by_id(sim_id)
        try:
            from multimod import npc_sync
            npc_sync.reported(sim_id, pos)
        except Exception:
            pass

        if sim is None:
            return
        if sim_id not in _disabled_autonomy_sims:
            if _disable_sim_autonomy(sim):
                _disabled_autonomy_sims.add(sim_id)
                network._log("auto-disabled AI for remote sim {}".format(sim_id))
        try:
            cur = sim.position
            far = ((cur.x - pos[0]) ** 2 + (cur.z - pos[2]) ** 2) ** 0.5 > 30.0
        except Exception:
            far = False

        if (data.get("still") or _seated(sim)) and not far:
            _remote_targets.pop(int(sim_id), None)
            return
        try:
            from multimod import walk
            if walk.handle(sim, int(sim_id), pos, data.get("dest"), "dest" in data):
                _remote_targets.pop(int(sim_id), None)
                return
        except Exception as e:
            network._log("walk error: {}".format(e))

        try:
            from sims4.math import Vector3
            target = Vector3(float(pos[0]), float(pos[1]), float(pos[2]))
            _remote_targets[int(sim_id)] = (target, time.time())
        except Exception as e:
            network._log("cache target error: {}".format(e))
            _move_sim_to(sim, pos)

        _ensure_interp()
        network._log("target sim {} -> {:.1f},{:.1f},{:.1f}".format(sim_id, pos[0], pos[1], pos[2]))

@sims4.commands.Command("mp_sync", command_type=(sims4.commands.CommandType.Live))
def mp_sync(_connection=None):
    """开始/停止位置同步广播（带 sim_id）

    ⚠️ 兼容 _connection=None（auto-apply 内部线程调用）：CheatOutput(None)
    会崩 "output() argument 2 must be int, not None"，需保护
    """
    global _sync_running

    def _out(msg):
        try:
            if _connection is not None:
                sims4.commands.CheatOutput(_connection)(msg)
        except Exception:
            pass

        network._log("mp_sync: {}".format(msg))

    if _sync_running:
        _sync_running = False
        _out("Position sync stopped")
        return
    if not network._is_host:
        if network._client_socket is None:
            _out("Not connected! Host or join first (mp_host / mp_join)")
            return
    _sync_running = True
    import threading
    threading.Thread(target=_sync_loop, daemon=True).start()
    try:
        from multimod import mood_sync
        if not mood_sync._mood_sync_running:
            mood_sync._mood_sync_running = True
            threading.Thread(target=(mood_sync._mood_loop), daemon=True).start()
    except Exception as e:
        network._log("mood auto start error: {}".format(e))

    for _mod_name in ('interaction_sync', 'inventory_sync', 'relationship_sync', 'buy_sync',
                      'money_sync'):
        try:
            mod = __import__(("multimod." + _mod_name), fromlist=[_mod_name])
            if hasattr(mod, "start"):
                mod.start()
        except Exception as e:
            network._log("{} auto start error: {}".format(_mod_name, e))

    _out("Position sync started (every {}s)".format(SYNC_INTERVAL))

@sims4.commands.Command('mp_self', command_type=sims4.commands.CommandType.Live)
def mp_self(_connection=None):
    """显示自己的 sim_id（调试用，两台对照确认 sim_id 不同 = 各控不同小人）"""
    output = sims4.commands.CheatOutput(_connection)
    sim = _get_active_sim()
    if sim is None:
        output('No active Sim found')
        return
    sim_id = int(sim.sim_info.id)
    sim_name = None
    try:
        sim_name = sim.sim_info.full_name
    except Exception:
        pass
    output('My sim_id: {} ({})'.format(sim_id, sim_name))
    network._log("mp_self: sim_id={} name={}".format(sim_id, sim_name))

@sims4.commands.Command('mp_ai', command_type=sims4.commands.CommandType.Live)
def mp_ai(action="status", _connection=None):
    """控制 AI 自主行为（M3b，对齐 S4MP autonomy_overrides）

    mp_ai off    关闭家庭里非活动 sim 的 AI（防止本地 AI 抢控制乱跑）
    mp_ai on     恢复所有 sim 的 AI
    mp_ai status 查看当前状态
    """
    output = sims4.commands.CheatOutput(_connection)
    action = str(action).lower().strip()

    if action == "off":
        active = _get_active_sim()
        active_id = int(active.sim_info.id) if active is not None else None
        count = 0
        for sim in _get_household_sims():
            sim_id = int(sim.sim_info.id)
            # 跳过自己控制的 sim（保持 AI 正常，玩家控制）
            if active_id is not None and sim_id == active_id:
                continue
            if _disable_sim_autonomy(sim):
                _disabled_autonomy_sims.add(sim_id)
                count += 1
        output('Turned off autonomy for {} inactive Sims'.format(count))
        network._log("mp_ai off: disabled AI for {} sims".format(count))
        return

    if action == "on":
        count = 0
        for sim in _get_household_sims():
            sim_id = int(sim.sim_info.id)
            if _enable_sim_autonomy(sim):
                _disabled_autonomy_sims.discard(sim_id)
                count += 1
        output('Restored autonomy for {} Sims'.format(count))
        network._log("mp_ai on: restored AI for {} sims".format(count))
        return

    # status
    output('AI: disabled on remote Sims = {}'.format(len(_disabled_autonomy_sims)))
    for sim_id in list(_disabled_autonomy_sims)[:5]:
        output("  - sim {}".format(sim_id))
    network._log("mp_ai status: disabled={}".format(len(_disabled_autonomy_sims)))

# ============ v9.17: 登录全量快照（研究: Minecraft chunk / Unreal late joiner / Rune stateSync） ============
def collect_snapshot():
    """收集当前所有 sim 位置快照 → {sim_id: [x, y, z]}（新客户端加入时对齐）"""
    snap = {}
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return snap
        for sim_info in hh.sim_info_gen():
            if sim_info is None:
                continue
            sim = sim_info.get_sim_instance()
            if sim is None:
                continue
            try:
                pos = sim.position
                if pos is not None:
                    snap[int(sim_info.id)] = [round(float(pos.x), 2), round(float(pos.y), 2), round(float(pos.z), 2)]
            except Exception:
                continue
    except Exception:
        pass
    return snap

def process_snapshot(positions):
    """应用快照位置（新客户端加入时 host 发来的全量）"""
    if not isinstance(positions, dict):
        return
    try:
        import services
        for sim_id_str, xyz in positions.items():
            try:
                sim_id = int(sim_id_str)
                sim_info = services.sim_info_manager().get(sim_id)
                if sim_info is None:
                    continue
                sim = sim_info.get_sim_instance()
                if sim is None:
                    continue
                from sims4.math import Vector3
                sim.location = sim.location.clone(translation=Vector3(float(xyz[0]), float(xyz[1]), float(xyz[2])))
                network._log("snapshot: moved sim {} to {}".format(sim_id, xyz))
            except Exception:
                continue
    except Exception as e:
        network._log("snapshot apply error: {}".format(e))
