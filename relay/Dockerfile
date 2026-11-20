# S4OM open relay as a container.  Build context: this folder.
#   docker build -t s4om-relay .
#   docker run -d --restart unless-stopped -p 7656:7656 s4om-relay
FROM python:3.12-alpine

# nothing to install: relay.py uses the standard library only
RUN adduser -D -H relay
WORKDIR /app
COPY relay.py /app/relay.py

USER relay
ENV PYTHONUNBUFFERED=1
EXPOSE 7656

# Raw TCP, not HTTP: ask the relay about a room nobody opened; any "OK"/"ERR ..." line means it is serving.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import socket,sys; s=socket.create_connection(('127.0.0.1',7656),3); s.sendall(b'S4OM FIND HEALTH\\n'); sys.exit(0 if s.recv(16)[:2] in (b'OK', b'ER') else 1)"]

# logs go to stdout (docker logs / the Logs tab); the status page stays inside the container
CMD ["python", "relay.py", "--port", "7656", "--status-port", "7657"]
