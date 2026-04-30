# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## O que é este projeto

**INEMA Claude Monitor (`iccmonit`)** — TUI em Python que roda em terminal separado e monitora todas as sessões Claude Code ativas na máquina em tempo real, com chat embutido via OAuth.

## Como rodar

```bash
./start.sh             # TUI no terminal
./start.sh web         # serve no navegador (porta padrão 8000)
./start.sh web 9000    # serve em porta custom
```

Teclas: `r` = refresh manual, `q` = sair.

Modo web usa `python3 -m textual serve` — empacota a TUI atual num WebSocket+xterm.js sem mudança de código.

## Dependências

```bash
pip install textual anthropic --break-system-packages
```

## Arquitetura

```
iccmonit/
├── monitor.py     # TUI principal (Textual)
├── config.json    # thresholds de cor e alertas — editável sem tocar no código
└── docs/
    ├── PLAN.md    # plano original e decisões de arquitetura
    └── RESEARCH.md # pesquisa das fontes de dados do Claude Code
```

## Fontes de dados

| Dado | Fonte | Notas |
|---|---|---|
| Sessões ativas | `~/.claude/sessions/*.json` | Um arquivo por PID ativo |
| Contexto %, modelo, custo | transcript JSONL — última msg `assistant` com campo `usage` | Path: `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` |
| Cota 5h / 7d / 7d-Sonnet | `/tmp/cc_limits_<uid>.json` | Cache 60s populado pelo statusline — fonte oficial (API Anthropic) |
| CLAUDE.md | `<cwd>/CLAUDE.md` | stat do arquivo |
| Memória | `~/.claude/projects/<encoded-cwd>/memory/` | Conta arquivos e soma bytes |
| Agentes lançados | transcript — tool_use com `name == "Agent"` | Contagem acumulada na sessão |
| Skills invocadas | transcript — tool_use com `name == "Skill"` | Conta distintas |
| Chat OAuth | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` | Header: `anthropic-beta: oauth-2025-04-20` |

## Encoding do cwd

Barras e dois pontos viram hífens, com hífen prefixado:
`/home/nmaldaner/projetos/skill` → `-home-nmaldaner-projetos-skill`

## Config (`config.json`)

Thresholds de cor por métrica. Esquema de cores:
- **Azul** — muito baixo / ocioso
- **Verde** — normal / saudável
- **Amarelo** — atenção
- **Vermelho** — crítico / alerta

Alertas podem ser habilitados/desabilitados individualmente em `alerts`. O modelo do chat e o intervalo de refresh também ficam aqui.

## Plano de versões

- **V1 (atual)** — painel de sessões + cota + chat geral com contexto do painel
- **V2** — interação remota com sessão ativa (injetar prompts em sessão Claude Code em execução — mecanismo a investigar)

## Esquema de versionamento

Constante `VERSION` em `monitor.py` segue o padrão **`v1.xx.yy`**:

- **major (`1`)** — só muda quando vira V2 (mudança de plano).
- **`xx`** — incrementa a cada **recurso novo** (feature). Sequencial: `01`, `02`, `03`...
- **`yy`** — incrementa a cada **bug fix**. Sequencial: `01`, `02`, `03`...
- Ao subir `xx`, `yy` reinicia em `00`.
- Ao subir a major, `xx` e `yy` reiniciam em `00`.

Sempre que adicionar feature ou corrigir bug, atualizar `VERSION` em `monitor.py:18` antes de finalizar.

## Decisões importantes

- **Não usar `ccm` (claude-monitor)** — calcula cota dos JSONLs locais e erra. Nossa fonte é a API via cache do statusline.
- **Não usar `ccusage`** para monitoramento — é ferramenta de auditoria/relatório, não tempo real.
- **Não usar `claude-pulse`** — o `statusline-command.sh` existente já cobre o mesmo escopo com comportamento customizado.
- **Plan Max20** detectado via `~/.claude/.credentials.json` → `claudeAiOauth.rateLimitTier: "default_claude_max_20x"`.
- **Cota semanal Sonnet separada** — campo `seven_day_sonnet` no cache de limits. Importante monitorar pois tem limite próprio.
