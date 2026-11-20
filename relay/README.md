# S4OM open relay

`relay.py` lets players meet by room code without anyone forwarding ports. The host and every
joiner connect *out* to it; it pairs them by code and then only copies bytes between them.

- No accounts, no tokens. Whoever knows the server's address and a room code can join that room.
  A room can still have a password; the host's game checks it, not the relay.
- It never reads, changes or stores game traffic. It logs when rooms open and close and the
  addresses that connect.
- One file, Python 3.7 or newer, standard library only. One TCP port (7656 by default).
- All game traffic passes through this machine, so pick one reasonably close to the players.
  A small VPS is plenty: the load is a few kB/s per player.

## Run it

```
python3 relay.py                     # listens on 0.0.0.0:7656, logs to the terminal
python3 relay.py --port 7656 --status-port 7657 --log /var/log/s4om-relay.log
```

Open the port for TCP in the server's firewall (and in the provider's panel, if it has one):

```
sudo ufw allow 7656/tcp
```

## Keep it running (systemd)

Copy `relay.py` to the server, e.g. `/opt/s4om/relay.py`, then create
`/etc/systemd/system/s4om-relay.service`:

```
[Unit]
Description=S4OM open relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/s4om/relay.py --port 7656 --status-port 7657
Restart=always
RestartSec=2
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now s4om-relay
sudo systemctl status s4om-relay        # should say "active (running)"
journalctl -u s4om-relay -f             # the log
curl http://127.0.0.1:7657/             # {"rooms": 0, "players": 0, "uptime": 12}
```

The status page listens on the server itself only (127.0.0.1) unless you pass `--status-bind`.

## Point the mod at it

Build the mod with the address baked in, so players have nothing to set up:

```
python build.py --relay your.server.example:7656
```

or, in a game that already has the mod: `mp_relay your.server.example:7656`.

Check from any PC that the server answers (expect `ERR NO_ROOM` - it is up, that room is not open):

```
python -c "import socket;s=socket.create_connection(('your.server.example',7656),5);s.sendall(b'S4OM FIND TEST00\n');print(s.recv(64))"
```

## Limits

| | default | flag |
|---|---|---|
| players per room | 12 (host + 11) | fixed, same as the mod |
| open rooms | 500 | `--max-rooms` |
| connections per address | 64 | `--max-conns-per-ip` |
| wrong room codes per address per minute | 40 | `--max-misses` |

Room codes are 6 characters from a 32-letter alphabet (about a billion combinations); the last
limit is what stops someone from finding a room by trying codes.

## Protocol

One text line, then raw bytes. `<token>` is accepted and ignored.

```
S4OM HOST <code> <token>   -> OK    control link: relay sends "PING" (answer "PONG") and "CONN <cid>" per joiner
S4OM DATA <code> <cid>     -> OK    host's link for that joiner; raw bytes from here on
S4OM JOIN <code> <token>   -> OK    raw bytes from here on
S4OM FIND <code>           -> OK    is that room open?
```

Refusals: `ERR NO_ROOM`, `ERR ROOM_FULL`, `ERR ROOM_TAKEN`, `ERR HOST_TIMEOUT`, `ERR BAD_REQUEST`,
`ERR SLOW_DOWN`, `ERR SERVER_FULL`.

A room lives while its host's control link is up. If that link drops, players already in the
room stay connected and the same host (same address) can re-open the room under the same code.
