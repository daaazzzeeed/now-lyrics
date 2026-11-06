"""python -m lyrics_tv — uvicorn с одним воркером.

Слушающий сокет создаём сами, чтобы выставить TCP_USER_TIMEOUT: на Linux он наследуется
принятыми соединениями, и пропавший без FIN телевизор отваливается за ~30 с, а не за 15 минут —
иначе сервер продолжал бы опрашивать Spotify для пустого экрана.
"""
import socket

import uvicorn

from . import config as C


def make_socket() -> socket.socket:
    family = socket.AF_INET6 if ":" in C.HOST else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 30_000)
    sock.bind((C.HOST, C.PORT))
    sock.set_inheritable(True)
    return sock


def main() -> None:
    config = uvicorn.Config("lyrics_tv.main:app", workers=1, log_level="warning", access_log=False,
                            proxy_headers=False, timeout_graceful_shutdown=2)
    uvicorn.Server(config).run(sockets=[make_socket()])


if __name__ == "__main__":
    main()
