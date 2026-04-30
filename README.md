# iccmonit — INEMA Claude Monitor

TUI em Python que monitora **todas as sessões Claude Code ativas** na máquina em tempo real, com cota 5h/7d, métricas por sessão e chat embutido via OAuth.

Pensado para rodar em um **terminal separado** ao lado das suas sessões Claude Code, dando visão consolidada de:

- Sessões ativas (busy / idle), modelo em uso, % do contexto consumido
- Cota da API: janela de 5 horas, semanal global e semanal Sonnet (separada)
- Tamanho do `CLAUDE.md`, da memória do projeto, agentes lançados e skills invocadas
- Chat com Claude (Haiku por padrão) ciente do estado do painel

Versão atual: **v1.01.00**

---

## Sumário

- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Uso](#uso)
  - [Modo terminal](#modo-terminal)
  - [Modo web](#modo-web)
  - [Atalhos](#atalhos-de-teclado)
- [Painéis da TUI](#painéis-da-tui)
- [Fontes de dados](#fontes-de-dados)
- [Configuração](#configuração--configjson)
- [Arquitetura](#arquitetura)
- [Versionamento](#versionamento)
- [Roadmap](#roadmap)
- [Solução de problemas](#solução-de-problemas)
- [Por que não usar X?](#por-que-não-usar-x)
- [Licença](#licença)

---

## Requisitos

- **Python 3.10+**
- **Claude Code instalado e logado** (precisa de `~/.claude/.credentials.json` válido)
- **Statusline padrão do Claude Code rodando** — é ele que popula `/tmp/cc_limits_<uid>.json` com a cota oficial da API. Sem isso o painel de cota fica vazio.
- Sistema operacional **Linux** ou **macOS** (testado no Linux). O `tac` é usado para ler transcripts de trás pra frente — em macOS tem o equivalente.

## Instalação

```bash
git clone git@github.com:inematds/iccmonit.git
cd iccmonit
chmod +x start.sh
```

Dependências (instaladas automaticamente pelo `start.sh` se faltarem):

```bash
pip install textual anthropic --break-system-packages
```

## Uso

### Modo terminal

```bash
./start.sh
```

Abre a TUI no terminal atual. Ideal para rodar lado-a-lado com as sessões Claude Code.

### Modo web

```bash
./start.sh web              # http://localhost:8000  (porta padrão)
./start.sh web 9000         # http://localhost:9000  (porta custom)
```

Usa **`textual serve`** para empacotar a TUI atual num WebSocket + xterm.js no navegador, sem mudança de código. Útil pra:

- Acompanhar de outra máquina na rede local (acesse `http://<ip-da-maquina>:8000`)
- Manter o monitor visível num browser sem ocupar terminal
- Compartilhar visão temporária com colegas na mesma rede

> **Atenção**: o modo web **não tem autenticação**. Não exponha em rede pública — use só em rede local de confiança ou atrás de VPN/SSH tunnel.

### Ajuda

```bash
./start.sh -h    # mostra os modos disponíveis
```

### Atalhos de teclado

| Tecla | Ação |
|-------|------|
| `r`   | Refresh manual |
| `q`   | Sair |

Auto-refresh a cada **10 segundos** por padrão (configurável em `config.json` via `refresh_interval_seconds`).

---

## Painéis da TUI

A TUI tem três seções verticais:

### 1. Cota (topo)

Barra horizontal por janela:

| Linha | Significado |
|-------|-------------|
| `5h`  | Janela rolante de 5 horas — todos os modelos |
| `7d`  | Janela semanal — todos os modelos |
| `7d♪` | Janela semanal **Sonnet** — limite separado, importante monitorar |

Cor da barra segue os thresholds de `quota_pct` em `config.json`. Se a barra aparecer "aguardando API..." é porque o cache `/tmp/cc_limits_<uid>.json` está vazio ou velho (> 2 min).

### 2. Sessões ativas (meio)

Tabela com uma linha por sessão Claude Code viva (PID alive). Colunas:

| Coluna | Descrição |
|--------|-----------|
| **Projeto** | Nome do diretório de trabalho (truncado em 20 chars) |
| **Status** | `idle` (verde) ou `busy` (amarelo) |
| **Modelo** | `opus`, `sonnet`, `haiku` ou outro |
| **Ctx%** | % da janela de 1M tokens em uso. Soma `input + cache_read + cache_creation + output` |
| **CLAUDE.md** | Tamanho do `CLAUDE.md` no `cwd` da sessão |
| **Mem** | `<n>f/<x>k` — número de arquivos e KB totais em `memory/` |
| **Agts** | Quantidade de chamadas à tool `Agent` no transcript |
| **Skls** | Skills distintas invocadas (tool `Skill`) |
| **Update** | Tempo desde o último update (`12s`, `3m`, `1h`) |

Cada métrica recebe cor azul/verde/amarelo/vermelho conforme thresholds em `config.json`.

### 3. Chat (rodapé)

Chat embutido com Claude com **dois modos**:

**Modo geral** (padrão) — recebe o estado completo do painel como system prompt:

- "Qual sessão tá usando mais contexto?"
- "Quanto falta pra cota Sonnet?"
- "Algum projeto com memória crítica?"

**Modo focado** — selecione uma linha da tabela (↑/↓ + **Enter**) e o chat carrega o transcript daquela sessão como contexto. Aí dá pra perguntar sobre o trabalho específico:

- "O que essa sessão tá fazendo agora?"
- "Qual foi o último erro?"
- "Resume o que foi feito até agora."
- "Por que ele tomou essa decisão?"

> O chat é um **observador analítico** — não interage com o agente daquela sessão, só lê o transcript. Pra interação remota real, ver V2 no roadmap.

#### Comandos do chat

| Comando | Ação |
|---------|------|
| `/clear` | Volta ao modo geral (limpa o foco e o histórico) |
| `/help`  | Lista de comandos |

Trocar de foco também limpa o histórico do chat — pra não misturar conversas.

Modelo padrão: `claude-haiku-4-5-20251001` (configurável em `config.json` → `chat.model`). Autenticação via OAuth do Claude Code (token em `~/.claude/.credentials.json`).

---

## Fontes de dados

| Métrica | Fonte |
|---|---|
| Sessões ativas | `~/.claude/sessions/*.json` — um arquivo por PID, filtrado por processo vivo (`os.kill(pid, 0)`) |
| Modelo, contexto %, custo | Última mensagem `assistant` com `usage` no transcript JSONL: `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` |
| Cota 5h / 7d / 7d-Sonnet | `/tmp/cc_limits_<uid>.json` — cache do statusline (fonte oficial via API Anthropic) |
| `CLAUDE.md` | `stat` do arquivo em `<cwd>/CLAUDE.md` |
| Memória | Conta arquivos e soma bytes de `~/.claude/projects/<encoded-cwd>/memory/` |
| Agentes lançados | Tool uses `name == "Agent"` no transcript (acumulado na sessão) |
| Skills invocadas | Tool uses `name == "Skill"` no transcript (distintas) |
| Chat OAuth | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`. Header: `anthropic-beta: oauth-2025-04-20` |

> A cota **não** é recalculada localmente como `ccm`/`ccusage` fazem — é lida do cache que o próprio Claude Code mantém com os valores oficiais da API. Isso evita divergência.

### Encoding do `cwd`

Barras e dois pontos viram hífens, com hífen prefixado:

```
/home/user/projetos/foo  →  -home-user-projetos-foo
```

O monitor tenta ambas as variações (com e sem hífen prefixado) ao localizar transcript e diretório de memória.

---

## Configuração — `config.json`

Editável sem tocar no código. Estrutura:

### `thresholds`

Limites de cor por métrica. Cada métrica tem 4 níveis: `blue` ≤ `green` ≤ `yellow` ≤ `red`.

| Métrica | O que mede | Defaults (b/g/y/r) |
|---------|------------|--------------------|
| `quota_pct`           | % da cota usada (5h, 7d, 7d-Sonnet) | 20 / 60 / 85 / 95 |
| `context_pct`         | % da janela de contexto da sessão. >90% = risco de auto-compaction | 10 / 50 / 75 / 90 |
| `memory_kb`           | Tamanho total da memória do projeto (KB). >600 = considere `/memory-audit` | 0 / 100 / 300 / 600 |
| `memory_files`        | Número de arquivos de memória. >35 = fragmentação | 0 / 10 / 20 / 35 |
| `claude_md_bytes`     | Tamanho do `CLAUDE.md`. >40KB = pode impactar contexto | 0 / 8192 / 20480 / 40960 |
| `agents_per_session`  | Agentes lançados na sessão. >30 = sessão muito pesada | 0 / 5 / 15 / 30 |
| `cost_per_session_usd`| Custo acumulado em USD. >$20 = revisar eficiência | 0 / 2 / 8 / 20 |

Esquema visual:

| Cor | Significado |
|-----|-------------|
| 🔵 Azul | Muito baixo / ocioso |
| 🟢 Verde | Normal / saudável |
| 🟡 Amarelo | Atenção |
| 🔴 Vermelho | Crítico / alerta |

### `alerts`

Habilita/desabilita mensagens de alerta individualmente:

| Chave | Padrão | Mensagem |
|-------|--------|----------|
| `quota_5h_red`  | enabled  | ⚠ Cota 5h crítica! |
| `quota_7d_red`  | enabled  | ⚠ Cota semanal crítica! |
| `context_red`   | enabled  | ⚠ Contexto alto — risco de compaction |
| `memory_red`    | enabled  | ⚠ Memória excessiva — rode `/memory-audit` |
| `claude_md_red` | disabled | ⚠ CLAUDE.md muito grande |
| `cost_red`      | enabled  | ⚠ Sessão cara |

### `chat`

| Campo | Padrão | Descrição |
|-------|--------|-----------|
| `model`      | `claude-haiku-4-5-20251001` | Modelo do chat embutido |
| `max_tokens` | `1024` | Tamanho máximo da resposta |

### Outros

| Campo | Padrão | Descrição |
|-------|--------|-----------|
| `title` | `INEMA Claude Monitor` | Título do header (a versão é appendada automaticamente) |
| `refresh_interval_seconds` | `10` | Intervalo de auto-refresh em segundos |

---

## Arquitetura

```
iccmonit/
├── monitor.py     # TUI principal (Textual) — V1
├── start.sh       # Entrypoint: terminal e modo web
├── config.json    # Thresholds, alertas, chat, refresh
├── README.md      # Este arquivo
├── CLAUDE.md      # Instruções do projeto pra Claude Code
├── .gitignore
└── docs/
    ├── PLAN.md      # Plano original e decisões de arquitetura
    └── RESEARCH.md  # Pesquisa das fontes de dados do Claude Code
```

### Componentes do `monitor.py`

| Símbolo | Papel |
|---------|-------|
| `load_sessions()` | Lê `~/.claude/sessions/*.json` e filtra por PID vivo |
| `get_transcript_usage()` | Lê última `assistant.usage` do transcript via `tac` |
| `get_session_extras()` | Calcula tamanho de `CLAUDE.md`, memória, contagem de agentes/skills |
| `load_quota()` | Lê `/tmp/cc_limits_<uid>.json` (descarta se > 120s) |
| `get_oauth_token()` | Extrai `accessToken` do `.credentials.json` |
| `QuotaBar` | Widget com barras de cota |
| `SessionTable` | DataTable com sessões |
| `ChatPane` | RichLog + Input — chat com `system` montado a cada mensagem com snapshot do painel |
| `MonitorApp` | App Textual; auto-refresh via `set_interval` |

---

## Versionamento

Constante `VERSION` em `monitor.py` no formato **`v1.xx.yy`**:

- **major (`1`)** — só muda na transição para V2
- **`xx`** — incrementa a cada **recurso novo** (sequencial: `01`, `02`, ...)
- **`yy`** — incrementa a cada **bug fix** (sequencial; reinicia em `00` quando `xx` sobe)

Exemplos:
- `v1.00.00` → estado inicial
- `v1.01.00` → primeiro recurso novo após inicial
- `v1.01.03` → terceiro bug fix dentro do recurso `01`
- `v1.02.00` → novo recurso (zera bugs)

A versão é exibida no título da janela TUI (header).

---

## Roadmap

- **V1** *(atual)* — painel de sessões + cota + chat geral + chat focado em sessão (read-only) + modo web
- **V2** *(planejado)* — interação remota com sessão ativa: injetar prompts numa sessão como se estivesse no terminal original. Mecanismo a investigar:
  - stdin do processo Claude Code
  - arquivo de IPC
  - ou `claude --resume <sessionId>` num processo paralelo (forka, não dirige a sessão original)
  - sem API oficial — requer pesquisa

---

## Solução de problemas

| Sintoma | Causa provável | Como resolver |
|---------|----------------|---------------|
| Cota mostra `aguardando API...` | `/tmp/cc_limits_<uid>.json` ausente ou > 2 min | Confirme que o statusline padrão do Claude Code está rodando — ele popula esse cache |
| Chat indisponível | Token OAuth não encontrado | Faça login novamente no Claude Code e confirme que `~/.claude/.credentials.json` tem `claudeAiOauth.accessToken` |
| Tabela vazia | Nenhuma sessão Claude Code ativa | Abra uma sessão `claude` em outro terminal e dê `r` para refresh |
| `Ctx%` zerado | Transcript ainda sem mensagem `assistant` com `usage` | Use a sessão um pouco — depois da primeira resposta o valor aparece |
| Modo web não abre | Porta ocupada | Tente outra porta: `./start.sh web 9001` |
| Erro `ImportError: textual` | Dependências não instaladas | Rode `pip install textual anthropic --break-system-packages` ou simplesmente `./start.sh` (instala automaticamente) |

---

## Por que não usar X?

- **`ccm` / claude-monitor** — calcula cota a partir dos JSONL locais e diverge da cota real da API. Aqui usamos o cache oficial.
- **`ccusage`** — ferramenta de auditoria/relatório, não tempo real.
- **`claude-pulse`** — o `statusline-command.sh` existente já cobre o mesmo escopo.

---

## Licença

MIT.
