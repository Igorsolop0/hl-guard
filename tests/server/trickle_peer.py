from __future__ import annotations

import socket
import sys
import threading
import time

GAP_S = 0.02


def endless(conn: socket.socket, first: bytes) -> None:
    conn.sendall(first)
    while True:
        conn.sendall(b"a")
        time.sleep(GAP_S)


def stall(conn: socket.socket) -> None:
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
    time.sleep(0.3)
    conn.sendall(b"[")
    time.sleep(3600)


def tunnel(conn: socket.socket) -> None:
    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
    conn.recv(65536)
    endless(conn, b"\x16\x03\x03\x40\x00\x02\x00\x30\x00")


MODES = {
    "header": lambda conn: endless(conn, b"HTTP/1.1 200 OK\r\nX-Slow: "),
    "proxy": lambda conn: endless(conn, b"HTTP/1.1 200 Connection established\r\nX-Slow: "),
    "tls": tunnel,
    "stall": stall,
}


def serve(conn: socket.socket, mode: str) -> None:
    try:
        conn.recv(65536)
        MODES[mode](conn)
    except OSError:
        pass
    finally:
        conn.close()


def main() -> None:
    server = socket.create_server(("127.0.0.1", 0), backlog=512)
    print(server.getsockname()[1], flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve, args=(conn, sys.argv[1]), daemon=True).start()


if __name__ == "__main__":
    main()
