# Plano de Implementação — Claude Code Session Monitor

## Sumário executivo

Construir um TUI (`monitor.py`) que roda em terminal separado com três painéis principais:
1. **Sessões ativas** — todas as sessões Claude Code em tempo real (contexto %, status, modelo, custo, memória)
2. **Chat geral** — chat direto com Claude API embutido no painel, sem precisar abrir outra janela
3. **Cota global** — cota 5h/7d lida do cache existente do statusline

Dados primários vêm do filesystem que o Claude Code escreve (sessões, transcripts). Cota via `/tmp/cc_limits_<uid>.json` (já populado pelo statusline atual). Chat via Anthropic SDK direto.

**Esforço estimado:** ~1 dia para V1 completo.

## Versões

### V1 — TUI com sessões + chat geral
- Painel de sessões ativas: projeto, status (busy/idle), modelo, ctx %, custo, última atividade
- Chat geral embutido: Haiku por padrão, sem contexto das sessões
- Cota 5h/7d lida do cache do statusline
- Stack: Python + `textual`

### V2 — Interação remota com sessão ativa
- Selecionar uma sessão no painel e enviar prompts para ela como se estivesse dentro
- Mecanismo a investigar: stdin do processo, arquivo de IPC, ou outro canal do Claude Code
- Não há API oficial — requer pesquisa de viabilidade antes de implementar

## Arquitetura em 3 fases

### Fase 1 — MVP filesystem-only (meio dia)

Objetivo: ver todas as sessões ativas, contexto, custo aproximado, memória e CLAUDE.md por sessão. **Sem cota** nessa fase.

```
monitor.py
├── poll ~/.claude/sessions/*.json a cada 1s
│     → lista de sessões ativas (pid, cwd, status, sessionId, version)
├── pra cada sessão, deriva caminhos:
│     ├── transcript:  ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl
│     ├── memory:      ~/.claude/projects/<encoded-cwd>/memory/
│     └── CLAUDE.md:   <cwd>/CLAUDE.md
├── tail última linha do transcript com `usage` → tokens atuais
├── stat memory dir e CLAUDE.md → tamanhos
└── render TUI (rich/textual)
```

Produto da Fase 1: dashboard funcional sem cota, atualização a cada 1s.

### Fase 2 — Hook de statusline para cota (1-2 horas)

Adiciona wrapper na statusline que duplica o JSON pra arquivo de estado:

```bash
# ~/.claude/monitor-statusline.sh
INPUT=$(cat)
mkdir -p ~/.claude-monitor
echo "$INPUT" > ~/.claude-monitor/statusline-${CLAUDE_SESSION_ID:-unknown}.json
echo "$INPUT" | <comando_statusline_atual>   # passthrough preserva visual existente
```

Configuração em `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/monitor-statusline.sh"
  }
}
```

O wrapper roda a cada mensagem do assistente (Claude Code dispara naturalmente). Monitor lê esses arquivos e ganha cota + tokens precisos + custo + modelo.

Produto da Fase 2: dashboard com cota 5h e 7d em tempo real.

### Fase 3 — OTel para histórico (opcional, 2-3 horas)

Para gráficos temporais, agregação semanal e métricas históricas, ligar OTel nativo do Claude Code:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Roda um collector mínimo (`otelcol-contrib`) com export pra arquivo JSON ou Prometheus. Monitor consome dali.

Produto da Fase 3: série temporal de custo/tokens, painéis de tendência.

## Fontes de dados — schemas confirmados

### 1. Sessões ativas

**Arquivo:** `~/.claude/sessions/<pid>.json` (um por processo Claude Code rodando)

**Schema:**
```json
{
  "pid": 78320,
  "sessionId": "50c05132-6f38-4584-8fa9-06edbb71aea7",
  "cwd": "C:\\Users\\neima\\projetos\\skill",
  "startedAt": 1777494421408,
  "procStart": "639128386570253930",
  "version": "2.1.123",
  "peerProtocol": 1,
  "kind": "interactive",
  "entrypoint": "cli",
  "status": "busy",
  "updatedAt": 1777497095915
}
```

Campos críticos: `pid`, `sessionId`, `cwd`, `status` (`busy` | `idle`), `updatedAt`, `version`.

### 2. Transcript JSONL

**Arquivo:** `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`

**Encoding do cwd:** barras invertidas e dois pontos viram hífens.
Exemplo: `C:\Users\neima\projetos\skill` → `C--Users-neima-projetos-skill`

**Append-only.** Cada mensagem do assistente tem campo `usage`:

