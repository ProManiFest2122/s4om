# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\party_games.py
# Compiled at: 2026-10-11 19:38:12
# Size of source mod 2**32: 12361 bytes

"""S4OM party games / shared challenges.

The host starts a challenge (launcher "Party games" or mp_challenge). Every player's mod
tracks their own Sim and reports progress to the host; the host announces progress and
the winner to everyone (notification + chat), and the result goes into the Discord
session recap.

  skills   Skill Race        - first to gain N skill levels (any skills)
  bingo    Mood Bingo        - first Sim to feel every mood on the card
  friends  Social Butterfly  - first to make N new friends
  happy    Good Vibes (team) - everyone's Sim Happy at the same time

Commands: mp_challenge [type] [n]   (host: start; anyone: no args = standings)
          mp_challenge_stop         (host)
"""

import random, time

try:
    import sims4.commands
except Exception:
    sims4 = None

from multimod import network

CHALLENGES = {
    "skills": {
        "title": "Skill Race",
        "goal": 3,
        "min": 1,
        "max": 20,
        "desc": "First to gain {n} skill levels wins - any skills count.",
    },
    "bingo": {
        "title": "Mood Bingo",
        "goal": 4,
        "min": 2,
        "max": 6,
        "desc": "First Sim to feel all of these wins: {moods}.",
    },
    "friends": {
        "title": "Social Butterfly",
        "goal": 2,
        "min": 1,
        "max": 10,
        "desc": "First to make {n} new friends wins.",
    },
    "happy": {
        "title": "Good Vibes",
        "goal": 1,
        "min": 1,
        "max": 1,
        "team": True,
        "desc": "Team challenge: get every Sim Happy at the same time.",
    },
}

BINGO_MOODS = ["Happy", "Flirty", "Energized", "Focused", "Inspired", "Playful", "Confident"]

FRIEND_SCORE = 30

TRACK_EVERY = 3.0

TIME_LIMIT = 3600

MAX_RESULTS_KEPT = 5

_active = None

_progress = {}

_baseline = None

_seen_moods = set()

_last_sent = None

_last_track = 0.0

_finished = []


def _my_sim_info():
    try:
        from multimod import sync

        sim = sync._get_active_sim()
        if sim is not None:
            return sim.sim_info
        return
    except Exception:
        return


def _skill_total(si):
    total = 0
    try:
        for stat in si.all_skills():
            try:
                if stat.is_skill():
                    total += int(stat.get_user_value())
            except Exception:
                continue

    except Exception:
        pass

    return total


def _mood_name(si):
    try:
        mood = si.get_mood()
        if mood is not None:
            return getattr(mood, "__name__", str(mood))
        return ""
    except Exception:
        return ""


def _friends(si):
    out = set()
    try:
        tracker = si.relationship_tracker
        for target in tracker.target_sim_gen():
            tid = getattr(target, "sim_id", target)
            try:
                if tracker.get_relationship_score(int(tid)) >= FRIEND_SCORE:
                    out.add(int(tid))
            except Exception:
                continue

    except Exception:
        pass

    return out


def _mood_matches(name, keyword):
    return keyword.lower() in (name or "").lower()


def read_state(si):
    """What we need from our Sim right now (tests replace this)."""
    return {"skills": _skill_total(si), "mood": _mood_name(si), "friends": _friends(si)}


def progress_for(ch, base, now, seen_moods):
    """(value, detail) for challenge `ch` given the start baseline and current state."""
    t = ch["ctype"]
    if t == "skills":
        v = max(0, now["skills"] - base["skills"])
        return (v, "{}/{} levels".format(v, ch["n"]))
    if t == "bingo":
        for m in ch["moods"]:
            if _mood_matches(now["mood"], m):
                seen_moods.add(m)

        got = [m for m in ch["moods"] if m in seen_moods]
        return (len(got), "{}/{} moods ({})".format(len(got), len(ch["moods"]), ", ".join(got) or "none yet"))
    if t == "friends":
        v = len(set(now["friends"]) - set(base["friends"]))
        return (v, "{}/{} new friends".format(v, ch["n"]))
    if t == "happy":
        happy = _mood_matches(now["mood"], "Happy")
        return (1 if happy else 0, "Happy" if happy else "not Happy yet")
    return (0, "")


