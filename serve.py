#!/usr/bin/env python3
"""Modo web — serve a TUI no navegador via textual-serve.

Bind em 127.0.0.1 (apenas local). A URL do websocket que o textual-serve
embute no HTML é o próprio host de bind — usar 0.0.0.0 quebra a conexão
no navegador. Pra acesso remoto, use SSH tunnel:

    ssh -L 8000:localhost:8000 usuario@maquina
"""

import sys
import threading
import time
import webbrowser

from textual_serve.server import Server

DEFAULT_PORT = 8000
HOST = "127.0.0.1"


def open_browser_after(url: str, delay: float = 1.0) -> None:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    url = f"http://localhost:{port}"
    print(f"Servindo em {url}  (Ctrl+C para sair)")
    print("  ⚠ sem autenticação — pra acesso remoto use SSH tunnel")
    open_browser_after(url)
    Server(
        command="python3 monitor.py",
        host=HOST,
        port=port,
        title="iccmonit",
    ).serve()


if __name__ == "__main__":
    main()
