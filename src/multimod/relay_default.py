"""The relay this build uses when the player's config names none.

"host" or "host:port" of a server running relay/relay.py (port 7656 if left out), or "" for
none. `python build.py --relay host:port` rewrites this file; players can override it in game
with `mp_relay <address>`.
"""

DEFAULT_RELAY = ""
