# iccmonit — INEMA Claude Monitor

TUI em Python que monitora **todas as sessões Claude Code ativas** na máquina em tempo real, com cota 5h/7d, métricas por sessão e chat embutido via OAuth.

Pensado para rodar em um **terminal separado** ao lado das suas sessões Claude Code, dando visão consolidada de:

- Sessões ativas (busy / idle), modelo em uso, % do contexto consumido
- Cota da API: janela de 5 horas, semanal global e semanal Sonnet (separada)
- Tamanho do `CLAUDE.md`, da memória do projeto, agentes lançados e skills invocadas
- Chat com Claude (Haiku por padrão) ciente do estado do painel

## Requisitos

- Python 3.10+
- Conta com Claude Code instalado e logado (precisa de `~/.claude/.credentials.json`)
- O statusline padrão do Claude Code rodando — é ele que popula `/tmp/cc_limits_<uid>.json` com a cota oficial da API

## Instalação

```bash
git clone git@github.com:inematds/iccmonit.git
cd iccmonit
pip install textual anthropic --break-system-packages
```

## Uso

```bash
./start.sh             # TUI no terminal atual
./start.sh web         # serve no navegador em http://localhost:8000
./start.sh web 9000    # serve em http://localhost:9000
./start.sh -h          # ajuda
```

O `start.sh` instala dependências em falta e executa `monitor.py`. O modo `web` usa `textual serve` para renderizar a TUI no navegador via xterm.js — útil pra acompanhar de outra máquina na rede local.

Atalhos dentro da TUI:

| Tecla | Ação |
|-------|------|
| `r`   | Refresh manual |
| `q`   | Sair |

Atualização automática a cada 10s (configurável em `config.json`).

## Fontes de dados

| Métrica | Fonte |
|---|---|
| Sessões ativas | `~/.claude/sessions/*.json` (um arquivo por PID vivo) |
| Contexto %, modelo, custo | Última msg `assistant` com `usage` no transcript JSONL em `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` |
| Cota 5h / 7d / 7d-Sonnet | `/tmp/cc_limits_<uid>.json` (cache do statusline — fonte oficial via API Anthropic) |
| `CLAUDE.md` | `<cwd>/CLAUDE.md` |
| Memória | `~/.claude/projects/<encoded-cwd>/memory/` |
| Agentes lançados | Tool uses `Agent` no transcript |
| Skills invocadas | Tool uses `Skill` no transcript (distintas) |
| Chat OAuth | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` |

A cota **não** é recalculada localmente como `ccm`/`ccusage` fazem — é lida do cache que o próprio Claude Code mantém com os valores oficiais da API. Isso evita divergência.

## Configuração — `config.json`

Editável sem tocar no código. Define:

- **`thresholds`** — limites de cor por métrica (azul → verde → amarelo → vermelho)
  - `quota_pct`, `context_pct`, `memory_kb`, `memory_files`, `claude_md_bytes`, `agents_per_session`, `cost_per_session_usd`
- **`alerts`** — habilita/desabilita alertas individualmente e customiza a mensagem
- **`chat.model`** — modelo do chat embutido (padrão: `claude-haiku-4-5-20251001`)
- **`chat.max_tokens`** — tamanho da resposta
- **`refresh_interval_seconds`** — intervalo de auto-refresh

Esquema de cores:

| Cor | Significado |
|-----|-------------|
| 🔵 Azul | Muito baixo / ocioso |
| 🟢 Verde | Normal / saudável |
| 🟡 Amarelo | Atenção |
| 🔴 Vermelho | Crítico / alerta |

## Arquitetura

```
iccmonit/
├── monitor.py     # TUI principal (Textual)
├── start.sh       # Entrypoint — instala deps e roda
├── config.json    # Thresholds e alertas
└── docs/
    ├── PLAN.md      # Plano original e decisões de arquitetura
    └── RESEARCH.md  # Pesquisa das fontes de dados do Claude Code
```

### Encoding do `cwd`

Barras e dois pontos viram hífens, com hífen prefixado:

```
/home/user/projetos/foo  →  -home-user-projetos-foo
```

## Versionamento

Constante `VERSION` em `monitor.py` no formato **`v1.xx.yy`**:

- **major (`1`)** — só muda na transição para V2
- **`xx`** — incrementa a cada **recurso novo** (sequencial: `01`, `02`, ...)
- **`yy`** — incrementa a cada **bug fix** (sequencial; reinicia em `00` quando `xx` sobe)

## Roadmap

- **V1** *(atual)* — painel de sessões + cota + chat geral com contexto do painel + modo web (`./start.sh web`)
- **V2** *(planejado)* — interação remota com sessão ativa: selecionar uma sessão e injetar prompts nela como se estivesse no terminal original (mecanismo a investigar — stdin do processo, IPC, ou canal próprio)

## Por que não usar X?

- **`ccm` / claude-monitor** — calcula cota a partir dos JSONL locais e diverge da cota real da API. Aqui usamos o cache oficial.
- **`ccusage`** — ferramenta de auditoria/relatório, não tempo real.
- **`claude-pulse`** — o `statusline-command.sh` existente já cobre o mesmo escopo.

## Licença

MIT.
