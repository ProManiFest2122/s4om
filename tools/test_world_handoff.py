# -*- coding: utf-8 -*-
"""Joining without commands: the joiner may start first, and the host's game reaches them by itself.

[A] the decisions, in-process: who needs the host's game, when the launcher config is applied
[B] a host and a joiner as two processes (two "PCs", each with its own Sims 4 folder) through a
    local relay, with the game's own calls stubbed:
      - the joiner asks for the room before the host has opened it, and gets in when it opens
      - the host has never saved: it is told to, and the request waits
      - once it can, the host saves and sends; the save file arrives intact on the joiner
      - the joiner is at the main menu, so the save is opened without a pop-up
      - the joiner drops and reconnects from inside the host's game: nothing is sent again

Run:  python tools/test_world_handoff.py      (Python 3.7, like the other tools)
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import two_process_test as t  # noqa: E402  (game stubs, paths, sandboxed S4OM_USERDIR)

PORT = 19756
ROOM = "HANDQF"
HOST_HH, OWN_HH = 111, 222
RUN = 34.0
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("  {} {}{}".format("PASS" if ok else "FAIL", name, "" if ok or not detail else "  | " + str(detail)))


# ---------------------------------------------------------------- [A]
def decision_tests():
    t._install_game_stubs()
    sys.path.insert(0, t.SRC)
    from multimod import network, session_watch as w, world_handoff as h

    network._log = lambda m: None
    network._notify = lambda m: None
    print("[A] decisions")
    n = h.needs_world
    check("host has sent nothing yet -> joiner needs the game", n("", 111, "", 0) is True and n("", 111, "abc", 111) is True)
    check("same copy, same household -> in the host's game", n("w1", 111, "w1", 111) is False)
    check("got the copy but never loaded it (own household) -> send", n("w1", 111, "w1", 222) is True)
    check("old copy loaded by hand (same household, other id) -> send", n("w1", 111, "w0", 111) is True)
    check("joiner at the main menu -> send (it is opened for them)", n("w1", 111, "w1", 0, True) is True)
    check("same copy, nothing loaded yet (loading screen) -> wait for their report, don't send",
          n("w1", 111, "w1", 0, False) is None)
    check("host has no household to compare with -> same copy is enough", n("w1", 0, "w1", 111) is False)
    check("old mod that reports nothing: sent once per copy, never in a loop",
          n("w1", 111, None, None) is True and n("w1", 111, None, None, False, "w1") is False
          and n("w2", 111, None, None, False, "w1") is True)

    sig = w.signature
    a = sig({"mode": "join", "room_code": "abc123", "at": 1})
    check("no mode -> no session", sig({}) is None and sig({"name": "x"}) is None and sig(None) is None)
    check("room codes compare without case", a == sig({"mode": "join", "room_code": "ABC123", "at": 1}))
    check("clicking again is a new request", a != sig({"mode": "join", "room_code": "ABC123", "at": 2}))
    d = w.decide
    check("nothing asked for -> nothing", d(None, None, None, False) is None and d(None, a, a, True) is None)
    check("new request, game idle -> apply", d(a, None, None, False) == "apply")
    check("request the start-up timer already handled -> nothing", d(a, None, a, False) is None and d(a, None, a, True) is None)
    check("request this watch already handled -> nothing", d(a, a, None, False) is None)
    again = sig({"mode": "join", "room_code": "ABC123", "at": 9})
    check("same room clicked again while in it -> nothing (not a restart notice)", d(again, a, a, True) is None)
    check("same room clicked again after leaving it -> apply", d(again, a, a, False) == "apply")
    b = sig({"mode": "host", "room_code": "ZZZ999", "at": 5})
    check("another session while in one -> restart notice, not a silent nothing", d(b, a, a, True) == "restart")

    # joiner side of the hand-off
    loads = []
    from multimod import autoload
    autoload._load = lambda slot, label: (loads.append(slot), "loading")[1]
    h.at_main_menu = lambda: False
    opened = h.on_world({"world": "w7"}, 0xFF40)
    check("in a household: remembers the copy, leaves the pop-up to resync", opened is False and h.world_id == "w7" and not loads)
    h.at_main_menu = lambda: True
    opened = h.on_world({"world": "w8"}, 0xFF40)
    check("at the main menu: opens the save itself", opened is True and loads == [0xFF40] and h.world_id == "w8")
    autoload._load = lambda slot, label: "skipped: game version 9.9 not supported yet"
    check("auto-open unavailable: falls back (pop-up / message), says why",
          h.on_world({"world": "w9"}, 0xFF40) is False and "Load Game" in h.status)
    h.household_id = lambda: 5
    check("hello carries the copy id, the household and 'at the main menu'",
          h.hello_fields() == {"world": "w9", "hh": 5, "menu": True})

    # what the status file says about the hand-off
    from multimod import lobby, resync
    h.phase, h._waiting = "", {}
    network._is_host = True
    h._waiting[3] = ("Ola", 0.0)
    h.phase = "need_save"
    check("status: host waiting for a save names who is waiting", h.handoff_status() == {"phase": "need_save", "names": ["Ola"], "progress": None})
    resync._phase = "saving"
    check("status: while the game is being saved / sent, that wins", h.handoff_status()["phase"] == "saving")
    resync._phase = None
    h._waiting.clear()
    check("status: nobody waiting any more -> no stale 'need save'", h.handoff_status()["phase"] == "")
    network._is_host = False
    h.receiving()
    check("status: joiner, host is saving -> receiving, no percentage yet", h.handoff_status() == {"phase": "receiving", "names": [], "progress": None})
    lobby._recv_save_cache[resync.SYNC_SLOT] = {"chunks": {0: "a", 1: "b", 2: "c"}, "total": 8}
    check("status: joiner, 3 of 8 parts received -> 37%", h.handoff_status()["progress"] == 37)
    lobby._recv_save_cache.clear()
    h.phase, h.household_id = "opening", (lambda: 0)
    check("status: joiner, save opening -> opening until a household is loaded", h.handoff_status()["phase"] == "opening")
    h.household_id = lambda: 5
    check("status: joiner, household loaded -> in the host's game", h.handoff_status()["phase"] == "in_host_game")
    h.phase = "arrived"
    check("status: joiner who hasn't clicked the pop-up stays 'arrived'", h.handoff_status()["phase"] == "arrived")
    h.phase = ""

    st = w.status()
    check("status file: state, room, players, hand-off, build",
          st["state"] == "idle" and "players" in st and "phase" in st["handoff"] and st["version"] == network.MOD_BUILD, st)
    network._last_relay_error = ("ERR ROOM_FULL", time.time())
    check("status file: a fresh refusal from the server is included", w.status().get("error") == "ERR ROOM_FULL")
    network._last_relay_error = ("ERR ROOM_FULL", time.time() - 600)
    check("status file: an old one is not", "error" not in w.status())
    network._last_relay_error = None


# ---------------------------------------------------------------- [B]
def run_peer(role):
    t._install_game_stubs()
    sys.path.insert(0, t.SRC)
    from multimod import autoload, lobby, network, resync, world_handoff as h

    log, notes = [], []
    network._log = lambda m: log.append(str(m))
    network._notify = lambda m: notes.append(str(m))
    network._ensure_alarm = lambda: None
    cfg = {"name": role, "password": "", "relay": "127.0.0.1:{}".format(PORT)}
    network._load_launcher_config = lambda: cfg
    network.ROOM_WAIT_POLL = 1.0
    h.SETTLE, h.RETRY, h.HINT_EVERY = 1.0, 1.0, 1.0
    resync.COOLDOWN = 2.0
    os.makedirs(lobby.SAVES_DIR, exist_ok=True)
    start = time.time()
    out = {"role": role}
    phases, progress = [], []

    def sample():
        st = h.handoff_status()
        if st["phase"] and (not phases or phases[-1] != st["phase"]):
            phases.append(st["phase"])
        if st["progress"] is not None:
            progress.append(st["progress"])

    if role == "host":
        save = os.path.join(lobby.SAVES_DIR, "Slot_00000002.save")
        state = {"saved_once": False, "requests": 0, "sha": ""}

        def start_save(slot_id, name):          # the game writing the save
            data = os.urandom(6 * 1024 * 1024)
            with open(save, "wb") as f:
                f.write(data)
            state["sha"] = hashlib.sha256(data).hexdigest()

        real_request = resync.request

        def counting_request(*a, **k):
            res = real_request(*a, **k)
            if res[0]:
                state["requests"] += 1
            return res

        resync.request = counting_request
        resync._start_save = start_save
        resync._current_slot = lambda: (2, "Test world") if state["saved_once"] else (None, None)
        network._has_active_sim = lambda: True
        h.household_id = lambda: HOST_HH
        time.sleep(4.0)                         # the joiner is already asking for the room
        network._is_host = True
        network._my_player_id = 0
        lobby.ROOM_CODE = ROOM
        assert network._start_relay_host(), "relay host did not start"
        while time.time() - start < RUN:
            if not state["saved_once"] and time.time() - start > 12.0:
                state["saved_once"] = True      # the host does Esc > Save As
            network._process_incoming()
            network._desync_tick()
            sample()
            time.sleep(0.05)
        out.update(requests=state["requests"], sha=state["sha"], world=h.world_id, phases=phases,
                   told_to_save=any("save it once" in n for n in notes),
                   saw_in_game=any("already in the host's game" in l for l in log),
                   saw_loading=any("still loading" in l for l in log),
                   waiting=len(h._waiting))
    else:
        loads = []
        # the joiner's game: at the main menu -> (host's save opens) loading screen -> in the household
        state = {"hh": 0, "menu": True, "dropped": False, "back": False, "reported": False}

        def load(slot, label):
            loads.append(slot)
            state["menu"] = False               # loading screen: no menu, no household yet
            return "loading"

        autoload._load = load
        h.household_id = lambda: state["hh"]
        h.at_main_menu = lambda: state["menu"]
        real_chunk = lobby.on_save_chunk

        def chunk_then_sample(data):        # look after every part: over loopback the whole save can
            real_chunk(data)                # arrive between two passes of the loop below
            sample()

        lobby.on_save_chunk = chunk_then_sample
        network._is_host = False
        ok, desc = network._join_when_open(ROOM)
        out["first_answer"] = desc
        loaded_at = None
        while time.time() - start < RUN:
            network._process_incoming()
            sample()
            if loads and loaded_at is None:
                loaded_at = time.time()
            if loaded_at and not state["dropped"] and time.time() - loaded_at > 2.0 and network._client_socket is not None:
                state["dropped"] = True         # connection lost during the loading screen
                dropped_sock = network._client_socket
                try:
                    dropped_sock.close()
                except Exception:
                    pass
            if state["dropped"] and network._client_socket is not None and network._client_socket is not dropped_sock:
                state["back"] = True            # (judged here, not at the end: the host process exits first)
            if state["back"] and not state["reported"] and time.time() - loaded_at > 9.0:
                state["hh"] = HOST_HH           # loading finished: the host's household is the one loaded
                state["reported"] = h.report()  # what network._check_lot_status does when a lot loads
            time.sleep(0.05)
        got = os.path.join(lobby.SAVES_DIR, resync.SYNC_SLOT)
        sha = hashlib.sha256(open(got, "rb").read()).hexdigest() if os.path.exists(got) else ""
        out.update(loads=loads, sha=sha, world=h.world_id, dropped=state["dropped"], phases=phases, progress=progress,
                   waited=any("isn't open yet" in l for l in log),
                   connected_again=sum(1 for l in log if l.startswith("connected to")),
                   back=state["back"], reported=state["reported"])
    out["log_tail"] = log[-12:]
    print("RESULT " + json.dumps(out))


def session_test():
    print("[B] host + joiner, two processes, local relay")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    relay = subprocess.Popen([sys.executable, t.RELAY_PY, "--port", str(PORT), "--bind", "127.0.0.1", "--log", os.devnull],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        assert relay.stdout.readline().strip() == b"READY"
        procs = {}
        for role in ("joiner", "host"):
            home = tempfile.mkdtemp(prefix="s4om_handoff_{}_".format(role))
            os.makedirs(os.path.join(home, "Mods"))
            procs[role] = subprocess.Popen([sys.executable, __file__, role], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           env=dict(env, S4OM_USERDIR=home))
        res = {}
        for role, p in procs.items():
            text = p.communicate(timeout=RUN + 40)[0].decode("utf-8", "replace")
            line = [l for l in text.splitlines() if l.startswith("RESULT ")]
            res[role] = json.loads(line[-1][7:]) if line else {"error": text[-1500:]}
    finally:
        relay.kill()
    host, joiner = res["host"], res["joiner"]
    if "error" in host or "error" in joiner:
        check("both processes finished", False, host.get("error") or joiner.get("error"))
        return
    check("joiner asked before the room existed and was told to wait", joiner["waited"] and "waiting" in joiner["first_answer"], joiner["first_answer"])
    check("joiner got in once the host opened the room", joiner["connected_again"] >= 1, joiner["log_tail"])
    check("host that never saved is told to save once", host["told_to_save"])
    check("host then saved and sent its game by itself - exactly once", host["requests"] == 1, host["requests"])
    check("the save file arrived on the joiner unchanged", bool(host["sha"]) and host["sha"] == joiner["sha"], (host["sha"][:12], joiner["sha"][:12]))
    check("joiner at the main menu opened it without a pop-up (sync slot)", joiner["loads"] == [0xFF40], joiner["loads"])
    check("both sides agree which copy it is", bool(host["world"]) and host["world"] == joiner["world"], (host["world"], joiner["world"]))
    check("joiner dropped and reconnected", joiner["dropped"] and joiner["connected_again"] >= 2 and joiner["back"], joiner["log_tail"])
    check("reconnect during the loading screen: host waits for the report instead of sending again", host["saw_loading"], host["log_tail"])
    check("joiner reported the household it loaded", joiner["reported"], joiner["log_tail"])
    check("host then saw them as in its game - and never sent a second copy",
          host["saw_in_game"] and host["waiting"] == 0 and host["requests"] == 1, (host["requests"], host["log_tail"]))
    in_order = lambda seq, want: [x for x in seq if x in want] == want
    check("host reported: need save -> saving -> sending -> sent", in_order(host["phases"], ["need_save", "saving", "sending", "sent"]), host["phases"])
    check("joiner reported: receiving -> opening -> in the host's game", in_order(joiner["phases"], ["receiving", "opening", "in_host_game"]), joiner["phases"])
    check("joiner reported a percentage while receiving", any(0 <= x <= 100 for x in joiner["progress"])
          and joiner["progress"] == sorted(joiner["progress"]), joiner["progress"][:12])
    if not all(results[-14:]):
        print("    host log:", *host["log_tail"], sep="\n      ")
        print("    joiner log:", *joiner["log_tail"], sep="\n      ")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("host", "joiner"):
        run_peer(sys.argv[1])
    else:
        decision_tests()
        session_test()
        passed = sum(results)
        print("result: {} passed, {} failed".format(passed, len(results) - passed))
        sys.exit(0 if passed == len(results) else 1)
