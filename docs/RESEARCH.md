# Pesquisa — Como Claude Code expõe estado de sessão

Pesquisa concluída em 2026-04-29 para fundamentar o plano do monitor. Combina inspeção local da máquina (`~/.claude/`) e documentação oficial.

## Resumo: o que é acessível externamente

| Dado | Fonte | Real-time? | Suportado oficialmente? |
|---|---|---|---|
| Sessões ativas (PID, cwd, status) | `~/.claude/sessions/<pid>.json` | sim | sim |
| Tamanho de contexto (tokens) | Transcript JSONL — campo `usage` | a cada msg | sim |
| Cota 5h e 7d | Statusline JSON (`rate_limits`) | a cada msg | sim |
| Custo da sessão (USD) | Statusline JSON (`cost.total_cost_usd`) | a cada msg | sim |
| Modelo em uso | Statusline ou transcript | sim | sim |
| Memória escrita | `~/.claude/projects/<encoded-cwd>/memory/` | fs watch | sim |
| CLAUDE.md tamanho | `<cwd>/CLAUDE.md` (stat) | fs watch | sim |
| Métricas históricas | OTel exporter | configurável | sim |
| Quota detalhada (`/usage` endpoint) | `api.anthropic.com/api/oauth/usage` | request | **não** (off-label) |

## Inspeção local (o que existe na máquina do usuário)

### `~/.claude/` (raiz)

```
.credentials.json          # OAuth tokens — NÃO TOCAR
backups/                   # snapshots automáticos
cache/                     # cache local; tem changelog.md útil
file-history/              # histórico de arquivos editados
history.jsonl              # histórico de comandos
mcp-needs-auth-cache.json  # estado de auth de MCP servers
paste-cache/
plans/                     # planos criados via Plan mode
plugins/                   # plugins instalados
projects/                  # transcripts e memórias por projeto
session-env/               # vars de ambiente das sessões
sessions/                  # metadata de sessões ATIVAS
settings.json              # config global
settings.local.json        # config local
shell-snapshots/
skills/                    # skills instaladas
tasks/                     # estado de TaskList
telemetry/                 # buffer de telemetria
```

### `~/.claude/sessions/<pid>.json`

Um arquivo por processo Claude Code rodando. Atualizado em tempo real (`updatedAt` muda).

Exemplo real coletado:
```json
{
  "pid": 78320,
  "sessionId": "50c05132-6f38-4584-8fa9-06edbb71aea7",
  "cwd": "C:\\Users\\neima\\projetos\\skill",
  "startedAt": 1777494421408,
  "version": "2.1.123",
  "peerProtocol": 1,
  "kind": "interactive",
  "entrypoint": "cli",
  "status": "busy",
  "updatedAt": 1777497095915
}
```

Quando a sessão fecha, o arquivo some. Polling de `ls` detecta nova/fechada.

### `~/.claude/projects/<encoded-cwd>/`

Encoding observado: caracteres `:` e `\` viram `-`.
- `C:\Users\neima\projetos\skill` → `C--Users-neima-projetos-skill`

Conteúdo:
- `<sessionId>.jsonl` — transcript completo append-only
- `<sessionId>/` — diretório com snapshots adicionais (file history)
- `memory/` — opcional, criado quando o assistant escreve memória

### Transcript JSONL — tipos de linha

Cada linha é um JSON object. Tipos observados:
- `permission-mode` — primeira linha, modo de permissão
- `file-history-snapshot` — snapshot de arquivos rastreados
- `user` — mensagens do usuário
- `assistant` — mensagens do modelo (com `usage` para tokens)
- `tool_result` — resultado de tool calls
- `system` — system prompts injetados
- `summary` — checkpoints de compactação
- `result` — status final da sessão
- `attachment` — paste/file attachments

Campo `usage` em assistant messages:
```json
"usage": {
  "input_tokens": 1,
  "cache_creation_input_tokens": 632,
  "cache_read_input_tokens": 72330,
  "output_tokens": 401,
  "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
  "service_tier": "standard",
  "cache_creation": {"ephemeral_1h_input_tokens": ...}
}
```

**Tamanho de contexto** = soma de `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens` da última mensagem.

### `~/.claude/cache/changelog.md`

Arquivo que o Claude Code mantém com histórico de releases. Útil pra rastrear quando features apareceram. Trechos relevantes pra o monitor:

- "Added `rate_limits` field to statusline scripts for displaying Claude.ai rate limit usage (5-hour and 7-day windows with `used_percentage` and `resets_at`)"
- "The Usage tab in Settings now shows your 5-hour and weekly usage immediately and no longer fails when the usage endpoint is rate-limited"
- "Increased default otel interval from 1s -> 5s"
- "Added `added_dirs` to the statusline JSON `workspace` section"
- "Fixed statusline showing another session's model when running multiple Claude Code instances and using `/model` in one of them" (já corrigido)

### Variáveis de ambiente do processo Claude Code

Coletadas via `env` em uma sessão ativa:
```
AI_AGENT=claude-code/2.1.123/agent
CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000
CLAUDECODE=1
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta
CLAUDE_CODE_ENTRYPOINT=cli
CLAUDE_CODE_EXECPATH=...
```

## Statusline — schema completo (oficial)

Cada execução da statusline recebe via stdin um JSON com a seguinte estrutura (campos confirmados em docs e changelog):

```json
{
  "session_id": "uuid",
  "session_name": "string ou null",
  "transcript_path": "string",
  "version": "string",
  "model": {"id": "string", "display_name": "string"},
  "rate_limits": {
    "five_hour": {"used_percentage": float, "resets_at": "ISO8601"},
    "seven_day": {"used_percentage": float, "resets_at": "ISO8601"}
  },
  "context_window": {
    "context_window_size": int,
    "used_percentage": float,
    "remaining_percentage": float,
    "current_usage": {
      "input_tokens": int,
      "cache_read_input_tokens": int,
      "cache_creation_input_tokens": int
    },
    "total_input_tokens": int,
    "total_output_tokens": int
  },
  "cost": {
    "total_cost_usd": float,
    "total_duration_ms": int,
    "total_api_duration_ms": int
  },
  "workspace": {
    "current_dir": "string",
    "added_dirs": ["string"]
  },
  "permission_mode": "string",
  "vim_mode": bool
}
```

**Disparo:** após cada mensagem do assistente, mudança de permission mode, toggle de vim. Debounced em 300ms.

**Configuração:** `statusLine.command` em `~/.claude/settings.json` ou `.claude/settings.json` (escopo de projeto).

**Dado importante:** `rate_limits` só popula **depois** da primeira chamada de API na sessão (Pro/Max subscribers).

## OpenTelemetry — emissão nativa do Claude Code

Habilitado via env vars:
```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317   # gRPC
# ou:
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

