# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\dialog_router.py
# Compiled at: 2026-10-26 18:05:40
# Size of source mod 2**32: 12517 bytes

"""S4OM pop-up routing: a question about your Sim shows up on YOUR screen.

The Sims 4 shows choice pop-ups (job offers, phone calls, "Sim wants to...", event
invitations) on whichever PC's game raised them - often the host's, even when the pop-up
is about another player's Sim. S4OM sends those pop-ups to the player who controls that
Sim instead, and sends their answer back so it's applied where the event lives.

How (decompiled ui/ui_dialog_service.py, ui/ui_dialog.py):
  - UiDialogService.dialog_show(dialog, ...) is patched. If dialog.owner is a Sim another
    player controls, the dialog stays registered (so it can still be answered) but isn't
    shown here; dialog.build_msg() - the exact Dialog_pb2.UiDialogMessage the game would
    display - is serialized and sent to that player.
  - Their game displays it with a local id; UiDialogService.dialog_respond for that id is
    patched to send the answer back, and the original PC calls dialog_respond(real id).
  - If the owner's own game raised the same pop-up a moment ago (both PCs ran the event),
    they don't see it twice - their answer is passed on to the other PC too.

Who controls which Sim: the host learns it from each player's position/state messages
and shares the map (sim_owners). Turn off with launcher config "route_popups": false.
"""

import base64, itertools, time

from multimod import network

DEDUPE_SECONDS = 30.0

FAKE_ID_BASE = 2113929216

_owners = {}

_owners_sent = None

_fake_ids = itertools.count(FAKE_ID_BASE)

_shown_remote = {}

_recent_local = {}

_linked = {}

_installed = False

def enabled():
    try:
        return (network._load_launcher_config() or {}).get("route_popups", True) is not False
    except Exception:
        return True

def in_session():
    if network._is_host:
        return bool(network._clients)
    return network._client_socket is not None

def note_owner(sim_id, pid):
    """Host: called for every sim_pos / sim_state - the sender is actively controlling that Sim.
    One active Sim per player: switching to another Sim releases the old one, so a pop-up about
    the Sim you just left no longer chases you (2026-10-25 - Kody saw other players' prompts)."""
    try:
        sim_id, pid = int(sim_id), int(pid)
    except (TypeError, ValueError):
        return
    else:
        if _owners.get(sim_id) == pid:
            return
        for sid in [s for s, p in _owners.items() if p == pid]:
            del _owners[sid]

        _owners[sim_id] = pid

def _my_active_sim():
    try:
        from multimod import sync

        sid = sync._get_my_sim_id()
        if sid is not None:
            return int(sid)
        return
    except Exception:
        return

def owner_sim_id(dialog):
    """The Sim a pop-up is about, or None (household/world pop-ups)."""
    try:
        owner = dialog.owner
        sid = getattr(owner, "sim_id", None) or getattr(owner, "id", None)
        if sid:
            return int(sid)
        return
    except Exception:
        return

def route_target(sim_id, me, my_active, allowed_pids=None):
    """Who should see a pop-up about `sim_id`. Pure - the whole routing rule in one place:
      - the Sim I'm actively controlling          -> me
      - a Sim another player actively controls    -> that player
      - nobody on it now, but the host gave it to players (control.py) -> one of them (host first)
      - a Sim nobody controls                      -> the host (someone has to answer, and only once)
    """
    if sim_id is None or sim_id == my_active:
        return me
    pid = _owners.get(sim_id)
    if pid is not None:
        return pid
    if allowed_pids:
        if 0 in allowed_pids:
            return 0
        return allowed_pids[0]
    return 0

def _allowed_pids(sim_id):
    if sim_id is None:
        return []
    try:
        from multimod import control

        return control.players_allowed(sim_id)
    except Exception:
        return []

