# -*- coding: utf-8 -*-

"""Sims4Multiplayer v9.17 - 交互队列事件同步（🥇 一起生活的核心体验）

研究（2026-08-05）:
- interactions/si_state.pyc 反编译确认: sim.si_state + sim.queue 读交互,
  sim.push_super_affordance(super_affordance, target, context) 执行交互
  （支持传字符串交互名或 tuning 实例，内部构造 AOP + test_and_execute）
- 竞品 SimSync/S4MP 都是"同步操作"模型: 检测交互启动 → 广播 → 对端执行同款交互

功能:
  检测本机 sim 的交互启动（做饭/聊天/打扫/阅读...）
  → 广播 {type:"interaction", sim_id, affordance_guid, target_id}
  → 对端 push_super_affordance 执行同款交互
  → 双方屏幕上看到同一个交互动作（对方小人在做饭/聊天）

回环抑制（关键）:
  对端 push 的交互会在本端触发同样的检测 → 又广播回去
  → _applied_ts 记录本端 push 的交互（抑制窗口 3s），轮询时跳过

命令:
  mp_intsync  开始/停止交互同步（默认随 mp_sync 自动带起）
"""

import threading, time, sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 0.5       # 交互变化快，0.5s 轮询

SUPPRESS_SEC = 3.0        # 回环抑制窗口（本端 push 的交互 3s 内不广播）

_running = False

_thread = None
# sim_id -> set((guid64, target_id))  已广播的交互（去重）

_seen_interactions = {}
# sim_id -> ts  本端 push 的交互时间戳（回环抑制）

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

# ---------------- 收集本机交互 ----------------
def _get_my_sim():
    """获取本机控制的 active sim 实例（无则 None）"""
    try:
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        client = services.client_manager().get_first_client()
        if client is None:
            return None
        return client.active_sim
    except Exception:
        return None

def _iter_sims():
    """遍历家庭内所有有 si_state 的 sim 实例"""
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
            if sim is not None and getattr(sim, "si_state", None) is not None:
                sims.append(sim)
        return sims
    except Exception:
        return []

def _interaction_key(si):
    """提取交互的 (affordance_guid, target_id) —— guid 唯一标识交互类型"""
    try:
        aff = getattr(si, "affordance", None)
        if aff is None:
            return
        guid = getattr(aff, "guid64", None)
        if guid is None:
            guid = str(aff)
        target = getattr(si, "target", None)
        target_id = getattr(target, "id", 0)
        try:
            _target_defs[target_id] = int(target.definition.id)
        except Exception:
            pass

        return (guid, target_id)
    except Exception:
        return

_crafts_sent = set()

_dbg_seen = set()

def _kind(si):
    """'craft_step' for steps of a recipe (fridge->counter->stove...), 'craft_start' for the
    'Cook...' picker, else None. Classes from crafting/crafting_interactions.py."""
    try:
        from crafting.crafting_interactions import StartCraftingMixin, CraftingInteractionMixin
        aff = getattr(si, "affordance", None) or type(si)
        if isinstance(aff, type):
            if issubclass(aff, StartCraftingMixin):
                return "craft_start"
        if isinstance(si, CraftingInteractionMixin) or getattr(si, "process", None) is not None:
            return "craft_step"
    except Exception:
        pass

    return

def craft_announcements(sim):
    """New crafting processes this Sim is running -> [(process id, recipe guid)]."""
    out = []
    try:
        sis = list(sim.si_state.sis_actor_gen()) + list(getattr(sim, "queue", None) or [])
        for si in sis:
            proc = getattr(si, "process", None)
            if proc is None:
                proc = (getattr(si, "interaction_parameters", None) or {}).get("crafting_process")
            rec = getattr(proc, "recipe", None) if proc is not None else None
            if rec is not None and id(proc) not in _crafts_sent:
                _crafts_sent.add(id(proc))
                out.append((id(proc), int(rec.guid64)))

    except Exception as e:
        network._log("interaction_sync: craft scan error: {!r}".format(e))

    return out

