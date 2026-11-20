# -*- coding: utf-8 -*-
"""Online play without tokens: the open relay and the console commands that use it.

Starts relay/relay.py on a local port and checks
  - the relay's answers (FIND / JOIN / DATA / bad input / status page) and that it pipes bytes
  - network._relay_cfg(): built-in default, config override, never sends a stored token
  - online_setup: mp_relay / mp_host_online / mp_join_online logic, with no token anywhere

Run:  python tools/test_online_setup.py      (Python 3.7, like the other tools)

      python tools/test_online_setup.py --relay host[:port]
          only the protocol checks, against a relay that is already running somewhere
          (a container, your server) - to confirm a deployment really works from this PC
"""
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import two_process_test as t  # noqa: E402  (game stubs, paths)

HOST, PORT, STATUS_PORT = "127.0.0.1", 19656, 19657
REMOTE = False      # True with --relay: someone else's process, no status page to look at
TIMEOUT = 5
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("  {} {}{}".format("PASS" if ok else "FAIL", name, "" if ok or not detail else "  | " + str(detail)))


def hello(line, keep=False):
    """Send one handshake line; -> reply (and the socket when keep=True)."""
    s = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    s.sendall((line + "\n").encode("ascii"))
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            break
        buf += c
    reply = buf.decode("ascii", "replace").strip()
    if keep:
        return reply, s
    s.close()
    return reply


def readline(s):
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            break
        buf += c
    return buf.decode("ascii", "replace").strip()


def status_is(name, test):
    if REMOTE:
        return
    status = json.loads(urllib.request.urlopen("http://127.0.0.1:{}/".format(STATUS_PORT), timeout=5).read())
    check(name, test(status), status)


def relay_tests():
    print("[A] relay protocol ({}:{})".format(HOST, PORT))
    # a room code of our own, so a run against a shared relay cannot collide with a real room
    room = "T" + "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(7))
    nobody = room[:-1] + ("A" if room[-1] != "A" else "B")
    check("FIND on a room nobody opened -> ERR NO_ROOM", hello("S4OM FIND " + nobody) == "ERR NO_ROOM")
    check("JOIN on a room nobody opened -> ERR NO_ROOM", hello("S4OM JOIN " + nobody + " -") == "ERR NO_ROOM")
    check("garbage -> ERR BAD_REQUEST", hello("HELLO THERE FRIEND") == "ERR BAD_REQUEST")
    check("bad room code -> ERR BAD_REQUEST", hello("S4OM FIND ../etc") == "ERR BAD_REQUEST")
    check("over-long hello -> refused", hello("S4OM FIND " + "A" * 400) == "ERR BAD_REQUEST")

    reply, ctl = hello("S4OM HOST {} anything-goes".format(room.lower()), keep=True)
    check("HOST without a token -> OK", reply == "OK", reply)
    check("FIND finds the room (codes are case-insensitive)", hello("S4OM FIND " + room.capitalize()) == "OK")
    status_is("status page: 1 room, 1 player", lambda st: st["rooms"] == 1 and st["players"] == 1)
    check("DATA with an unknown id -> ERR NO_ROOM", hello("S4OM DATA {} deadbeef".format(room)) == "ERR NO_ROOM")

    # a joiner arrives: host is told CONN <cid>, opens the DATA link, bytes flow both ways untouched
    joiner = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    joiner.sendall("S4OM JOIN {} -\n".format(room).encode("ascii"))
    ctl.settimeout(TIMEOUT)
    conn = readline(ctl)
    check("host gets CONN <id>", conn.startswith("CONN ") and len(conn.split()) == 2, conn)
    reply, data = hello("S4OM DATA {} {}".format(room, conn.split()[-1]), keep=True)
    check("host's DATA link -> OK", reply == "OK", reply)
    check("joiner -> OK", readline(joiner) == "OK")
    payload = bytes(bytearray(range(256))) * 40
    joiner.sendall(payload)
    data.settimeout(TIMEOUT)
    got = b""
    while len(got) < len(payload):
        got += data.recv(65536)
    check("joiner -> host bytes arrive unchanged", got == payload)
    data.sendall(b"\x00\xffpong\n")
    joiner.settimeout(TIMEOUT)
    check("host -> joiner bytes arrive unchanged", joiner.recv(64) == b"\x00\xffpong\n")
    status_is("status page: 2 players", lambda st: st["players"] == 2)
    joiner.close()
    data.settimeout(TIMEOUT)
    check("joiner leaves -> host's link closes", data.recv(16) == b"")
    data.close()
    ctl.close()
    time.sleep(1.0 if REMOTE else 0.5)
    check("host leaves -> room is gone", hello("S4OM FIND " + room) == "ERR NO_ROOM")


