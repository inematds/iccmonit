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

usage() {
    cat <<EOF
Uso:
  ./start.sh             roda a TUI no terminal atual
  ./start.sh web [porta] serve a TUI no navegador (padrão: http://localhost:${DEFAULT_WEB_PORT})
  ./start.sh -h          mostra esta ajuda
EOF
}

case "${1:-}" in
    web)
        ensure_deps
        PORT="${2:-$DEFAULT_WEB_PORT}"
        echo "Servindo em http://localhost:${PORT}  (Ctrl+C para sair)"
        exec python3 -m textual serve --port "$PORT" "python3 monitor.py"
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