def _say(text):
    network._notify(text)
    try:
        from multimod import lobby

        lobby.add_chat("S4OM", text)
    except Exception:
        pass


def _members():
    """pid -> name for everyone in the session (host = 0)."""
    out = {0: (network._member_name(0))}
    try:
        with network._clients_lock:
            pids = list(network._clients.keys())
    except Exception:
        pids = list(getattr(network, "_clients", {}).keys())

    for pid in pids:
        out[pid] = network._member_name(pid)

    return out


def start(ctype, n=None, by=None):
    """Host: start a challenge for everyone. Returns (ok, message)."""
    global _progress
    if not network._is_host:
        return (False, "Only the host can start a party game.")
    ctype = str(ctype or "").lower()
    spec = CHALLENGES.get(ctype)
    if spec is None:
        return (False, "Unknown game. Try: " + ", ".join(CHALLENGES))
    try:
        n = int(n) if n not in (None, "") else spec["goal"]
    except (TypeError, ValueError):
        n = spec["goal"]

    n = max(spec["min"], min(spec["max"], n))
    ch = {
        "id": (int(time.time() * 1000)) % 1000000000,
        "ctype": ctype,
        "n": n,
        "title": spec["title"],
        "team": bool(spec.get("team")),
        "started": (time.time)(),
        "moods": random.sample(BINGO_MOODS, n) if (ctype == "bingo") else [],
    }
    ch["desc"] = spec["desc"].format(n=n, moods=(", ".join(ch["moods"])))
    _progress = {}
    network._broadcast({"type": "challenge_start", "ch": ch})
    _begin(ch)
    _say("🎉 {}! {}".format(ch["title"], ch["desc"]))
    return (True, "{} started".format(ch["title"]))


def stop(reason="stopped by the host"):
    global _active
    if not network._is_host or _active is None:
        return False
    ch = _active
    network._broadcast(
        {"type": "challenge_end", "id": ch["id"], "winner": None, "reason": reason, "results": _standings()}
    )
    _active = None
    _say("{} {} .".format(ch["title"], reason).replace(" .", "."))
    return True


def _standings():
    rows = sorted((_progress.values()), key=(lambda r: -r["value"]))
    return [[r["name"], r["value"], r["detail"]] for r in rows]


def on_progress(pid, data):
    """Host: a player's progress arrived (pid 0 = the host itself)."""
    if not network._is_host or _active is None or data.get("id") != _active["id"]:
        return
    name = network._member_name(pid)
    value = int(data.get("value", 0))
    detail = str(data.get("detail", ""))[:120]
    prev = _progress.get(pid, {}).get("value", 0)
    _progress[pid] = {"name": name, "value": value, "detail": detail}
    if not _active["team"]:
        if value > prev:
            network._broadcast(
                {"type": "challenge_update", "id": _active["id"], "name": name, "detail": detail}
            )
            network._notify("{}: {}".format(name, detail))
    _check_winner()


def _check_winner(now=None):
    global _active
    ch = _active
    if ch is None:
        return
    winner = None
    if ch["team"]:
        members = _members()
        if members and all((_progress.get(pid, {}).get("value", 0) >= 1 for pid in members)):
            winner = "Everyone"
    else:
        goal = len(ch["moods"]) if ch["ctype"] == "bingo" else ch["n"]
        done = [r for r in _progress.values() if r["value"] >= goal]
        if done:
            winner = done[0]["name"]
    now = now or time.time()
    timed_out = winner is None and now - ch["started"] > TIME_LIMIT
    if timed_out and not ch["team"] and _progress:
        best = max((_progress.values()), key=(lambda r: r["value"]))
        winner = best["name"] if best["value"] > 0 else None
    if winner is None and not timed_out:
        return
    results = _standings()
    network._broadcast(
        {
            "type": "challenge_end",
            "id": ch["id"],
            "winner": winner,
            "reason": "time's up" if timed_out else "",
            "results": results,
        }
    )
    _finished.append({"title": ch["title"], "winner": winner or "nobody"})
    del _finished[:-MAX_RESULTS_KEPT]
    _active = None
    _announce_end(ch["title"], winner, results, timed_out)
    return winner


