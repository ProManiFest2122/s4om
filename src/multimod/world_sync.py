# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\world_sync.py
# Compiled at: 2026-10-26 18:28:00
# Size of source mod 2**32: 19706 bytes

"""S4OM world sync - the systems that belong to the whole game, not one Sim. The host
decides; joiners follow.

  Calendar, holidays, festivals, invitations: the drama scheduler
      (drama_scheduler/drama_scheduler.py). Everyone loads the same save, so scheduled
      events start out identical; they drift because every game randomly schedules new
      ones. During a session joiners don't roll their own (score_and_schedule_nodes_gen
      is skipped) and copy every node the host schedules - same type, same Sim, same
      time - so calendars, holidays and festivals match. Host-cancelled ones are removed.
  Neighbourhood action plans: street civic policies (civic_policies/). Enacted and
      balloted plans mirror the host; a joiner's votes are sent to the host, where the
      voting happens.
  Small businesses: open/closed, price markup and business funds of the business on the
      current lot (business/business_manager.py) follow the host; a joiner opening or
      closing it, or changing prices, is sent to the host first.
  Social events (parties, dates, weddings...): when a player starts one, the same event
      starts on everyone's game with that player's Sim as host, so the goals show for all
      (situations/situation_manager.create_situation). Ending it ends it everywhere.

Launcher config "world_sync": false turns it off.
"""

import time

from multimod import network

SEND_EVERY = 20.0

MAX_NODES = 400

S4OM_SOURCE = "S4OM mirror"

_last_sent = 0.0

_node_map = {}

_events_out = {}

_events_in = {}

_mirroring = False

_installed = False


def enabled():
    try:
        return (network._load_launcher_config() or {}).get("world_sync", True) is not False
    except Exception:
        return True


def in_session():
    if network._is_host:
        return bool(network._clients)
    return network._client_socket is not None


def _send(msg):
    if network._is_host:
        network._broadcast(msg)
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, msg)


def _me():
    if network._is_host:
        return 0
    return network._my_player_id


def read_drama():
    import services

    out = []
    for node in services.drama_scheduler_service().scheduled_nodes_gen():
        try:
            t = node.selected_time
            r, s = node.get_receiver_sim_info(), node.get_sender_sim_info()
            out.append(
                {
                    "guid": int(node.guid64),
                    "uid": int(node.uid),
                    "time": int(t) if (t is not None) else 0,
                    "receiver": int(r.sim_id) if (r is not None) else 0,
                    "sender": int(s.sim_id) if (s is not None) else 0,
                }
            )
        except Exception:
            continue

        if len(out) >= MAX_NODES:
            break

    return out


def read_civic():
    import services

    out = {}
    svc = services.street_service()
    for street, provider in getattr(svc, "_provider_instances", {}).items():
        try:
            out[str(int(street.guid64))] = {
                "enacted": sorted((int(p.guid64) for p in provider.get_enacted_policies(tuning=True))),
                "ballot": sorted((int(p.guid64) for p in provider.get_balloted_policies(tuning=True))),
            }
        except Exception:
            continue

    return out


def read_trends():
    """Trendi fashion trend strengths (statistics on FashionTrendService)."""
    import services

    svc = services.fashion_trend_service()
    tracker = getattr(svc, "statistic_tracker", None) if svc is not None else None
    if tracker is None:
        return
    return {str(int(st.guid64)): float(st.get_value()) for st in tracker}


def read_business():
    import services

    bm = services.business_service().get_business_manager_for_zone()
    if bm is None:
        return
    try:
        return {
            "zone": int(bm.business_zone_id),
            "open": bool(bm.is_open),
            "markup": float(bm.markup_multiplier),
            "funds": int(bm.funds.money),
        }
    except Exception:
        return


def plan_drama(host_nodes, local_uids, node_map):
    """-> (to_schedule [host node], to_cancel [our uid]) for one host snapshot."""
    host_uids = set((n["uid"] for n in host_nodes))
    to_schedule = [n for n in host_nodes if n["uid"] not in local_uids if n["uid"] not in node_map]
    to_cancel = [ours for theirs, ours in node_map.items() if theirs not in host_uids if ours in local_uids]
    return (to_schedule, to_cancel)