def collect_running_interactions(sim):
    """收集 sim 当前运行中的交互 → [(key, sim_id)]"""
    result = []
    try:
        si_state = getattr(sim, "si_state", None)
        if si_state is None:
            return result
        sim_id = getattr(getattr(sim, "sim_info", None), "id", 0)
        for si in si_state.sis_actor_gen():
            try:
                if _kind(si) == "craft_step":
                    continue
                key = _interaction_key(si)
                if key is None:
                    continue
                result.append((key, sim_id))
            except Exception:
                continue

    except Exception:
        pass

    return result

def owns(sim_id, is_host, owners, my_sim_id):
    """Only the player controlling a Sim reports what it does (pure, tested). The host owns every
    Sim no joiner is playing; a joiner owns only the Sim they have selected. Before this both
    games reported every household Sim, so each echoed the other player's actions back."""
    if is_host:
        return owners.get(sim_id) in (None, 0)
    return my_sim_id is not None and sim_id == my_sim_id

_sent_count = 0

_retry = []

(RETRIES, RETRY_AFTER) = (3, 3.0)

_target_defs = {}

_recv_count = 0

def recently_applied(sim_id, within=15.0):
    """walk.py: a Sim we just told to do something (sit, cook...) - don't route it elsewhere."""
    t = _applied_ts.get(sim_id)
    return t is not None and time.time() - t < within

def _broadcast_loop():
    global _running
    while _running:
        try:
            sims = _iter_sims()
            try:
                from multimod import dialog_router
                owners = dict(dialog_router._owners)
            except Exception:
                owners = {}

            me = _get_my_sim()
            my_id = getattr(getattr(me, "sim_info", None), "id", None)
            for sim in sims:
                sim_id = getattr(getattr(sim, "sim_info", None), "id", 0)
                if not owns(sim_id, network._is_host, owners, my_id):
                    continue
                if sim_id in _applied_ts and time.time() - _applied_ts[sim_id] < SUPPRESS_SEC:
                    continue
                seen = _seen_interactions.setdefault(sim_id, set())
                for si_ in list(sim.si_state.sis_actor_gen()):
                    nm = getattr(getattr(si_, "affordance", None), "__name__", "")
                    if ("create" in nm.lower() or "craft" in nm.lower() or "cook" in nm.lower()) and nm not in _dbg_seen:
                        _dbg_seen.add(nm)
                        network._log("interaction_sync: craft-ish SI {} class {} process={}".format(nm, type(si_).__mro__[1].__name__, getattr(si_, "process", "n/a")))

                for (_pid, recipe) in craft_announcements(sim):
                    _send_craft(sim_id, recipe)

                for (key, sid) in collect_running_interactions(sim):
                    if key not in seen:
                        seen.add(key)
                        if len(seen) > 100:
                            seen.clear()
                        _broadcast_interaction(sid, key)

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

def _send_craft(sim_id, recipe):
    msg = {'type':"interaction", 
     'sim_id':sim_id,  'craft_recipe':recipe,  'affordance':0,  'target_id':0, 
     'ts':(time.time)()}
    if network._is_host:
        network._broadcast(msg)
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, msg)
    network._log("interaction_sync: sim {} started recipe {}".format(sim_id, recipe))

def _broadcast_interaction(sim_id, key):
    global _sent_count
    if network is None:
        return
    (guid, target_id) = key
    _sent_count += 1
    if _sent_count <= 30 or _sent_count % 25 == 0:
        network._log("interaction_sync: sent {} on sim {} (target {}) #{}".format(guid, sim_id, target_id, _sent_count))
    try:
        msg = {'type':"interaction", 
         'sim_id':sim_id,  'affordance':guid,  'target_id':target_id,  'target_def':(_target_defs.get)(target_id, 0), 
         'ts':(time.time)()}
        if network._is_host:
            network._broadcast(msg)
        elif network._client_socket is not None:
            network._send_json(network._client_socket, msg)
    except Exception:
        pass

_craft_start = {}

