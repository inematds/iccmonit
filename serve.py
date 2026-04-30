#!/usr/bin/env python3
"""Modo web — serve a TUI no navegador via textual-serve."""

import sys

from textual_serve.server import Server

DEFAULT_PORT = 8000


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    host = "0.0.0.0"
    print(f"Servindo em http://localhost:{port}  (Ctrl+C para sair)")
    print(f"  → na LAN: http://<seu-ip>:{port}")
    print("  ⚠ sem autenticação — use só em rede de confiança")
    Server(
        command="python3 monitor.py",
        host=host,
        port=port,
        title="iccmonit",
    ).serve()


if __name__ == "__main__":
    main()