### Métricas

| Nome | Tipo | Atributos |
|---|---|---|
| `claude_code.cost.usage` | counter (USD) | model, session_id |
| `claude_code.token.usage` | counter | type (input/output/cacheRead/cacheCreation), model |
| `claude_code.session.count` | counter | start_type (fresh/resume/continue) |

Default interval: 60s.

### Eventos / logs estruturados

| Evento | Atributos principais |
|---|---|
| `claude_code.api_request` | model, cost_usd, duration_ms, input_tokens, output_tokens, cache_read_tokens, prompt.id |
| `claude_code.tool_result` | tool_name, success, duration_ms, prompt.id |
| `claude_code.user_prompt` | prompt_length (conteúdo redacted por padrão) |
| `claude_code.tool_decision` | tool_name, decision |

Default interval: 5s.

### Spans (traces, beta)

```
claude_code.interaction
├── claude_code.llm_request   [input_tokens, output_tokens, cache_read_tokens]
└── claude_code.tool
    ├── claude_code.tool.blocked_on_user
    └── claude_code.tool.execution
```

### Env vars de privacidade

| Variável | Efeito |
|---|---|
| `OTEL_LOG_USER_PROMPTS=1` | inclui texto dos prompts |
| `OTEL_LOG_TOOL_DETAILS=1` | inclui comandos bash, file paths |
| `OTEL_LOG_TOOL_CONTENT=1` | inclui stdin/stdout (truncado em 60 KB) |
| `DISABLE_TELEMETRY` | legado, ainda funciona |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | suprime telemetria de uso |

## Hooks

Hooks fazem fire de comandos shell em eventos. Recebem JSON via stdin com `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`.

**Hooks per-turn relevantes:**
- `UserPromptSubmit` — antes de Claude processar
- `PostToolUse` — após cada tool call
- `Stop` — quando Claude termina a resposta

**Limitação:** payload de hook **não inclui** tokens/custo. Para esses, usar statusline ou OTel.

**Ideia descartada:** usar hook para escrever estado em arquivo. Funciona, mas statusline já dá payload mais completo, sem necessidade de configurar hooks adicionais.

## Endpoint `/usage` — análise

Comando interno `/usage` consulta `https://api.anthropic.com/api/oauth/usage` usando o OAuth token de `~/.claude/.credentials.json`.

**Resposta inclui:** windows de 5h e 7d, com `used_percentage`, `reset_at`, `tier`.

**Por que não usar:**
1. Endpoint não documentado publicamente — pode mudar sem aviso.
2. Reuso do OAuth token de outro processo é off-label e potencialmente fora de TOS.
3. Statusline e OTel já expõem o mesmo dado de forma suportada.

## Conclusão

Tudo o que o monitor precisa está disponível por canais oficiais e estáveis:

- **Sessões ativas, contexto, modelo:** filesystem (`~/.claude/sessions/` + transcripts).
- **Cota, custo, tokens detalhados:** statusline JSON.
- **Histórico/agregação:** OTel.

Sem proxy, sem reverse-engineering, sem violar TOS. Plano detalhado em [`PLAN.md`](PLAN.md).