def _announce_end(title, winner, results, timed_out=False):
    if winner == "Everyone":
        _say("🏆 {} complete - everyone did it together!".format(title))
    else:
        if winner:
            _say("🏆 {} wins {}!{}".format(winner, title, " (time's up)" if timed_out else ""))
        else:
            _say("{} ended - no winner{}.".format(title, " (time's up)" if timed_out else ""))
    if results:
        if winner != "Everyone":
            network._log("challenge results: " + "; ".join(("{} {}".format(r[0], r[2]) for r in results)))


def recap_results():
    return list(_finished)


def _begin(ch):
    global _active
    global _baseline
    global _last_sent
    global _last_track
    global _seen_moods
    _active = ch
    si = _my_sim_info()
    _baseline = read_state(si) if si is not None else None
    _seen_moods = set()
    _last_sent = None
    _last_track = 0.0


def on_start(data):
    ch = data.get("ch") or {}
    if not isinstance(ch, dict) or ch.get("ctype") not in CHALLENGES:
        return
    _begin(ch)
    network._notify("🎉 {}! {}".format(ch.get("title"), ch.get("desc")))


def on_update(data):
    if _active:
        if data.get("id") == _active["id"]:
            network._notify("{}: {}".format(data.get("name"), data.get("detail")))


def on_end(data):
    global _active
    if _active is None or data.get("id") != _active["id"]:
        return
    title = _active["title"]
    _active = None
    _announce_end(title, data.get("winner"), data.get("results") or [], data.get("reason") == "time's up")


def tick(now=None):
    """Main thread, every housekeeping tick: read our Sim, report changes to the host."""
    global _baseline
    global _last_sent
    global _last_track
    if _active is None:
        return
    now = now or time.time()
    if now - _last_track < TRACK_EVERY:
        return
    _last_track = now
    si = _my_sim_info()
    if si is None:
        return
    state = read_state(si)
    if _baseline is None:
        _baseline = state
    (value, detail) = progress_for(_active, _baseline, state, _seen_moods)
    if (value, detail) == _last_sent:
        if network._is_host:
            _check_winner(now)
        return
    _last_sent = (value, detail)
    msg = {"type": "challenge_progress", "id": _active["id"], "value": value, "detail": detail}
    if network._is_host:
        on_progress(0, msg)
    elif network._client_socket is not None:
        network._send_json(network._client_socket, msg)


def status_text():
    if _active is None:
        return "No party game running. Host: mp_challenge skills|bingo|friends|happy [n]"
    lines = ["{} - {}".format(_active["title"], _active["desc"])]
    if _last_sent:
        lines.append("You: " + _last_sent[1])
    for r in _standings()[:12]:
        lines.append("  {}: {}".format(r[0], r[2]))

    return "\n".join(lines)


def run_launcher_cmd(data):
    """Launcher 'Party games' buttons (network._check_launcher_cmd)."""
    if data.get("cmd") == "mp_challenge_stop":
        stop()
        return
    ok, msg = start(data.get("game", ""), data.get("n"))
    if not ok:
        network._notify(msg)


if sims4 is not None:

    @sims4.commands.Command("mp_challenge", command_type=(sims4.commands.CommandType.Live))
    def mp_challenge(ctype=None, n=None, _connection=None):
        out = sims4.commands.CheatOutput(_connection)
        if ctype is None:
            for line in status_text().split("\n"):
                out(line)

            return
        (ok, msg) = start(ctype, n)
        out(msg)

    @sims4.commands.Command("mp_challenge_stop", command_type=(sims4.commands.CommandType.Live))
    def mp_challenge_stop(_connection=None):
        sims4.commands.CheatOutput(_connection)("Stopped." if stop() else "Nothing to stop (host only).")