def note_my_sim():
    try:
        from multimod import sync

        sid = sync._get_my_sim_id()
        if sid is not None:
            if network._is_host:
                note_owner(sid, 0)
    except Exception:
        pass

def tick():
    """Host: share the owner map when it changes."""
    global _owners_sent
    if not (network._is_host and network._clients):
        return
    note_my_sim()
    snap = dict(_owners)
    if snap != _owners_sent:
        _owners_sent = snap
        network._broadcast({"type": "sim_owners", "owners": {str(k): v for k, v in snap.items()}})

def on_owners(data):
    try:
        _owners.clear()
        _owners.update({int(k): int(v) for k, v in (data.get("owners") or {}).items()})
    except Exception:
        pass

def _route(msg):
    """Deliver a dialog_fwd / dialog_answer to player msg['to'] (through the host)."""
    to = msg.get("to")
    if network._is_host:
        if to == 0:
            handle(msg)
            return
        entry = network._clients.get(to)
        if entry:
            network._send_json(entry[0], msg)
    elif network._client_socket is not None:
        network._send_json(network._client_socket, msg)

def handle(msg):
    """Host relays messages for someone else; everyone handles their own."""
    to = msg.get("to")
    me = 0 if network._is_host else network._my_player_id
    if to != me:
        if network._is_host:
            _route(msg)
        return
    if msg.get("type") == "dialog_fwd":
        _show_forwarded(msg)
    elif msg.get("type") == "dialog_answer":
        _apply_answer(msg)

def _fingerprint(msg_proto):
    try:
        return bytes(msg_proto.text.SerializeToString()) + bytes(msg_proto.title.SerializeToString())
    except Exception:
        return

def maybe_forward(service, dialog, kwargs):
    """True when the dialog was sent to its owner instead of being shown here."""
    if not (in_session() and enabled()):
        return False
    me = 0 if network._is_host else network._my_player_id
    sid = owner_sim_id(dialog)
    pid = route_target(sid, me, _my_active_sim(), _allowed_pids(sid))
    if pid == me:
        return False
    try:
        if not dialog.has_responses():
            return False
        msg = (dialog.build_msg)(**kwargs)
        service._active_dialogs[dialog.dialog_id] = dialog
        _route(
            {
                "type": "dialog_fwd",
                "to": pid,
                "origin": me,
                "dialog_id": int(dialog.dialog_id),
                "msg_type": int(dialog.DIALOG_MSG_TYPE),
                "proto": (base64.b64encode(msg.SerializeToString()).decode)("ascii"),
            }
        )
        network._log("popups: dialog {} about sim of player {} sent to them".format(dialog.dialog_id, pid))
        return True
    except Exception as e:
        network._log("popups: forward failed, showing here: {}".format(e))
        return False

def _show_forwarded(msg):
    try:
        from protocolbuffers import Dialog_pb2
        from distributor.system import Distributor

        proto = Dialog_pb2.UiDialogMessage()
        proto.ParseFromString(base64.b64decode(msg["proto"]))
    except Exception as e:
        network._log("popups: can't read forwarded dialog: {}".format(e))
        return

    origin = (int(msg.get("origin", 0)), int(msg.get("dialog_id", 0)))
    fp = _fingerprint(proto)
    now = time.time()
    hit = _recent_local.get(fp) if fp else None
    if hit:
        if now - hit[0] < DEDUPE_SECONDS:
            _linked.setdefault(hit[1], []).append(origin)
            return
    fake = next(_fake_ids)
    _shown_remote[fake] = origin
    proto.dialog_id = fake
    Distributor.instance().add_event(int(msg.get("msg_type")), proto)

def _apply_answer(msg):
    try:
        import services

        services.ui_dialog_service().dialog_respond(int(msg["dialog_id"]), int(msg["response"]))
        network._log("popups: answer {} applied to dialog {}".format(msg["response"], msg["dialog_id"]))
    except Exception as e:
        network._log("popups: applying answer failed: {}".format(e))

