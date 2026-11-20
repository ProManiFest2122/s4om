# -*- coding: utf-8 -*-
"""Online play without tokens: the open relay and the console commands that use it.

Starts relay/relay.py on a local port and checks
  - the relay's answers (FIND / JOIN / DATA / bad input / status page) and that it pipes bytes
  - network._relay_cfg(): built-in default, config override, never sends a stored token
  - online_setup: mp_relay / mp_host_online / mp_join_online logic, with no token anywhere

Run:  python tools/test_online_setup.py      (Python 3.7, like the other tools)
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import two_process_test as t  # noqa: E402  (game stubs, paths)

PORT, STATUS_PORT = 19656, 19657
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("  {} {}{}".format("PASS" if ok else "FAIL", name, "" if ok or not detail else "  | " + str(detail)))


def hello(line, port=PORT, keep=False):
    """Send one handshake line; -> reply (and the socket when keep=True)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
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


def relay_tests():
    print("[A] relay protocol")
    check("FIND on a room nobody opened -> ERR NO_ROOM", hello("S4OM FIND ABCDEF") == "ERR NO_ROOM")
    check("JOIN on a room nobody opened -> ERR NO_ROOM", hello("S4OM JOIN ABCDEF -") == "ERR NO_ROOM")
    check("garbage -> ERR BAD_REQUEST", hello("HELLO THERE FRIEND") == "ERR BAD_REQUEST")
    check("bad room code -> ERR BAD_REQUEST", hello("S4OM FIND ../etc") == "ERR BAD_REQUEST")
    check("over-long hello -> refused", hello("S4OM FIND " + "A" * 400) == "ERR BAD_REQUEST")

    reply, ctl = hello("S4OM HOST roomAA anything-goes", keep=True)
    check("HOST without a token -> OK", reply == "OK", reply)
    check("FIND finds the room (codes are case-insensitive)", hello("S4OM FIND ROOMaa") == "OK")
    status = json.loads(urllib.request.urlopen("http://127.0.0.1:{}/".format(STATUS_PORT), timeout=5).read())
    check("status page: 1 room, 1 player", status["rooms"] == 1 and status["players"] == 1, status)
    check("DATA with an unknown id -> ERR NO_ROOM", hello("S4OM DATA ROOMAA deadbeef") == "ERR NO_ROOM")

    # a joiner arrives: host is told CONN <cid>, opens the DATA link, bytes flow both ways untouched
    joiner = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    joiner.sendall(b"S4OM JOIN ROOMAA -\n")
    ctl.settimeout(5)
    conn = readline(ctl)
    check("host gets CONN <id>", conn.startswith("CONN ") and len(conn.split()) == 2, conn)
    reply, data = hello("S4OM DATA ROOMAA " + conn.split()[-1], keep=True)
    check("host's DATA link -> OK", reply == "OK", reply)
    check("joiner -> OK", readline(joiner) == "OK")
    payload = bytes(bytearray(range(256))) * 40
    joiner.sendall(payload)
    data.settimeout(5)
    got = b""
    while len(got) < len(payload):
        got += data.recv(65536)
    check("joiner -> host bytes arrive unchanged", got == payload)
    data.sendall(b"\x00\xffpong\n")
    joiner.settimeout(5)
    check("host -> joiner bytes arrive unchanged", joiner.recv(64) == b"\x00\xffpong\n")
    status = json.loads(urllib.request.urlopen("http://127.0.0.1:{}/".format(STATUS_PORT), timeout=5).read())
    check("status page: 2 players", status["players"] == 2, status)
    joiner.close()
    data.settimeout(5)
    check("joiner leaves -> host's link closes", data.recv(16) == b"")
    data.close()
    ctl.close()
    time.sleep(0.5)
    check("host leaves -> room is gone", hello("S4OM FIND ROOMAA") == "ERR NO_ROOM")


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

    before = dict(cfg())
    ok, msg = online_setup.join_online("ZZZZZZ")
    check("joining a room that isn't open is refused, config untouched", not ok and "isn't open" in msg and cfg() == before, msg)
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
    check("launcher describes the saved session", "HOST" in L.describe(L.load_config()) and "off" in L.describe({}))


def main():
    relay = subprocess.Popen([sys.executable, t.RELAY_PY, "--port", str(PORT), "--status-port", str(STATUS_PORT),
                              "--bind", "127.0.0.1", "--log", os.devnull], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        line = relay.stdout.readline()
        assert line.strip() == b"READY", "relay failed to start: " + line.decode("utf-8", "replace")
        relay_tests()
        mod_tests()
        launcher_tests()
    finally:
        relay.kill()
    passed = sum(results)
    print("result: {} passed, {} failed".format(passed, len(results) - passed))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
