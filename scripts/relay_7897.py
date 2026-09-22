"""Minimal TCP relay: expose host 127.0.0.1:7897 to the WSL/Docker VM.

Listens on 0.0.0.0:17897 and forwards every connection to the local
verge-mihomo mixed port, so the Docker daemon inside the WSL VM (which can
reach the host only through the NAT gateway IP) has a reachable proxy hop.
"""

import socket
import threading

LISTEN = ("0.0.0.0", 17897)
UPSTREAM = ("127.0.0.1", 7897)


def pump(src, dst):
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client):
    try:
        upstream = socket.create_connection(UPSTREAM, timeout=10)
    except OSError:
        client.close()
        return
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pump, args=(upstream, client), daemon=True).start()


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(LISTEN)
    server.listen(64)
    print("relay listening on %s:%d -> %s:%d" % (LISTEN[0], LISTEN[1], UPSTREAM[0], UPSTREAM[1]), flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()