def on_local_show(dialog, service, kwargs):
    """Remember questions our own game asks (for de-duplication)."""
    try:
        fp = _fingerprint((dialog.build_msg)(**kwargs))
        if fp:
            _recent_local[fp] = (time.time(), int(dialog.dialog_id))
            for k in [k for k, v in _recent_local.items() if time.time() - v[0] > DEDUPE_SECONDS]:
                del _recent_local[k]

    except Exception:
        pass

def on_respond(dialog_id, response):
    """True if this answer belonged to a forwarded pop-up (then it's sent back, not applied here)."""
    origin = _shown_remote.pop(dialog_id, None)
    if origin is not None:
        _route({"type": "dialog_answer", "to": origin[0], "dialog_id": origin[1], "response": int(response)})
        return True
    for pid, did in _linked.pop(dialog_id, []):
        _route({"type": "dialog_answer", "to": pid, "dialog_id": did, "response": int(response)})

    return False

def install():
    global _installed
    if _installed:
        return True
    try:
        from ui.ui_dialog_service import UiDialogService
    except Exception:
        return False
    if getattr(UiDialogService, "_s4om_patched", False):
        _installed = True
        return True
    UiDialogService._s4om_patched = True
    orig_show, orig_respond = UiDialogService.dialog_show, UiDialogService.dialog_respond

    def dialog_show(self, dialog, phone_ring_type, *args, **kwargs):
        try:
            build_kwargs = {
                k: v for k, v in kwargs.items() if k not in ("auto_response", "caller_id", "immediate")
            }
            if maybe_forward(self, dialog, build_kwargs):
                return
            if in_session():
                if getattr(dialog, "has_responses", (lambda: False))():
                    on_local_show(dialog, self, build_kwargs)
        except Exception as e:
            network._log("popups hook error (shown here instead): {}".format(e))

        return orig_show(self, dialog, phone_ring_type, *args, **kwargs)

    def dialog_respond(self, dialog_id, response, *args, **kwargs):
        try:
            if on_respond(dialog_id, response):
                return True
        except Exception as e:
            network._log("popups respond hook error: {}".format(e))

        return orig_respond(self, dialog_id, response, *args, **kwargs)

    UiDialogService.dialog_show = dialog_show
    UiDialogService.dialog_respond = dialog_respond
    _installed = True
    network._log("popups: routing installed")
    return True

install()

try:
    import sims4.commands

    @sims4.commands.Command("mp_popup_test", command_type=(sims4.commands.CommandType.Live))
    def mp_popup_test(sim_id=None, _connection=None):
        """Testing: raise an OK/Cancel pop-up about a Sim (goes to whoever plays that Sim)."""
        import services
        from ui.ui_dialog import UiDialog, UiDialogResponse, ButtonType
        from sims4.localization import LocalizationHelperTuning

        raw = LocalizationHelperTuning.get_raw_text
        si = services.sim_info_manager().get(int(sim_id))
        dlg = UiDialog.TunableFactory().default(
            si,
            text=(lambda *a, **k: raw("S4OM pop-up test about {}".format(si.first_name))),
            title=(lambda *a, **k: raw("S4OM test")),
        )
        dlg.set_responses(
            (
                UiDialogResponse(
                    dialog_response_id=(ButtonType.DIALOG_RESPONSE_OK),
                    sort_order=0,
                    text=(lambda *a, **k: raw("OK")),
                ),
                UiDialogResponse(
                    dialog_response_id=(ButtonType.DIALOG_RESPONSE_CANCEL),
                    sort_order=1,
                    text=(lambda *a, **k: raw("Cancel")),
                ),
            )
        )
        dlg.show_dialog(
            on_response=(
                lambda d: network._log("popup test: answered {}".format(getattr(d, "response", None)))
            )
        )
        network._log("popup test: raised dialog about {}".format(sim_id))

except Exception:
    pass