def plan_civic(host, local):
    """-> {street: {"enact": [...], "repeal": [...], "ballot": [...]}} differences only."""
    out = {}
    for street, h in host.items():
        l = local.get(street, {"enacted": [], "ballot": []})
        d = {
            "enact": sorted(set(h["enacted"]) - set(l["enacted"])),
            "repeal": sorted(set(l["enacted"]) - set(h["enacted"])),
            "ballot": sorted(set(h["ballot"]) - set(l["ballot"])),
        }
        if any(d.values()):
            out[street] = d

    return out


def tick(now=None):
    global _last_sent
    if not (network._is_host and network._clients and enabled()):
        return
    now = now or time.time()
    if now - _last_sent < SEND_EVERY:
        return
    _last_sent = now
    msg = {"type": "world_state", "ts": now}
    for key, reader in (
        ("drama", read_drama),
        ("civic", read_civic),
        ("business", read_business),
        ("trends", read_trends),
        ("weather", read_weather),
    ):
        try:
            msg[key] = reader()
        except Exception as e:
            network._log("world_sync read {} failed: {}".format(key, e))

    network._broadcast(msg)


def _manager(kind):
    import services, sims4.resources

    return services.get_instance_manager(getattr(sims4.resources.Types, kind))


def apply_world(data):
    if network._is_host or not enabled():
        return
    if data.get("drama") is not None:
        _apply_drama(data["drama"])
    if data.get("civic") is not None:
        _apply_civic(data["civic"])
    if data.get("business"):
        _apply_business(data["business"])
    if data.get("trends"):
        _apply_trends(data["trends"])
    if data.get("weather"):
        _apply_weather(data["weather"])


def read_weather():
    """Host: the weather event playing on this lot (Seasons), or None."""
    import services

    ws = services.weather_service()
    ev = getattr(ws, "_current_event", None) if ws is not None else None
    if ev is not None:
        return int(ev.guid64)


def _apply_weather(guid):
    import services

    ws = services.weather_service()
    if ws is None:
        return
    cur = getattr(ws, "_current_event", None)
    if cur is not None:
        if int(cur.guid64) == int(guid):
            return
    ev = _manager("WEATHER_EVENT").get(int(guid))
    if ev is None:
        return
    ws.force_start_weather_event(ev, 6.0)
    network._log("world_sync: weather -> {}".format(ev.__name__))


def _apply_trends(trends):
    import services

    svc = services.fashion_trend_service()
    tracker = getattr(svc, "statistic_tracker", None) if svc is not None else None
    if tracker is None:
        return
    stat_mgr = _manager("STATISTIC")
    for guid, value in trends.items():
        st = stat_mgr.get(int(guid))
        if st is None:
            continue
        current = tracker.get_value(st)
        if current is None or abs(float(current) - float(value)) > 0.01:
            tracker.set_value(st, float(value))


def _apply_drama(nodes):
    import services
    from date_and_time import DateAndTime
    from event_testing.resolver import SingleSimResolver

    ds = services.drama_scheduler_service()
    local = {int(n.uid) for n in ds.scheduled_nodes_gen()}
    (to_schedule, to_cancel) = plan_drama(nodes, local, _node_map)
    for uid in to_cancel:
        ds.cancel_scheduled_node(uid)

    for k in [k for k, v in _node_map.items() if v in to_cancel]:
        del _node_map[k]

    sims, types = services.sim_info_manager(), _manager("DRAMA_NODE")
    now = services.time_service().sim_now
    made = 0
    for n in to_schedule:
        node_type = types.get(n["guid"])
        receiver = sims.get(n["receiver"]) if n["receiver"] else None
        if node_type is None or (n["time"] and DateAndTime(n["time"]) < now):
            continue
        if receiver is None:
            hh = services.active_household()
            receiver = next(iter(hh.sim_info_gen()), None) if hh is not None else None
        if receiver is None:
            continue
        try:
            uid = ds.schedule_node(
                node_type,
                (SingleSimResolver(receiver)),
                specific_time=(DateAndTime(n["time"]) if n["time"] else None),
            )
            if uid:
                _node_map[n["uid"]] = uid
                made += 1
        except Exception as e:
            network._log("world_sync: schedule {} failed: {}".format(n["guid"], e))

    if made or to_cancel:
        network._log("world_sync: calendar +{} -{}".format(made, len(to_cancel)))