def mod_tests():
    t._install_game_stubs()
    sys.path.insert(0, t.SRC)
    from multimod import network, online_setup

    tmp = tempfile.mkdtemp(prefix="s4om_online_")
    network.LAUNCHER_CONFIG_PATH = os.path.join(tmp, "mp_launcher_config.json")
    network._log = lambda m: None

    def cfg():
        return network._load_launcher_config() or {}

    def write(c):
        with open(network.LAUNCHER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(c, f)

    print("[B] which relay the mod uses")
    network.DEFAULT_RELAY = ""
    check("no default, no config -> no relay", network._relay_cfg() is None)
    network.DEFAULT_RELAY = "play.example.org"
    check("built-in default, standard port", network._relay_cfg() == ("play.example.org", 7656, "-"), network._relay_cfg())
    write({"relay": "10.0.0.5:9000", "relay_token": "SECRET-TOKEN-123"})
    rc = network._relay_cfg()
    check("config overrides the default", rc[:2] == ("10.0.0.5", 9000), rc)
    check("a token left in an old config is never sent", rc[2] == "-", rc)

    print("[C] console commands, no token anywhere")
    applied = []
    network._apply_launcher_config = lambda: (applied.append(dict(cfg())) or (True, "applied"))
    os.remove(network.LAUNCHER_CONFIG_PATH)
    network.DEFAULT_RELAY = ""
    ok, msg = online_setup.host_online()
    check("hosting with no server set explains what to do", not ok and "mp_relay" in msg, msg)
    check("mp_relay rejects nonsense", not online_setup.set_relay("not an address")[0])
    ok, msg = online_setup.set_relay("127.0.0.1:{}".format(PORT))
    check("mp_relay sets the server", ok and online_setup.relay_address() == "127.0.0.1:{}".format(PORT), msg)

    ok, msg = online_setup.host_online()
    c = cfg()
    code = c.get("room_code", "")
    check("mp_host_online works without a token", ok and c.get("mode") == "host", msg)
    check("it makes a 6-character code and shows it", len(code) == 6 and code in msg
          and all(ch in online_setup.CODE_ALPHABET for ch in code), msg)
    check("nothing token-like is written to the config", "relay_token" not in c and "token" not in json.dumps(c).lower(), c)

    # a joiner may start before the host: that is "waiting", not an error (the game keeps asking)
    ok, msg = online_setup.join_online("ZZZZZZ")
    c = cfg()
    check("joining a room that isn't open yet is accepted and says it is waiting",
          ok and "waiting for the host" in msg and c.get("mode") == "join" and c.get("room_code") == "ZZZZZZ", msg)
    check("joining needs a code", not online_setup.join_online("")[0])
    reply, ctl = hello("S4OM HOST FRIEND -", keep=True)
    ok, msg = online_setup.join_online(" friend ")
    c = cfg()
    check("mp_join_online <code> works without a token", ok and c.get("mode") == "join" and c.get("room_code") == "FRIEND", msg)
    ctl.close()

    online_setup.set_relay("127.0.0.1:1")      # nothing listens there
    ok, msg = online_setup.join_online("FRIEND")
    check("unreachable server gives a clear message", not ok and "can't reach" in msg, msg)
    network.DEFAULT_RELAY = "play.example.org:7656"
    ok, msg = online_setup.set_relay("default")
    check("mp_relay default goes back to the built-in server", ok and online_setup.relay_address() == "play.example.org:7656", msg)


def launcher_tests():
    print("[D] launcher (online_launcher.py) - logic only, no window")
    sys.path.insert(0, os.path.join(HERE, ".."))
    import online_launcher as L
    from multimod import network, lobby

    here = "127.0.0.1:{}".format(PORT)
    old = {"name": "Old", "host": "192.168.0.5", "relay_token": "SECRET", "relay": "relay.s4om.gg:7656", "password": "pw"}
    L.DEFAULT_RELAY = "play.example.org"
    h = L.host_config(old, "  Ania  ", "play.example.org")
    check("Host: mode, 6-char code, trimmed name", h["mode"] == "host" and len(h["room_code"]) == 6 and h["name"] == "Ania", h)
    check("Host: old token and LAN host IP are dropped, password kept",
          "relay_token" not in h and "host" not in h and h["password"] == "pw", h)
    check("Host: built-in server is not written to the config", "relay" not in h, h)
    j = L.join_config(old, "", here, "ABCDEF")
    check("Join: mode, code, own server kept", j["mode"] == "join" and j["room_code"] == "ABCDEF" and j["relay"] == here, j)
    check("Join: empty name falls back to something", bool(j["name"]))
    off = L.offline_config(h)
    check("Play alone: mode and code removed, name kept", "mode" not in off and "room_code" not in off and off["name"] == "Ania", off)
    # 2026-11-20: a second click on Host made a new code while the running game kept hosting the
    # old room, so the host read out a code that was never open ("room isn't open" for the friend)
    again = L.host_config(h, "Ania", "play.example.org")
    check("Host clicked again: same room code, not a new one", again["room_code"] == h["room_code"], (h["room_code"], again["room_code"]))
    check("Host after Play alone: a fresh code", L.host_config(off, "Ania", "play.example.org")["room_code"] != h["room_code"])
    check("Host after having joined someone: own new code, not theirs", L.host_config(j, "Ania", here)["room_code"] != "ABCDEF")
    check("every click is stamped, so the running game sees a new request", isinstance(h.get("at"), int) and "at" not in off)
    check("room codes: spaces/case tidied, junk refused",
          L.clean_code(" ab cd ef ") == "ABCDEF" and L.clean_code("ab") is None and L.clean_code("../x!") is None)
    check("server address: host, host:port, junk",
          L.clean_relay("play.example.org") == ("play.example.org", 7656) and L.clean_relay("1.2.3.4:9000") == ("1.2.3.4", 9000)
          and L.clean_relay("not an address") is None and L.clean_relay("") is None)

    relay = L.clean_relay(here)
    check("server reachable", L.relay_reachable(relay) is True)
    check("unreachable server is reported as such", L.relay_reachable(("127.0.0.1", 1), timeout=2) is False)
    check("room not open", L.find_room(relay, "NOPE99") is False)
    reply, ctl = hello("S4OM HOST LAUNCH -", keep=True)
    check("room open", L.find_room(relay, "LAUNCH") is True)
    ctl.close()

    # what the launcher writes is what the mod reads
    path = L.config_path()
    check("launcher and mod agree on the config file", os.path.normcase(path) == os.path.normcase(os.path.join(
        os.environ["S4OM_USERDIR"], "Mods", "mp_launcher_config.json")), path)
    network.LAUNCHER_CONFIG_PATH = path
    network.DEFAULT_RELAY = "play.example.org"
    L.save_config(L.host_config({}, "Ania", here, code="PARTY7"))
    check("mod: relay from the launcher's config", network._relay_cfg() == ("127.0.0.1", PORT, "-"), network._relay_cfg())
    lobby.reset_room_state()
    lobby.add_host_self()
    check("mod: hosts with the launcher's room code", lobby.ROOM_CODE == "PARTY7", lobby.ROOM_CODE)
    L.save_config(L.host_config({}, "Ania", "play.example.org", code="PARTY7"))
    check("mod: built-in server when the config names none", network._relay_cfg() == ("play.example.org", 7656, "-"))


def launcher_setup_tests():
    print("[E] launcher: installs the mod, fixes the game's options, says what the game is doing")
    import online_launcher as L

    root = tempfile.mkdtemp(prefix="s4om_launcher_")
    mods = os.path.join(root, "Mods")
    os.makedirs(os.path.join(mods, "sub"))
    src = os.path.join(root, "bundle.ts4script")
    with open(src, "wb") as f:
        f.write(b"new mod build")
    target = os.path.join(mods, L.MOD_FILE)

    check("no bundled mod -> says so, touches nothing", L.install_mod(src="", mods=mods, running=False)[0] == "none")
    st, moved = L.install_mod(src=src, mods=mods, running=False)
    check("empty Mods folder -> installed", st == "installed" and open(target, "rb").read() == b"new mod build", st)
    check("same file there -> nothing to do", L.install_mod(src=src, mods=mods, running=False) == ("current", []))
    with open(target, "wb") as f:
        f.write(b"old mod build")
    check("older mod, game running -> not replaced mid-game, asks to close it",
          L.install_mod(src=src, mods=mods, running=True)[0] == "close" and open(target, "rb").read() == b"old mod build")
    st, moved = L.install_mod(src=src, mods=mods, running=False)
    check("older mod, game closed -> updated", st == "updated" and open(target, "rb").read() == b"new mod build", st)
    os.remove(target)
    check("no mod, game running -> installed for the next start",
          L.install_mod(src=src, mods=mods, running=True)[0] == "restart" and os.path.exists(target))
    for name in ("S4OM (1).ts4script", "S4OM.ts4script.zip", os.path.join("sub", "S4OM.ts4script"), "OtherMod.ts4script", "s4om_notes.txt"):
        with open(os.path.join(mods, name), "wb") as f:
            f.write(b"x")
    st, moved = L.install_mod(src=src, mods=mods, running=False)
    left = sorted(os.listdir(mods)) + sorted(os.listdir(os.path.join(mods, "sub")))
    check("misnamed and duplicate copies are moved out of Mods, other files stay",
          sorted(moved) == ["S4OM (1).ts4script", "S4OM.ts4script", "S4OM.ts4script.zip"]
          and left == ["OtherMod.ts4script", "S4OM.ts4script", "s4om_notes.txt", "sub"], (moved, left))
    check("...moved, not deleted", len(os.listdir(os.path.join(root, "S4OM-old-files"))) == 3)

    ini = os.path.join(root, "Options.ini")
    check("no Options.ini (game never started) -> nothing to fix", L.fix_options(ini, running=False) == "missing")
    body = b"[options]\r\ncdsautomaticupdates = 1\r\nscriptmodsenabled = 0\r\nname = Za\xbf\xf3\xb3\xe6\r\nmodsdisabled = 1\r\n"
    with open(ini, "wb") as f:
        f.write(body)
    check("script mods off, game running -> asks to close the game, file untouched",
          L.fix_options(ini, running=True) == "close" and open(ini, "rb").read() == body)
    check("script mods off, game closed -> turned on", L.fix_options(ini, running=False) == "fixed")
    fixed = open(ini, "rb").read()
    check("only those two lines changed (other bytes and line ends kept)",
          fixed == body.replace(b"scriptmodsenabled = 0", b"scriptmodsenabled = 1").replace(b"modsdisabled = 1", b"modsdisabled = 0"))
    check("the original is kept as a backup", open(ini + ".s4om-backup", "rb").read() == body)
    check("already on -> nothing to do", L.fix_options(ini, running=False) == "ok")
    with open(ini, "wb") as f:
        f.write(b"[options]\ncdsautomaticupdates = 1\n")
    check("setting missing altogether -> added", L.fix_options(ini, running=False) == "fixed"
          and b"[options]\nscriptmodsenabled = 1\n" in open(ini, "rb").read())
    check("messages for the player exist in both languages",
          "Mods" in L.setup_message("installed", [], "ok", lang="en") and "Mods" in L.setup_message("installed", [], "ok", lang="pl")
          and L.setup_message("current", [], "ok", lang="pl") == L.TEXT["pl"]["setup_ok"]
          and set(L.TEXT["en"]) == set(L.TEXT["pl"]))

    spath = os.path.join(root, "mp_status.json")
    now = time.time()
    check("no status file -> None", L.read_status(spath) is None)
    with open(spath, "w", encoding="utf-8") as f:
        json.dump({"at": now - 5, "state": "hosting", "room": "PARTY7", "players": ["Ania"], "note": ""}, f)
    check("fresh status is read", (L.read_status(spath, now) or {}).get("state") == "hosting")
    check("stale status (game closed) is ignored", L.read_status(spath, now + 60) is None)

    host = {"mode": "host", "room_code": "PARTY7"}
    join = {"mode": "join", "room_code": "PARTY7"}
    d = lambda cfg, status, running, **k: L.describe(cfg, status, running, lang="en", **k)
    check("game closed: tells the host / joiner / newcomer what to do next",
          "Start The Sims 4" in d(host, None, False) and "PARTY7" in d(join, None, False) and "Host a game" in d({}, None, False))
    check("game just started: 'starting', not 'the mod is missing'", "starting" in d(join, None, True, starting=True))
    check("game running without the mod reporting in: says the mod didn't load", "didn't load" in d(join, None, True))
    check("hosting alone / with friends", "nobody has joined" in d(host, {"state": "hosting", "room": "PARTY7", "players": ["Ania"]}, True)
          and "Ania, Ola" in d(host, {"state": "hosting", "room": "PARTY7", "players": ["Ania", "Ola"]}, True))
    check("joiner waiting for the host", "isn't open yet" in d(join, {"state": "waiting for the host", "room": "PARTY7", "players": []}, True))
    check("joiner connected, with the hand-off note",
          "Connected to room PARTY7" in d(join, {"state": "connected", "room": "PARTY7", "players": ["Ania", "Ola"], "note": "the host's game arrived"}, True)
          and "host's game arrived" in d(join, {"state": "connected", "room": "PARTY7", "players": ["Ania"], "note": "the host's game arrived"}, True))
    check("game in another room than the launcher asks for -> says a restart is needed",
          "close The Sims 4" in d(join, {"state": "hosting", "room": "OLD123", "players": ["Ania"]}, True))
    check("Polish window on request", "Hostujesz pokój PARTY7" in L.describe(host, {"state": "hosting", "room": "PARTY7", "players": ["Ania"]}, True, lang="pl"))


def remote_main(address):
    """--relay host[:port]: protocol checks only, against a relay running somewhere else."""
    global HOST, PORT, REMOTE, TIMEOUT
    host, _, port = address.partition(":")
    HOST, PORT, REMOTE, TIMEOUT = host, int(port or 7656), True, 10
    try:
        socket.create_connection((HOST, PORT), timeout=TIMEOUT).close()
    except socket.timeout:
        print("no answer from {}:{} within {}s - a firewall is dropping the port, or the address is wrong".format(
            HOST, PORT, TIMEOUT))
        return 2
    except OSError as e:
        print("{}:{} did not accept the connection ({}) - the machine answers but nothing listens on that port".format(
            HOST, PORT, e))
        return 2
    relay_tests()
    passed = sum(results)
    print("result: {} passed, {} failed".format(passed, len(results) - passed))
    return 0 if passed == len(results) else 1


def main():
    if "--relay" in sys.argv:
        return remote_main(sys.argv[sys.argv.index("--relay") + 1])
    relay = subprocess.Popen([sys.executable, t.RELAY_PY, "--port", str(PORT), "--status-port", str(STATUS_PORT),
                              "--bind", "127.0.0.1", "--log", os.devnull], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        line = relay.stdout.readline()
        assert line.strip() == b"READY", "relay failed to start: " + line.decode("utf-8", "replace")
        relay_tests()
        mod_tests()
        launcher_tests()
        launcher_setup_tests()
    finally:
        relay.kill()
    passed = sum(results)
    print("result: {} passed, {} failed".format(passed, len(results) - passed))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
