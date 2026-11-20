#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4OM open relay - lets players meet by room code without forwarding ports.

Host and joiners all connect OUT to this server. It matches them by room code and then
only copies bytes between them; it never reads, changes or stores the game traffic.
No accounts and no tokens: anyone who knows this server's address and a room code can join
that room (rooms can still have a password - that is checked by the host's game, not here).

    python3 relay.py                      # listen on 0.0.0.0:7656
    python3 relay.py --port 7656 --status-port 7657 --log /var/log/s4om-relay.log

Wire protocol (one text line, then raw bytes), as spoken by src/multimod/network.py:

    S4OM HOST <code> <token>   -> OK      control link; relay then sends "PING" (answer "PONG")
                                          and "CONN <cid>" once per joiner
    S4OM DATA <code> <cid>     -> OK      host's link for one joiner, raw bytes from here on
    S4OM JOIN <code> <token>   -> OK      raw bytes from here on
    S4OM FIND <code>           -> OK      is that room open?

    refusals: ERR NO_ROOM, ERR ROOM_FULL, ERR ROOM_TAKEN, ERR HOST_TIMEOUT, ERR BAD_REQUEST,
              ERR SLOW_DOWN, ERR SERVER_FULL

The <token> field is accepted and ignored. Standard library only; Python 3.7 or newer.
"""
import argparse
import asyncio
import collections
import json
import os
import socket
import time

MAX_PLAYERS = 12            # host + 11 joiners, same cap as the mod
PING_EVERY = 20.0           # seconds between PINGs on a host's control link
HOST_SILENT_AFTER = 75.0    # no line from the host for this long -> control link is dead
DATA_WAIT = 15.0            # how long a joiner waits for the host's DATA link
HELLO_TIMEOUT = 10.0
HELLO_LIMIT = 256
CONTROL_LINE_LIMIT = 16384
COPY_CHUNK = 65536
CODE_MIN, CODE_MAX = 4, 12


class Room:
    def __init__(self, code, host_ip):
        self.code = code
        self.host_ip = host_ip
        self.control = None     # StreamWriter of the host's control link, or None while it is down
        self.pending = {}       # cid -> Future resolved with the host's (reader, writer)
        self.pipes = set()      # ids of joiners connected right now
        self.down_since = None
        self.created = time.time()

    def players(self):
        return 1 + len(self.pipes) + len(self.pending)


class Relay:
    def __init__(self, args):
        self.args = args
        self.rooms = {}
        self.conns_by_ip = collections.Counter()
        self.misses = collections.defaultdict(collections.deque)   # ip -> times of failed JOIN/FIND
        self.started = time.time()
        self.log_file = None
        if args.log and args.log != os.devnull:
            self.log_file = open(args.log, "a", encoding="utf-8", buffering=1)

    # ---- small helpers ----
    def log(self, text):
        line = "{} {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), text)
        if self.log_file is not None:
            self.log_file.write(line + "\n")
        elif not self.args.log:
            print(line, flush=True)

    @staticmethod
    def valid_code(code):
        return CODE_MIN <= len(code) <= CODE_MAX and code.isalnum() and all(ord(c) < 128 for c in code)

    @staticmethod
    def is_local(ip):
        return ip in ("127.0.0.1", "::1")

    def too_many_misses(self, ip):
        """Wrong codes are rate-limited per address so room codes cannot be guessed by trying."""
        if self.is_local(ip) or not self.args.max_misses:
            return False
        q, now = self.misses[ip], time.time()
        while q and now - q[0] > 60.0:
            q.popleft()
        return len(q) >= self.args.max_misses

    def miss(self, ip):
        self.misses[ip].append(time.time())

    @staticmethod
    async def reply(writer, text):
        writer.write((text + "\n").encode("ascii"))
        try:
            await writer.drain()
        except Exception:
            pass

    @staticmethod
    def close(writer):
        try:
            writer.close()
        except Exception:
            pass

    @staticmethod
    def tune(writer):
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        for level, opt in ((socket.IPPROTO_TCP, socket.TCP_NODELAY), (socket.SOL_SOCKET, socket.SO_KEEPALIVE)):
            try:
                sock.setsockopt(level, opt, 1)
            except Exception:
                pass

    def drop_room_if_dead(self, room):
        if self.rooms.get(room.code) is room and room.control is None and not room.pipes and not room.pending:
            del self.rooms[room.code]
            self.log("room {} closed".format(room.code))

    # ---- connection entry point ----
    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = peer[0]
        self.conns_by_ip[ip] += 1
        try:
            limit = self.args.max_conns_per_ip
            if limit and self.conns_by_ip[ip] > limit and not self.is_local(ip):
                await self.reply(writer, "ERR SLOW_DOWN")
                return
            self.tune(writer)
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\n"), HELLO_TIMEOUT)
            except Exception:
                return      # silent, slow, too long or gone
            if len(raw) > HELLO_LIMIT:
                await self.reply(writer, "ERR BAD_REQUEST")
                return
            parts = raw.decode("ascii", "replace").split()
            if len(parts) < 3 or parts[0] != "S4OM" or not self.valid_code(parts[2]):
                await self.reply(writer, "ERR BAD_REQUEST")
                return
            verb, code = parts[1], parts[2].upper()
            if verb == "HOST":
                await self.host(reader, writer, ip, code)
            elif verb == "JOIN":
                await self.join(reader, writer, ip, code)
            elif verb == "DATA" and len(parts) >= 4:
                await self.data(reader, writer, ip, code, parts[3])
            elif verb == "FIND":
                await self.find(writer, ip, code)
            else:
                await self.reply(writer, "ERR BAD_REQUEST")
        except Exception as e:      # one bad connection must never take the server down
            self.log("error from {}: {!r}".format(ip, e))
        finally:
            self.conns_by_ip[ip] -= 1
            if self.conns_by_ip[ip] <= 0:
                del self.conns_by_ip[ip]
            self.close(writer)

    # ---- HOST: the control link ----
    async def host(self, reader, writer, ip, code):
        room = self.rooms.get(code)
        if room is not None:
            if room.host_ip != ip:
                await self.reply(writer, "ERR ROOM_TAKEN")
                return
            if room.control is not None:    # same host came back before we noticed the old link died
                self.close(room.control)
        else:
            if len(self.rooms) >= self.args.max_rooms:
                await self.reply(writer, "ERR SERVER_FULL")
                return
            room = self.rooms[code] = Room(code, ip)
            self.log("room {} opened by {}".format(code, ip))
        room.control, room.down_since = writer, None
        await self.reply(writer, "OK")
        last_heard = time.time()
        try:
            while room.control is writer:
                try:
                    line = await asyncio.wait_for(reader.readline(), PING_EVERY)
                except asyncio.TimeoutError:
                    if time.time() - last_heard > HOST_SILENT_AFTER:
                        break
                    writer.write(b"PING\n")
                    await writer.drain()
                    continue
                except ValueError:          # a line longer than we accept (e.g. a huge STAT): skip it
                    last_heard = time.time()
                    continue
                if not line:
                    break
                last_heard = time.time()    # "PONG" and "STAT ..." both just prove the host is alive
        except Exception:
            pass
        finally:
            if room.control is writer:
                room.control, room.down_since = None, time.time()
                self.log("room {} lost its host link".format(code))
                self.drop_room_if_dead(room)

    # ---- JOIN: a player wants into a room ----
    async def join(self, reader, writer, ip, code):
        if self.too_many_misses(ip):
            await self.reply(writer, "ERR SLOW_DOWN")
            return
        room = self.rooms.get(code)
        if room is None or room.control is None:
            self.miss(ip)
            await self.reply(writer, "ERR NO_ROOM")
            return
        if room.players() >= MAX_PLAYERS:
            await self.reply(writer, "ERR ROOM_FULL")
            return
        cid = os.urandom(8).hex()
        fut = asyncio.get_event_loop().create_future()
        room.pending[cid] = fut
        try:
            room.control.write("CONN {}\n".format(cid).encode("ascii"))
            await room.control.drain()
            host_reader, host_writer, done = await asyncio.wait_for(fut, DATA_WAIT)
        except Exception:
            room.pending.pop(cid, None)
            await self.reply(writer, "ERR HOST_TIMEOUT")
            self.drop_room_if_dead(room)
            return
        room.pending.pop(cid, None)
        room.pipes.add(cid)
        self.log("room {}: player joined from {} ({} in room)".format(code, ip, room.players()))
        try:
            await self.reply(host_writer, "OK")
            await self.reply(writer, "OK")
            await self.pipe(reader, writer, host_reader, host_writer)
        finally:
            room.pipes.discard(cid)
            if not done.done():
                done.set_result(None)
            self.log("room {}: player left ({} in room)".format(code, room.players()))
            self.drop_room_if_dead(room)

    # ---- DATA: the host's end of one joiner's pipe ----
    async def data(self, reader, writer, ip, code, cid):
        room = self.rooms.get(code)
        fut = room.pending.get(cid) if room is not None and room.host_ip == ip else None
        if fut is None or fut.done():
            await self.reply(writer, "ERR NO_ROOM")
            return
        done = asyncio.get_event_loop().create_future()
        fut.set_result((reader, writer, done))
        await done      # join() owns the pipe; keep this connection open until it ends

    async def find(self, writer, ip, code):
        if self.too_many_misses(ip):
            await self.reply(writer, "ERR SLOW_DOWN")
            return
        room = self.rooms.get(code)
        if room is not None and room.control is not None:
            await self.reply(writer, "OK")
        else:
            self.miss(ip)
            await self.reply(writer, "ERR NO_ROOM")

    # ---- copying bytes ----
    async def pipe(self, r1, w1, r2, w2):
        async def copy(reader, writer):
            try:
                while True:
                    chunk = await reader.read(COPY_CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            except Exception:
                pass

        tasks = [asyncio.ensure_future(copy(r1, w2)), asyncio.ensure_future(copy(r2, w1))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)   # one side closed -> close both
        finally:
            for t in tasks:
                t.cancel()
            self.close(w1)
            self.close(w2)

    # ---- housekeeping ----
    async def sweep(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for room in list(self.rooms.values()):
                self.drop_room_if_dead(room)
            for ip in [ip for ip, q in self.misses.items() if not q or now - q[-1] > 60.0]:
                del self.misses[ip]

    def status(self):
        open_rooms = [r for r in self.rooms.values() if r.control is not None]
        return {"rooms": len(open_rooms),
                "players": sum(1 + len(r.pipes) for r in open_rooms),
                "uptime": int(time.time() - self.started)}

    async def handle_status(self, reader, writer):
        try:
            await asyncio.wait_for(reader.readline(), 5)
            body = json.dumps(self.status()).encode("ascii")
            writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode("ascii") + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            self.close(writer)


async def main(args):
    relay = Relay(args)
    server = await asyncio.start_server(relay.handle, args.bind, args.port, limit=CONTROL_LINE_LIMIT)
    if args.status_port:
        await asyncio.start_server(relay.handle_status, args.status_bind or
                                   ("127.0.0.1" if args.bind in ("0.0.0.0", "::", "") else args.bind), args.status_port)
    asyncio.ensure_future(relay.sweep())
    relay.log("S4OM open relay listening on {}:{}".format(args.bind, args.port))
    print("READY", flush=True)
    async with server:
        await server.serve_forever()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="S4OM open relay: meet by room code, no port forwarding, no tokens.")
    p.add_argument("--port", type=int, default=7656, help="TCP port players connect to (default 7656)")
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on (default all)")
    p.add_argument("--status-port", type=int, default=0, help="HTTP port for a small JSON status page (off by default)")
    p.add_argument("--status-bind", default="", help="address for the status page (default: this machine only)")
    p.add_argument("--log", default="", help="log file (default: print to the terminal)")
    p.add_argument("--max-rooms", type=int, default=500)
    p.add_argument("--max-conns-per-ip", type=int, default=256,
                   help="connections allowed from one address at a time, 0 = no limit (a full room is ~23 from the "
                        "host's address; behind a proxy that hides addresses everyone shares one)")
    p.add_argument("--max-misses", type=int, default=40,
                   help="wrong room codes allowed per address per minute, 0 = no limit")
    p.add_argument("--open", action="store_true", help="accepted for compatibility; this relay never asks for tokens")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