def _apply_civic(host):
    import services

    svc = services.street_service()
    providers = {str(int(s.guid64)): p for s, p in getattr(svc, "_provider_instances", {}).items()}
    import types

    for street, d in plan_civic(host, read_civic()).items():
        provider = providers.get(street)
        if provider is None:
            continue
        for guid in d["repeal"]:
            provider.repeal(types.SimpleNamespace(guid64=guid))

        for guid in d["enact"]:
            provider.enact(types.SimpleNamespace(guid64=guid))

        for guid in d["ballot"]:
            inst = provider.get_policy_instance_for_tuning(guid)
            if inst is not None:
                provider.add_to_ballot(inst)


def _apply_business(b):
    global _mirroring
    import services

    bm = services.business_service().get_business_manager_for_zone()
    if bm is None or int(bm.business_zone_id) != int(b["zone"]):
        return
    _mirroring = True
    try:
        if bool(bm.is_open) != bool(b["open"]):
            bm.set_open(bool(b["open"]))
        if abs(float(bm.markup_multiplier) - float(b["markup"])) > 0.001:
            bm.set_markup_multiplier(float(b["markup"]))
        diff = int(b["funds"]) - int(bm.funds.money)
        if diff > 0:
            bm.funds.add(diff, bm.funds_transfer_gain_reason)
        elif diff < 0:
            bm.funds.try_remove((-diff), (bm.funds_transfer_loss_reason), require_full_amount=False)
    finally:
        _mirroring = False


def on_request(sender_pid, data):
    """Host: a joiner voted / opened their business / changed prices."""
    global _last_sent
    if not network._is_host:
        return
    import services

    try:
        if data.get("what") == "vote":
            svc = services.street_service()
            for s, p in getattr(svc, "_provider_instances", {}).items():
                if str(int(s.guid64)) == str(data["street"]):
                    inst = p.get_policy_instance_for_tuning(int(data["policy"]))
                    if inst is not None:
                        p.vote_by_instance(inst, (int(data.get("count", 1))), user_directed=True)

        else:
            if data.get("what") == "business":
                bm = services.business_service().get_business_manager_for_zone()
                if bm is not None:
                    if "open" in data:
                        bm.set_open(bool(data["open"]))
                    if "markup" in data:
                        bm.set_markup_multiplier(float(data["markup"]))
    except Exception as e:
        network._log("world_sync request failed: {}".format(e))

    _last_sent = 0.0


def on_event_start(data):
    global _mirroring
    if not enabled():
        return
    import services
    from situations.situation_guest_list import SituationGuestList

    st = _manager("SITUATION").get(int(data["guid"]))
    if st is None:
        return
    _mirroring = True
    try:
        try:
            sid = services.get_zone_situation_manager().create_situation(
                st,
                guest_list=SituationGuestList(invite_only=True, host_sim_id=(int(data.get("host_sim", 0)))),
                user_facing=True,
                creation_source=S4OM_SOURCE,
            )
            if sid:
                _events_in[(int(data["origin"]), int(data["id"]))] = sid
                network._notify(
                    "S4OM: {} started an event - you're in it!".format(data.get("by", "A player"))
                )
        except Exception as e:
            network._log("world_sync: mirror event failed: {}".format(e))

    finally:
        _mirroring = False


def on_event_end(data):
    global _mirroring
    sid = _events_in.pop((int(data["origin"]), int(data["id"])), None)
    if sid is None:
        return
    import services

    _mirroring = True
    try:
        services.get_zone_situation_manager().destroy_situation_by_id(sid)
    finally:
        _mirroring = False


