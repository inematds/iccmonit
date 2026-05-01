#!/usr/bin/env python3
"""Modo web — serve a TUI no navegador via textual-serve.

Uso:
    python3 serve.py [porta] [lan|<ip>]

- sem args        → bind em 127.0.0.1 (apenas local, padrão seguro)
- lan             → detecta IP da LAN e bind nele (acessível de outras máquinas)
- <ip>            → bind no IP fornecido (ex.: 192.168.1.50)

textual-serve embute o host de bind no websocket do HTML — bindar em 0.0.0.0
quebra a conexão no navegador. Por isso resolvemos o IP de antemão.
"""

import socket
import sys
import threading
import time
import webbrowser

from textual_serve.server import Server

DEFAULT_PORT = 8000


def detect_lan_ip() -> str:
    """Descobre o IP da LAN abrindo um socket UDP pro gateway (não envia pacote)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def open_browser_after(url: str, delay: float = 1.0) -> None:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def resolve_host(arg: str | None) -> tuple[str, bool]:
    """Retorna (host, is_lan)."""
    if arg == "lan":
        return detect_lan_ip(), True
    if arg:
        return arg, arg not in ("127.0.0.1", "localhost")
    return "127.0.0.1", False


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    host, is_lan = resolve_host(sys.argv[2] if len(sys.argv) > 2 else None)

    url = f"http://{host}:{port}"
    print(f"Servindo em {url}  (Ctrl+C para sair)")
    if is_lan:
        print(f"  ⚠ MODO LAN — qualquer máquina em {host}/24 pode acessar")
        print("    SEM AUTENTICAÇÃO. Use só em rede de confiança.")
    else:
        print("  ⚠ sem autenticação — pra acesso remoto use SSH tunnel ou modo lan")
    open_browser_after(url)
    Server(
        command="python3 monitor.py",
        host=host,
        port=port,
        title="iccmonit",
    ).serve()


if __name__ == "__main__":
    main()
