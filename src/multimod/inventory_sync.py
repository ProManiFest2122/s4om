# -*- coding: utf-8 -*-

"""Sims4Multiplayer v9.17 - 背包同步（🥈 摸对方背包/共享物品）

研究（2026-08-05）:
- objects/components/inventory.pyc 反编译确认: InventoryComponent
  __iter__/__len__ 读物品 + player_try_add_object/system_add_object 写
  + try_remove_object_by_id 删除
- sims/sim.pyc 确认: sim.inventory_component 属性（SimInventoryComponent）
- 两端同存档 → 物品对象 ID 天然对齐

功能:
  轮询本机控制 sim 的背包物品列表（definition_id -> count）
  → 变化广播 {type:"inventory", sim_id, items:{def_id:count}}
  → 接收端对比 → 缺的补加 / 多的删除
  → 双方背包一致（拿到对方给的物品）

命令:
  mp_invsync  开始/停止背包同步
"""

import threading, time, sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 3.0       # 背包变化较慢，3s 轮询

_running = False

_thread = None

_last_items = {}          # sim_id -> {def_id: count} 上次广播的快照

_applied_ts = {}

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

def _iter_sims():
    """遍历家庭内所有有背包的 sim 实例"""
    try:
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        hh = services.active_household()
        if hh is None:
            return []
        sims = []
        for sim_info in hh.sim_info_gen():
            if sim_info is None:
                continue
            sim = sim_info.get_sim_instance()
            if sim is not None and getattr(sim, "inventory_component", None) is not None:
                sims.append(sim)
        return sims
    except Exception:
        return []

def _stack(obj):
    """How many items this inventory slot holds. stack_count is a METHOD in the game; reading it
    as a number made every item raise and get skipped - inventories always looked empty."""
    c = getattr(obj, "stack_count", 1)
    try:
        c = c() if callable(c) else c
    except Exception:
        c = 1

    return int(c or 1)

def collect_inventory(sim):
    """收集 sim 背包 → {def_id: count}"""
    items = {}
    try:
        inv = sim.inventory_component
        for obj in inv:
            try:
                if obj is None:
                    continue
                def_id = getattr(getattr(obj, "definition", None), "id", 0)
                if not def_id:
                    continue
                count = _stack(obj)
                items[def_id] = items.get(def_id, 0) + count
            except Exception:
                continue

    except Exception:
        pass

    return items

def _broadcast_loop():
    global _running
    while _running:
        try:
            try:
                from multimod import dialog_router, interaction_sync
                owners = dict(dialog_router._owners)
                me = interaction_sync._get_my_sim()
                my_id = getattr(getattr(me, "sim_info", None), "id", None)
            except Exception:
                owners, my_id = {}, None

            for sim in _iter_sims():
                sim_id = getattr(getattr(sim, "sim_info", None), "id", 0)
                if not interaction_sync.owns(sim_id, network._is_host, owners, my_id):
                    continue
                items = collect_inventory(sim)
                prev = _last_items.get(sim_id, {})
                if items != prev:
                    _last_items[sim_id] = items
                    _broadcast(sim_id, items)

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

def _broadcast(sim_id, items):
    if network is None:
        return
    try:
        msg = {"type": "inventory", "sim_id": sim_id, "items": items, "ts": time.time()}
        if network._is_host:
            network._broadcast(msg)
        elif network._client_socket is not None:
            network._send_json(network._client_socket, msg)
    except Exception:
        pass

def inventory_plan(local, remote, cap=10):
    """Pure (tested): what to add / remove here so this Sim's bag matches the owner's.
    {def_id: count} both ways; at most `cap` of one item per update (no item storms)."""
    remote = {int(k): int(v) for k, v in (remote or {}).items()}
    local = {int(k): int(v) for k, v in (local or {}).items()}
    add = {d: min(cap, n - local.get(d, 0)) for d, n in remote.items() if n > local.get(d, 0)}
    rem = {d: min(cap, n - remote.get(d, 0)) for d, n in local.items() if n > remote.get(d, 0)}
    return (
     add, rem)

def _apply_inventory(sim, remote_items):
    """Make this Sim's inventory match the owner's PC (fork 2026-10-26 rewrite: the old code called
    object_manager.create_new_object - not a game API - so nothing was ever added, only acted on
    3+ differences, and on smaller ones sent ITS OWN stale bag back, undoing the owner's change)."""
    try:
        (add, rem) = inventory_plan(collect_inventory(sim), remote_items)
        if not add:
            if not rem:
                return
        inv = sim.inventory_component
        for (def_id, count) in rem.items():
            for obj in list(inv):
                if count <= 0:
                    break
                if obj is not None:
                    if getattr(getattr(obj, "definition", None), "id", 0) == def_id:
                        n = min(count, _stack(obj))
                        inv.try_remove_object_by_id((obj.id), count=n)
                        count -= n

        from objects.system import create_object
        for (def_id, count) in add.items():
            for _ in range(count):
                obj = create_object(def_id)
                if obj is None:
                    break
                if not inv.player_try_add_object(obj):
                    obj.destroy(source=obj, cause="S4OM: couldn't put the item in the inventory")
                    break

        network._log("inventory_sync: sim {} +{} -{}".format(getattr(getattr(sim, "sim_info", None), "id", 0), add, rem))
    except Exception as e:
        if network is not None:
            try:
                network._log("inventory_sync apply error: {}".format(e))
            except Exception:
                pass

def process_message(data):
    """收到远端背包状态 → 应用"""
    try:
        sim_id = int(data.get("sim_id", 0))
        items = data.get("items", {})
        if not isinstance(items, dict):
            return
        import services
        sim_info = services.sim_info_manager().get(sim_id)
        if sim_info is None:
            return
        sim = sim_info.get_sim_instance()
        if sim is None or getattr(sim, "inventory_component", None) is None:
            return
        try:
            from multimod import interaction_sync
            me = interaction_sync._get_my_sim()
            if me is not None:
                if int(me.sim_info.id) == sim_id:
                    return
        except Exception:
            pass

        _apply_inventory(sim, items)
    except Exception as e:
        if network is not None:
            network._log("inventory_sync error: {}".format(e))

@sims4.commands.Command('mp_invsync', command_type=sims4.commands.CommandType.Live)
def mp_invsync(*args, _connection=None):
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
    _out('mp_invsync: inventory sync {}'.format('on' if _running else 'off'), _connection)

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

@sims4.commands.Command("mp_inv_test", command_type=(sims4.commands.CommandType.Live))
def mp_inv_test(def_id=None, _connection=None):
    """Testing: log the selected Sim's inventory; with a definition id, put one in it first."""
    try:
        import services
        sim = services.client_manager().get_first_client().active_sim
        if def_id == "auto":
            for o in services.object_manager().get_valid_objects_gen():
                if getattr(o, "inventoryitem_component", None) is not None:
                    if not getattr(o, "is_sim", False):
                        def_id = int(o.definition.id)
                        break

        if def_id:
            from objects.system import create_object
            obj = create_object(int(def_id))
            ok = sim.inventory_component.player_try_add_object(obj) if obj is not None else False
            if obj is not None and not ok:
                obj.destroy(source=obj, cause="S4OM inv test")
            network._log("inv test: added def {} -> {}".format(def_id, ok))
        network._log("inv test: sim {} has {}".format(sim.sim_info.id, collect_inventory(sim)))
    except Exception as e:
        network._log("inv test error: {}".format(e))