def _find_start(recipe):
    """(start affordance, object, now) on this lot that offers `recipe` - a fridge for food,
    a bar for drinks... Used when the owner's 'Cook...' step was too quick to be sent."""
    try:
        import services
        from crafting.crafting_interactions import StartCraftingMixin
        for obj in services.object_manager().get_valid_objects_gen():
            if getattr(obj, "is_sim", False):
                continue
            try:
                affs = obj.super_affordances()
            except Exception:
                continue

            for aff in affs:
                if isinstance(aff, type) and issubclass(aff, StartCraftingMixin) and recipe in (getattr(aff, "recipes", ()) or ()):
                    return (
                     aff, obj, time.time())

    except Exception as e:
        network._log("interaction_sync: no place found for recipe: {}".format(e))

    return

def _begin_craft(sim_id, recipe_guid):
    """Start the owner's recipe on this Sim the way the game does when the player picks it in the
    'Cook...' menu (StartCraftingSuperInteraction.on_choice_selected) - then the whole chain
    (fridge, counter, stove, serve) plays here too. Live test 2026-10-26: cooking didn't show."""
    try:
        import services
        from interactions.aop import AffordanceObjectPair
        from interactions.context import InteractionContext, QueueInsertStrategy
        from interactions.priority import Priority
        si_ = services.sim_info_manager().get(sim_id)
        sim = si_.get_sim_instance() if si_ is not None else None
        recipe = services.get_instance_manager(sims4.resources.Types.RECIPE).get(recipe_guid)
        start = _craft_start.pop(sim_id, None) or _find_start(recipe)
        if sim is None or recipe is None or start is None:
            network._log("interaction_sync: can't start recipe {} on {} (sim/recipe/start missing)".format(recipe_guid, sim_id))
            return
        (aff, target, _when) = start
        ctx = InteractionContext(sim, (InteractionContext.SOURCE_SCRIPT), (Priority.High), insert_strategy=(QueueInsertStrategy.NEXT))
        res = AffordanceObjectPair(aff, target, aff, None).interaction_factory(ctx)
        inter = getattr(res, "interaction", None)
        if inter is None:
            network._log("interaction_sync: recipe start failed: {}".format(res))
            return
        if hasattr(inter, "on_choice_selected"):
            out = inter.on_choice_selected(recipe, ingredient_data={})
        else:
            out = inter._handle_begin_crafting(recipe, sim, orderer_ids=[sim.id])
        network._log("interaction_sync: {} cooks {} -> {}".format(sim_id, recipe.__name__, out))
    except Exception as e:
        network._log("interaction_sync: recipe start error: {!r}".format(e))

def _find_copy(sim, remote_id, def_id):
    """The object an action targets was made during play (the plate of food, a book from the bag)
    and has a different id on every PC. Find ours: the copy buy-mode sync made of it, else the
    same kind of object in this Sim's inventory, else the nearest one on the lot."""
    try:
        from multimod import buy_sync
        obj = buy_sync._local_obj(remote_id)
        if obj is not None:
            return obj
    except Exception:
        pass

    if not def_id:
        return
    try:
        for obj in sim.inventory_component:
            if int(obj.definition.id) == def_id:
                return obj

    except Exception:
        pass

    try:
        import services
        best, best_d = (None, None)
        p = sim.position
        for obj in services.object_manager().get_valid_objects_gen():
            try:
                if int(obj.definition.id) != def_id:
                    continue
                q = obj.position
                d = (q.x - p.x) ** 2 + (q.z - p.z) ** 2
                if best_d is None or d < best_d:
                    best, best_d = obj, d
            except Exception:
                continue

        return best
    except Exception:
        return