def install():
    """Patch the game classes; safe to call more than once."""
    global _installed
    if _installed:
        return True
    try:
        from drama_scheduler.drama_scheduler import DramaScheduleService
        from situations.situation_manager import SituationManager
        from business.business_manager import BusinessManager
        from civic_policies.base_civic_policy_provider import BaseCivicPolicyProvider
    except Exception:
        return False
    if getattr(DramaScheduleService, "_s4om_patched", False):
        _installed = True
        return True
    DramaScheduleService._s4om_patched = True
    orig_score = DramaScheduleService.score_and_schedule_nodes_gen

    def score_and_schedule_nodes_gen(self, *args, **kwargs):
        try:
            skip = not network._is_host and in_session() and enabled()
        except Exception:
            skip = False

        if skip:
            return
        result = yield from orig_score(self, *args, **kwargs)
        return result
        if False:
            yield None

    DramaScheduleService.score_and_schedule_nodes_gen = score_and_schedule_nodes_gen
    orig_create, orig_destroy = SituationManager.create_situation, SituationManager.destroy_situation_by_id

    def create_situation(self, situation_type, *args, **kwargs):
        sid = orig_create(self, situation_type, *args, **kwargs)
        try:
            if sid:
                if not _mirroring:
                    if in_session():
                        if enabled():
                            if kwargs.get("user_facing", True):
                                if kwargs.get("creation_source") != S4OM_SOURCE:
                                    gl = kwargs.get("guest_list")
                                    host_sim = (
                                        int(getattr(gl, "host_sim_id", 0) or 0) if gl is not None else 0
                                    )
                                    from multimod import sync

                                    if host_sim:
                                        if host_sim == sync._get_my_sim_id():
                                            _events_out[sid] = sid
                                            _send(
                                                {
                                                    "type": "event_start",
                                                    "origin": _me(),
                                                    "id": int(sid),
                                                    "guid": int(situation_type.guid64),
                                                    "host_sim": host_sim,
                                                    "by": network._member_name(_me())
                                                    if (network._is_host)
                                                    else "A player",
                                                }
                                            )
        except Exception as e:
            network._log("world_sync: event broadcast failed: {}".format(e))

        return sid

    SituationManager.create_situation = create_situation

    def destroy_situation_by_id(self, situation_id, *args, **kwargs):
        try:
            if situation_id in _events_out:
                if not _mirroring:
                    _events_out.pop(situation_id, None)
                    _send({"type": "event_end", "origin": _me(), "id": int(situation_id)})
        except Exception as e:
            network._log("world_sync event end hook error: {}".format(e))

        return orig_destroy(self, situation_id, *args, **kwargs)

    SituationManager.destroy_situation_by_id = destroy_situation_by_id
    orig_set_open, orig_markup = BusinessManager.set_open, BusinessManager.set_markup_multiplier

    def set_open(self, is_open, *args, **kwargs):
        try:
            if not network._is_host:
                if in_session():
                    if enabled():
                        if not _mirroring:
                            _send({"type": "world_req", "what": "business", "open": bool(is_open)})
        except Exception as e:
            network._log("world_sync business hook error: {}".format(e))

        return orig_set_open(self, is_open, *args, **kwargs)

    def set_markup_multiplier(self, multiplier, *args, **kwargs):
        try:
            if not network._is_host:
                if in_session():
                    if enabled():
                        if not _mirroring:
                            _send({"type": "world_req", "what": "business", "markup": float(multiplier)})
        except Exception as e:
            network._log("world_sync business hook error: {}".format(e))

        return orig_markup(self, multiplier, *args, **kwargs)

    BusinessManager.set_open, BusinessManager.set_markup_multiplier = set_open, set_markup_multiplier
    orig_vote = BaseCivicPolicyProvider.vote_by_instance

    def vote_by_instance(self, policy_instance, count=1, user_directed=False, *args, **kwargs):
        if user_directed and not network._is_host and in_session() and enabled():
            try:
                import services

                street = services.street_service().get_street(self)
                _send(
                    {
                        "type": "world_req",
                        "what": "vote",
                        "street": int(street.guid64),
                        "policy": int(policy_instance.guid64),
                        "count": int(count),
                    }
                )
            except Exception:
                pass

        return orig_vote(self, policy_instance, count, user_directed, *args, **kwargs)

    BaseCivicPolicyProvider.vote_by_instance = vote_by_instance
    _installed = True
    network._log("world_sync: hooks installed")
    return True


install()

try:
    import sims4.commands

    @sims4.commands.Command("mp_event_test", command_type=(sims4.commands.CommandType.Live))
    def mp_event_test(name="houseparty", _connection=None):
        """Testing: throw a social event (first situation whose name contains `name`) hosted by
        the selected Sim - same as planning one, so it syncs like a real one."""
        import services
        from situations.situation_guest_list import SituationGuestList

        sim = services.client_manager().get_first_client().active_sim
        st = next(
            (
                t
                for t in _manager("SITUATION").types.values()
                if name.lower() in getattr(t, "__name__", "").lower()
            ),
            None,
        )
        if st is None:
            network._log("event test: no situation like {}".format(name))
            return
        sid = services.get_zone_situation_manager().create_situation(
            st,
            guest_list=SituationGuestList(invite_only=True, host_sim_id=(sim.sim_info.id)),
            user_facing=True,
        )
        network._log("event test: started {} -> {}".format(st.__name__, sid))

except Exception:
    pass