```json
{
  "type": "assistant",
  "message": {
    "model": "claude-opus-4-7",
    "usage": {
      "input_tokens": 1,
      "cache_creation_input_tokens": 632,
      "cache_read_input_tokens": 72330,
      "output_tokens": 401,
      "service_tier": "standard"
    }
  },
  ...
}
```

**Tamanho de contexto** = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens` da última mensagem do assistente.

Outros tipos de linha: `user`, `tool_result`, `system`, `summary`, `result`, `file-history-snapshot`, `permission-mode`.

### 3. Statusline JSON (stdin via wrapper)

**Schema oficial** (parcial, campos mais úteis):

```json
{
  "session_id": "50c05132-...",
  "session_name": "skill review",
  "transcript_path": "/c/Users/neima/.claude/projects/.../...jsonl",
  "version": "2.1.123",
  "model": {
    "id": "claude-opus-4-7",
    "display_name": "Claude Opus 4.7"
  },
  "rate_limits": {
    "five_hour": {
      "used_percentage": 42.3,
      "resets_at": "2026-04-29T23:00:00Z"
    },
    "seven_day": {
      "used_percentage": 18.7,
      "resets_at": "2026-05-04T00:00:00Z"
    }
  },
  "context_window": {
    "context_window_size": 1000000,
    "used_percentage": 12.4,
    "remaining_percentage": 87.6,
    "current_usage": {
      "input_tokens": 1234,
      "cache_read_input_tokens": 72330,
      "cache_creation_input_tokens": 632
    },
    "total_input_tokens": 73000,
    "total_output_tokens": 4200
  },
  "cost": {
    "total_cost_usd": 0.42,
    "total_duration_ms": 12000,
    "total_api_duration_ms": 8500
  },
  "workspace": {
    "current_dir": "C:\\Users\\neima\\projetos\\skill",
    "added_dirs": []
  }
}
```

Disparo: a cada mensagem do assistente, mudança de permission mode ou toggle de vim, debounced em 300ms.

**Cota só popula após primeira chamada de API** na sessão (Pro/Max subscribers).

### 4. Memória

**Diretório:** `~/.claude/projects/<encoded-cwd>/memory/`

Conteúdo típico: `MEMORY.md` (índice) + arquivos individuais por tópico (`user_role.md`, `feedback_*.md`, etc).

Pode não existir se a sessão nunca escreveu memória.

### 5. OTel events e métricas (Fase 3)

**Métricas:**
- `claude_code.cost.usage` (USD)
- `claude_code.token.usage` (atributo `type`: input/output/cacheRead/cacheCreation)
- `claude_code.session.count` (atributo `start_type`: fresh/resume/continue)

**Eventos/logs:**
- `claude_code.api_request` — model, cost_usd, duration_ms, token counts
- `claude_code.tool_result` — tool_name, success, duration_ms
- `claude_code.user_prompt` — prompt_length (conteúdo só com `OTEL_LOG_USER_PROMPTS=1`)

Default interval: métricas 60s, logs 5s.

## Stack proposta

**Linguagem:** Python 3.11+

**Bibliotecas:**
- `textual` — TUI moderna, fs watch nativo, layout reativo
- alternativa leve: `rich` + polling de 1s (sem fs watch)
- `watchdog` — fs events cross-platform

**Estimativa de tamanho:** 300-400 linhas para Fase 1+2 inteira.

**Alternativa em Go:** `bubbletea` + `fsnotify`. Maior performance, binário único, mais código.

## Layout do TUI

```
┌─ Claude Code Monitor ─────────────────────────────────────────────┐
│ Cota 5h: ████████░░  82%  reset em 23:00                          │
│ Cota 7d: ███░░░░░░░  31%  reset em 04 mai                         │
├─ Sessões ativas (2) ──────────────────────────────────────────────┤
│ #  PID    Projeto         Status  Modelo    Ctx     Custo  Update │
│ ▶  22276  hyperframes     busy    opus-4-7  127k 13% $0.42  há 2s │
│    78320  skill           idle    opus-4-7   72k  7% $0.31  há 8s │
├─ Sessão focada: skill ────────────────────────────────────────────┤
│ Transcript: .../50c05132-...jsonl  (380 KB, 50 turnos)            │
│ Memória:    4 arquivos, 18 KB                                     │
│ CLAUDE.md:  não encontrado em cwd                                 │
│ Última msg do user (há 8s): "documenta o plano…"                  │
│ Última ação: Edit (en/memory-audit/SKILL.md)                      │
└───────────────────────────────────────────────────────────────────┘
[F1] alterna sessão  [F2] abre transcript  [F5] força refresh  [q] sai
```

## Roadmap detalhado

| # | Tarefa | Tempo estimado | Dependências |
|---|---|---|---|
| 1 | Confirmar statusline atual do usuário (custom ou built-in) | 5 min | — |
| 2 | Esqueleto Python: scanner de `~/.claude/sessions/` | 30 min | — |
| 3 | Parser de transcript JSONL (extrai `usage` da última msg do assistant) | 1 h | 2 |
| 4 | Stats de memory dir e CLAUDE.md | 30 min | 2 |
| 5 | TUI básico (rich ou textual) com polling 1s | 1-2 h | 2, 3, 4 |
| 6 | Wrapper de statusline que dumpa JSON | 30 min | 1 |
| 7 | Integrar JSON da statusline no TUI (cota + custo) | 30 min | 5, 6 |
| 8 | Refinamentos visuais, atalhos, foco de sessão | 1 h | 7 |
| 9 | (opcional) OTel collector + integração | 2-3 h | 8 |

**Caminho crítico:** 1 → 2 → 3 → 5 → 6 → 7. Total ~5h pra MVP utilizável.

## Riscos e mitigações

### Schema da statusline pode mudar
Versões futuras do Claude Code podem renomear/adicionar/remover campos.

**Mitigação:** monitor usa `dict.get()` com defaults, falha de campo individual não derruba o painel. Schema versionado pelo `version` do JSON. Logar warning quando encontrar campo desconhecido.

### Multi-sessão na mesma cwd
Bug histórico (já corrigido em versão recente): statusline mostrava modelo de outra sessão.

**Mitigação:** segregar tudo por `session_id`, nunca por cwd.

### Privacidade
Transcript JSONL contém todo o conteúdo da conversa. Memory contém preferências pessoais.

**Mitigação:** monitor é read-only, não persiste cópia, não expõe via rede. Default = nenhum log persistente além do necessário.

### Token de OAuth
`~/.claude/.credentials.json` tem token de OAuth.

**Mitigação:** monitor **não toca** nesse arquivo. Toda a leitura é de arquivos que Claude Code já escreve por design para uso externo (sessões, transcript, statusline JSON).

### Cota não populada no início
Os campos `rate_limits` só aparecem **após a primeira chamada de API** da sessão (Pro/Max). Sessões recém-iniciadas mostram cota indisponível por alguns segundos.

**Mitigação:** UI mostra `aguardando primeira chamada` até `rate_limits` aparecer.

### `/usage` endpoint direto
Tecnicamente possível usar o token de `.credentials.json` para chamar `https://api.anthropic.com/api/oauth/usage`, mas é off-label, fora de TOS oficial.

