#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

DEFAULT_WEB_PORT=8000

ensure_deps() {
    if ! python3 -c "import textual, anthropic" 2>/dev/null; then
        echo "Instalando dependências..."
        pip install textual anthropic --break-system-packages
    fi
}

ensure_web_deps() {
    ensure_deps
    if ! python3 -c "import textual_serve" 2>/dev/null; then
        echo "Instalando textual-serve (modo web)..."
        pip install textual-serve --break-system-packages
    fi
}

usage() {
    cat <<EOF
Uso:
  ./start.sh                       roda a TUI no terminal atual
  ./start.sh web [porta]           serve no navegador local (127.0.0.1:${DEFAULT_WEB_PORT})
  ./start.sh web [porta] lan       serve na LAN (detecta o IP automaticamente)
  ./start.sh web [porta] <ip>      serve num IP específico (ex.: 192.168.1.50)
  ./start.sh -h                    mostra esta ajuda
EOF
}

case "${1:-}" in
    web)
        ensure_web_deps
        PORT="${2:-$DEFAULT_WEB_PORT}"
        HOST_MODE="${3:-}"
        if [ -n "$HOST_MODE" ]; then
            exec python3 serve.py "$PORT" "$HOST_MODE"
        else
            exec python3 serve.py "$PORT"
        fi
        ;;
    -h|--help|help)
        usage
        ;;
    "")
        ensure_deps
        exec python3 monitor.py
        ;;
    *)
        echo "Comando desconhecido: $1" >&2
        usage
        exit 1
        ;;
esac
