# -*- coding: utf-8 -*-

"""Sims4Multiplayer v9.17 - 关系同步（🥉 依赖交互同步先做，否则关系变化少）

研究（2026-08-05）:
- relationships/relationship_tracker.pyc 反编译成功确认:
  get_relationship_score / set_relationship_score / add_relationship_score
  get_all_bits / has_bit / add_relationship_bit(force_add) / remove_relationship_bit
- 关系是双向的（A→B 和 B→A）→ 两边都同步
- 关系 bits（朋友/恋人/家人等）用 tuning 资源 ID 标识

功能:
  轮询家庭内 sim 两两之间的关系（分数 + bits）
  → 变化广播 {type:"relationship", sim_a, sim_b, score, track, bits:[...]}
  → 接收端 set_relationship_score + add_relationship_bit
  → 双方看到的关系一致（A 和 B 成为朋友，两边都显示）

命令:
  mp_relsync  开始/停止关系同步
"""

import threading, time, sims4.commands, sims4.resources

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 5.0       # 关系变化慢，5s 轮询

SCORE_THRESHOLD = 2.0     # 分数变化 >2 才广播

_APPLY_THRESHOLD = 3.0    # 接收端分数差异 >3 才应用（防互相打架）

_running = False

_thread = None

_last_scores = {}         # (a,b) -> score 上次广播的分数

_last_bits = {}           # (a,b) -> frozenset(bits)

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

def _family_sim_ids():
    """Household Sims + every Sim on the lot (their relationships with townies/visitors too)."""
    try:
        import services
        hh = services.active_household()
        ids = [si.id for si in hh.sim_info_gen() if si is not None] if hh is not None else []
        others = [int(s.sim_info.id) for s in services.sim_info_manager().instanced_sims_gen()]
        return (
         ids, sorted(set(ids) | set(others)))
    except Exception:
        return ([], [])

def _read_relationship(sim_info_a, sim_info_b):
    """A -> B: ({track guid: value}, sorted bit guids). Every track - friendship, romance,
    and pack ones - not just the default one (fork 2026-10-26)."""
    tracks, bits = {}, []
    try:
        tracker = getattr(sim_info_a, "relationship_tracker", None)
        if tracker is None:
            return ({}, ())
        for tr in tracker.relationship_tracks_gen(sim_info_b.id):
            try:
                tracks[str(int(tr.guid64))] = round(float(tr.get_value()), 1)
            except Exception:
                continue

        for bit in tracker.get_all_bits(sim_info_b.id):
            try:
                bits.append(str(int(bit.guid64)))
            except Exception:
                continue

    except Exception:
        pass

    return (
     tracks, tuple(sorted(bits)))

def rel_plan(local_tracks, local_bits, remote_tracks, remote_bits, tol=1.0):
    """Pure (tested): (tracks to set {guid: value}, bits to add, bits to remove)."""
    set_t = {g: v for g, v in (remote_tracks or {}).items() if g not in (local_tracks or {}) or abs(float(v) - float(local_tracks[g])) >= tol}
    rb, lb = set((str(b) for b in remote_bits or ())), set((str(b) for b in local_bits or ()))
    return (
     set_t, sorted(rb - lb), sorted(lb - rb))

def _broadcast_loop():
    """Host only: the host's game is the one truth for relationships (fork 2026-10-26 - both
    games used to send every pair and fight over the score)."""
    global _running
    while _running:
        try:
            if network is not None:
                if network._is_host:
                    if network._clients:
                        import services
                        (household, everyone) = _family_sim_ids()
                        for a in household:
                            sia = services.sim_info_manager().get(a)
                            if sia is None:
                                continue
                            for b in everyone:
                                if a == b:
                                    continue
                                sib = services.sim_info_manager().get(b)
                                if sib is None:
                                    continue
                                for (x, y, sx, sy) in ((a, b, sia, sib), (b, a, sib, sia)):
                                    (tracks, bits) = _read_relationship(sx, sy)
                                    if not tracks and not bits:
                                        continue
                                    key = (
                                     x, y)
                                    if _last_scores.get(key) != tracks or _last_bits.get(key) != bits:
                                        _last_scores[key] = tracks
                                        _last_bits[key] = bits
                                        _broadcast(x, y, tracks, bits)

        except Exception as e:
            if network is not None:
                network._log("relationship_sync send error: {}".format(e))

        for _ in range(int(SYNC_INTERVAL / 0.05)):
            if not _running:
                return
            else:
                time.sleep(0.05)

def _broadcast(sim_a, sim_b, tracks, bits):
    if network is None:
        return
    try:
        msg = {'type':"relationship", 
         'sim_a':sim_a,  'sim_b':sim_b,  'tracks':tracks,  'bits':list(bits), 
         'ts':(time.time)()}
        if network._is_host:
            network._broadcast(msg)
    except Exception:
        pass

def process_message(data):
    """Joiners: make A -> B match the host exactly (tracks, bits added AND removed)."""
    try:
        if network is not None and network._is_host:
            return
        sim_a, sim_b = int(data.get("sim_a", 0)), int(data.get("sim_b", 0))
        if not (sim_a and sim_b):
            return
        import services
        sia = services.sim_info_manager().get(sim_a)
        if sia is None or services.sim_info_manager().get(sim_b) is None:
            return
        tracker = getattr(sia, "relationship_tracker", None)
        if tracker is None:
            return
        (lt, lb) = _read_relationship(sia, services.sim_info_manager().get(sim_b))
        (set_t, add_b, rem_b) = rel_plan(lt, lb, data.get("tracks") or {}, data.get("bits") or [])
        stats = services.get_instance_manager(sims4.resources.Types.STATISTIC)
        for (g, v) in set_t.items():
            tt = stats.get(int(g))
            if tt is not None:
                tracker.set_relationship_score(sim_b, (float(v)), track=tt)

        bit_mgr = services.get_instance_manager(sims4.resources.Types.RELATIONSHIP_BIT)
        for g in add_b:
            bit = bit_mgr.get(int(g))
            if bit is not None:
                tracker.add_relationship_bit(sim_b, bit, force_add=True)

        for g in rem_b:
            bit = bit_mgr.get(int(g))
            if bit is not None:
                tracker.remove_relationship_bit(sim_b, bit)

        if set_t or add_b or rem_b:
            network._log("relationship_sync: {} -> {} tracks {} +bits {} -bits {}".format(sim_a, sim_b, set_t, add_b, rem_b))
    except Exception as e:
        if network is not None:
            try:
                network._log("relationship_sync apply error: {}".format(e))
            except Exception:
                pass

@sims4.commands.Command('mp_relsync', command_type=sims4.commands.CommandType.Live)
def mp_relsync(*args, _connection=None):
    global _running, _thread
    if _running:
        _running = False
        if _thread is not None:
            _thread.join(timeout=2)
            _thread = None
    else:
        _running = True
        _thread = threading.Thread(target=_broadcast_loop, daemon=True)
        _thread.start()
    _out('mp_relsync: relationship sync {}'.format('on' if _running else 'off'), _connection)

def start():
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_broadcast_loop, daemon=True)
    _thread.start()

def stop():
    global _running
    _running = False