**Decisão:** **não usar.** Statusline + OTel cobrem o mesmo dado de forma suportada.

## O que NÃO vamos fazer

- Proxy HTTP local interceptando chamadas pra api.anthropic.com — quebra com updates do CLI, é frágil e provavelmente fora de TOS.
- Modificar binário do Claude Code — manutenção impossível.
- Persistir transcripts ou memória em outro local — duplicação desnecessária e problema de privacidade.
- Suporte multi-máquina — escopo é máquina única, terminal local.

## Referências oficiais

- Statusline: https://code.claude.com/docs/en/statusline.md
- Monitoring com OTel: https://code.claude.com/docs/en/monitoring-usage.md
- Como Claude Code funciona (sessões/transcripts): https://code.claude.com/docs/en/how-claude-code-works.md
- Diretório `.claude`: https://code.claude.com/docs/en/claude-directory.md
- Hooks: https://code.claude.com/docs/en/hooks.md

## Checkpoints de validação

Ao final de cada fase, validar:

**Fase 1:**
- [ ] Lista todas as sessões ativas detectadas via `ls ~/.claude/sessions/`
- [ ] Mostra contexto correto comparado ao `/context` da sessão real
- [ ] Atualiza quando uma nova sessão é aberta
- [ ] Atualiza quando uma sessão é fechada (arquivo some)

**Fase 2:**
- [ ] Cota 5h aparece na UI dentro de 5s após primeira chamada de API
- [ ] Cota 7d aparece na UI
- [ ] Custo da sessão atualiza a cada mensagem do assistente
- [ ] Statusline original do usuário continua funcionando (passthrough)

**Fase 3:**
- [ ] Métricas chegam no collector
- [ ] Histórico de custo é persistido
- [ ] Painel de tendência funciona