def process_message(data):
    """收到对端交互 → 在本地执行同款交互"""
    global _recv_count
    try:
        sim_id = int(data.get("sim_id", 0))
        guid = data.get("affordance")
        target_id = int(data.get("target_id", 0))
        _recv_count += 1
        if data.get("craft_recipe"):
            _applied_ts[sim_id] = time.time()
            _begin_craft(sim_id, int(data["craft_recipe"]))
            return
        if _recv_count <= 30 or _recv_count % 25 == 0:
            network._log("interaction_sync: got {} for sim {} (target {}) #{}".format(guid, sim_id, target_id, _recv_count))
        if not guid:
            return
        me = _get_my_sim()
        if me is not None and getattr(getattr(me, "sim_info", None), "id", None) == sim_id:
            return
        import services
        sim_info = services.sim_info_manager().get(sim_id)
        if sim_info is None:
            network._log("interaction_sync: sim {} not in this world".format(sim_id))
            return
        sim = sim_info.get_sim_instance()
        if sim is None or getattr(sim, "si_state", None) is None:
            network._log("interaction_sync: sim {} not on this lot".format(sim_id))
            return
        target = None
        if target_id:
            target = services.object_manager().get(target_id)
            if target is None:
                target = services.sim_info_manager().get(target_id)
                if target is not None:
                    target = target.get_sim_instance()
            if target is None:
                target = _find_copy(sim, target_id, int(data.get("target_def") or 0))
        if isinstance(guid, int):
            aff = services.get_instance_manager(sims4.resources.Types.INTERACTION).get(guid)
        else:
            aff = guid
        if aff is None:
            network._log("interaction_sync: unknown interaction {}".format(guid))
            return
        try:
            from crafting.crafting_interactions import StartCraftingMixin
            if isinstance(aff, type):
                if issubclass(aff, StartCraftingMixin):
                    _craft_start[sim_id] = (
                     aff, target, time.time())
                    return
        except Exception:
            pass

        from interactions.context import InteractionContext, QueueInsertStrategy
        from interactions.priority import Priority
        ctx = InteractionContext(sim, (InteractionContext.SOURCE_SCRIPT), (Priority.High), insert_strategy=(QueueInsertStrategy.NEXT))
        res = sim.push_super_affordance(aff, target, ctx)
        _applied_ts[sim_id] = time.time()
        if not res:
            tries = int(data.get("_tries", 0)) + 1
            if tries <= RETRIES:
                _retry.append((time.time() + RETRY_AFTER, dict(data, _tries=tries)))
        network._log("interaction_sync: applied {} on sim {} -> {}".format(aff, sim_id, bool(res) if res is not None else res))
    except Exception as e:
        if network is not None:
            try:
                network._log("interaction_sync apply error: {}".format(e))
            except Exception:
                pass

def tick(now=None):
    """Main thread: re-try actions the game refused a moment ago."""
    now = now or time.time()
    due = [m for t, m in _retry if t <= now]
    if not due:
        return
    _retry[:] = [(t, m) for t, m in _retry if t > now]
    for m in due:
        process_message(m)

@sims4.commands.Command("mp_cook_test", command_type=(sims4.commands.CommandType.Live))
def mp_cook_test(sim_id=None, _connection=None):
    """Testing: start the first recipe a fridge offers on a Sim, like picking it in 'Cook...'."""
    import services
    sim = services.client_manager().get_first_client().active_sim
    sid = int(sim_id) if sim_id else int(sim.sim_info.id)
    from crafting.crafting_interactions import StartCraftingMixin
    found = []
    for obj in services.object_manager().get_valid_objects_gen():
        try:
            for aff in obj.super_affordances():
                if isinstance(aff, type) and issubclass(aff, StartCraftingMixin) and getattr(aff, "recipes", None):
                    found.append((aff, obj))

        except Exception:
            continue

    found.sort(key=(lambda f: "food" not in f[1].__class__.__name__.lower() and "fridge" not in str(f[0].__name__).lower()))
    if not found:
        network._log("cook test: nothing to cook with")
        return
    aff, obj = found[0]
    _craft_start[sid] = (aff, obj, time.time())
    network._log("cook test: {} at {} recipe {}".format(aff.__name__, obj.id, aff.recipes[0].__name__))
    _begin_craft(sid, int(aff.recipes[0].guid64))

# ---------------- 命令 ----------------
@sims4.commands.Command('mp_intsync', command_type=sims4.commands.CommandType.Live)
def mp_intsync(*args, _connection=None):
    global _running, _thread
    _out('S4OM interaction sync: {}'.format('on' if not _running else 'off'))
    if _running:
        _running = False
        if _thread is not None:
            _thread.join(timeout=2)
            _thread = None
    else:
        _running = True
        _thread = threading.Thread(target=_broadcast_loop, daemon=True)
        _thread.start()
    _out('mp_intsync: interaction sync {}'.format('on' if _running else 'off'), _connection)

def start():
    """随 mp_sync 自动带起（幂等）"""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_broadcast_loop, daemon=True)
    _thread.start()

def stop():
    global _running
    _running = False
