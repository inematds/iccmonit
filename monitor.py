#!/usr/bin/env python3
"""Claude Code Session Monitor — V1"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

VERSION = "v1.01.02"  # v1.xx.yy → xx=recurso, yy=bug (sequencial até mudar a major)

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIMITS_CACHE = Path(f"/tmp/cc_limits_{os.getuid()}.json")
CONTEXT_WINDOW = 1_000_000  # Opus/Sonnet 4.x
CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


CONFIG = load_config()


def color_for(value: float, key: str) -> str:
    t = CONFIG.get("thresholds", {}).get(key, {})
    if not t:
        return "white"
    if value <= t.get("blue", 0):
        return "blue"
    if value <= t.get("green", 60):
        return "green"
    if value <= t.get("yellow", 85):
        return "yellow"
    return "red"


def colored(value: str, color: str) -> str:
    return f"[{color}]{value}[/]"


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[:/\\]", "-", cwd).lstrip("-")


def utc_to_local(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d/%m %H:%M")
    except Exception:
        return iso


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)


def load_sessions() -> list[dict]:
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            # check if process is still alive
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                continue
            sessions.append(data)
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s.get("updatedAt", 0), reverse=True)


def get_transcript_usage(session: dict) -> tuple[str, int, float]:
    """Returns (model, total_tokens, ctx_pct)"""
    cwd = session.get("cwd", "")
    session_id = session.get("sessionId", "")
    encoded = encode_cwd(cwd)
    transcript = PROJECTS_DIR / encoded / f"{session_id}.jsonl"
    if not transcript.exists():
        # try with leading dash
        transcript = PROJECTS_DIR / f"-{encoded}" / f"{session_id}.jsonl"
    if not transcript.exists():
        return "unknown", 0, 0.0
    try:
        result = subprocess.run(
            ["tac", str(transcript)], capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            try:
                obj = json.loads(line)
                if obj.get("type") == "assistant" and "message" in obj:
                    msg = obj["message"]
                    usage = msg.get("usage", {})
                    model = msg.get("model", "unknown")
                    if usage:
                        total = sum(
                            usage.get(k, 0)
                            for k in [
                                "input_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                                "output_tokens",
                            ]
                        )
                        pct = round(total * 100 / CONTEXT_WINDOW, 1)
                        return model, total, pct
            except Exception:
                continue
    except Exception:
        pass
    return "unknown", 0, 0.0


def get_session_cost(session: dict) -> float:
    """Sum cost from all assistant messages in transcript."""
    cwd = session.get("cwd", "")
    session_id = session.get("sessionId", "")
    encoded = encode_cwd(cwd)
    transcript = PROJECTS_DIR / encoded / f"{session_id}.jsonl"
    if not transcript.exists():
        transcript = PROJECTS_DIR / f"-{encoded}" / f"{session_id}.jsonl"
    if not transcript.exists():
        return 0.0
    total_cost = 0.0
    try:
        with open(transcript) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "result":
                        cost = obj.get("costUSD", 0) or obj.get("cost_usd", 0)
                        if cost:
                            total_cost += float(cost)
                except Exception:
                    continue
    except Exception:
        pass
    return total_cost


def load_quota() -> dict:
    if not LIMITS_CACHE.exists():
        return {}
    try:
        age = time.time() - LIMITS_CACHE.stat().st_mtime
        if age > 120:
            return {}
        return json.loads(LIMITS_CACHE.read_text())
    except Exception:
        return {}


def get_oauth_token() -> Optional[str]:
    creds = CLAUDE_DIR / ".credentials.json"
    try:
        data = json.loads(creds.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def get_session_extras(session: dict) -> dict:
    """Returns CLAUDE.md size, memory size/count, agent count, skills used."""
    cwd = session.get("cwd", "")
    session_id = session.get("sessionId", "")
    encoded = encode_cwd(cwd)
    result = {"claude_md": 0, "mem_kb": 0, "mem_files": 0, "agents": 0, "skills": 0}

    # CLAUDE.md
    claude_md = Path(cwd) / "CLAUDE.md"
    if claude_md.exists():
        result["claude_md"] = claude_md.stat().st_size

    # memory dir
    for prefix in ["", "-"]:
        mem_dir = PROJECTS_DIR / f"{prefix}{encoded}" / "memory"
        if mem_dir.exists():
            files = [f for f in mem_dir.iterdir() if f.is_file()]
            result["mem_files"] = len(files)
            result["mem_kb"] = sum(f.stat().st_size for f in files) // 1024
            break

    # count Agent tool uses and skills invoked in transcript
    for prefix in ["", "-"]:
        transcript = PROJECTS_DIR / f"{prefix}{encoded}" / f"{session_id}.jsonl"
        if transcript.exists():
            skills_seen = set()
            try:
                with open(transcript) as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "assistant":
                                for block in obj.get("message", {}).get("content", []):
                                    if not isinstance(block, dict):
                                        continue
                                    if block.get("type") == "tool_use":
                                        if block.get("name") == "Agent":
                                            result["agents"] += 1
                                        elif block.get("name") == "Skill":
                                            inp = block.get("input", {})
                                            skills_seen.add(inp.get("skill", "?"))
                        except Exception:
                            continue
                result["skills"] = len(skills_seen)
            except Exception:
                pass
            break

    return result


def fmt_bytes(size: int) -> str:
    if size == 0:
        return "-"
    if size >= 1024:
        return f"{size//1024}k"
    return f"{size}b"


def short_model(model: str) -> str:
    model = model.lower()
    if "opus" in model:
        return "opus"
    if "sonnet" in model:
        return "sonnet"
    if "haiku" in model:
        return "haiku"
    return model[:8]


def short_project(cwd: str) -> str:
    return Path(cwd).name[:20] if cwd else "?"


def ago(ts_ms: int) -> str:
    secs = int(time.time() - ts_ms / 1000)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs//60}m"
    return f"{secs//3600}h"


# ── Widgets ───────────────────────────────────────────────────────────────────

class QuotaBar(Static):
    def render_quota(self, label: str, pct: float, reset_iso: str) -> str:
        filled = int(pct / 2)
        color = color_for(pct, "quota_pct")
        bar = "█" * filled + "░" * (50 - filled)
        reset = utc_to_local(reset_iso) if reset_iso else "?"
        return f"[bold]{label}[/] [{color}]{bar} {pct:.0f}%[/] → {reset}"

    def update_quota(self, quota: dict) -> None:
        lines = []
        fh = quota.get("five_hour", {})
        if fh:
            lines.append(self.render_quota("5h ", fh.get("utilization", 0), fh.get("resets_at", "")))
        sd = quota.get("seven_day", {})
        if sd:
            lines.append(self.render_quota("7d ", sd.get("utilization", 0), sd.get("resets_at", "")))
        sds = quota.get("seven_day_sonnet", {})
        if sds and sds.get("utilization") is not None:
            lines.append(self.render_quota("7d♪", sds.get("utilization", 0), sds.get("resets_at", "")))
        self.update("\n".join(lines) if lines else "[dim]aguardando API...[/]")


class SessionTable(Static):
    SESSIONS: reactive[list] = reactive([])

    class SessionSelected(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def compose(self) -> ComposeResult:
        yield DataTable(id="session-table", show_cursor=True, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Projeto", "Status", "Modelo", "Ctx%", "CLAUDE.md", "Mem", "Agts", "Skls", "Update")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.post_message(self.SessionSelected(str(event.row_key.value)))

    def update_sessions(self, sessions: list[dict]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for s in sessions:
            model, tokens, ctx_pct = get_transcript_usage(s)
            extras = get_session_extras(s)
            status = s.get("status", "?")
            status_str = "[green]idle[/]" if status == "idle" else "[yellow]busy[/]"
            ctx_color = color_for(ctx_pct, "context_pct")
            mem_color = color_for(extras["mem_kb"], "memory_kb")
            cmd_color = color_for(extras["claude_md"], "claude_md_bytes")
            agt_color = color_for(extras["agents"], "agents_per_session")
            mem_str = f"{extras['mem_files']}f/{extras['mem_kb']}k" if extras["mem_files"] else "-"
            table.add_row(
                short_project(s.get("cwd", "")),
                status_str,
                short_model(model),
                colored(f"{ctx_pct}%", ctx_color),
                colored(fmt_bytes(extras["claude_md"]), cmd_color),
                colored(mem_str, mem_color),
                colored(str(extras["agents"]), agt_color) if extras["agents"] else "-",
                str(extras["skills"]) if extras["skills"] else "-",
                ago(s.get("updatedAt", 0)),
                key=s.get("sessionId"),
            )


def load_session_messages(session: dict, max_chars: int = 25000) -> str:
    """Extrai turns user/assistant recentes do transcript da sessão."""
    cwd = session.get("cwd", "")
    session_id = session.get("sessionId", "")
    encoded = encode_cwd(cwd)
    transcript = None
    for prefix in ["", "-"]:
        p = PROJECTS_DIR / f"{prefix}{encoded}" / f"{session_id}.jsonl"
        if p.exists():
            transcript = p
            break
    if not transcript:
        return "(transcript não encontrado)"

    turns: list[tuple[str, str]] = []
    try:
        with open(transcript) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") not in ("user", "assistant"):
                    continue
                role = obj.get("type")
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type")
                        if bt == "text":
                            parts.append(block.get("text", ""))
                        elif bt == "tool_use":
                            parts.append(f"[tool_use: {block.get('name', '?')}]")
                        elif bt == "tool_result":
                            parts.append("[tool_result]")
                    content = "\n".join(p for p in parts if p)
                if not isinstance(content, str) or not content.strip():
                    continue
                turns.append((role, content.strip()))
    except Exception:
        return "(erro lendo transcript)"

    out: list[str] = []
    total = 0
    for role, text in reversed(turns):
        snippet = f"[{role}] {text[:2500]}"
        if total + len(snippet) > max_chars:
            break
        out.append(snippet)
        total += len(snippet)
    out.reverse()
    return "\n\n".join(out) if out else "(sessão sem mensagens textuais)"


def build_session_prompt(focus: dict, sessions: list[dict], quota: dict) -> str:
    """System prompt quando o chat está focado numa sessão específica."""
    base = build_system_prompt(sessions, quota)
    transcript_excerpt = load_session_messages(focus)
    extra = f"""

## Sessão em foco

Você está focado em discutir UMA sessão específica abaixo. Responda perguntas SOBRE essa sessão usando o transcript como referência. Você é um observador analítico — NÃO finja ser o usuário ou o agente daquela sessão.

- Projeto: {short_project(focus.get('cwd', ''))}
- cwd: {focus.get('cwd', '')}
- sessionId: {focus.get('sessionId', '')}
- Status: {focus.get('status', '?')}

### Transcript recente (últimas mensagens textuais)

{transcript_excerpt}
"""
    return base + extra


def build_system_prompt(sessions: list[dict], quota: dict) -> str:
    lines = [
        "Você é um assistente integrado ao Claude Code Monitor.",
        "Você tem acesso ao estado atual do painel e deve usá-lo para responder perguntas sobre as sessões ativas, cotas e uso.",
        "",
        f"Horário atual: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        "",
        "## Cotas",
    ]
    fh = quota.get("five_hour", {})
    if fh:
        lines.append(f"- 5h: {fh.get('utilization', 0):.0f}% usado, reset em {utc_to_local(fh.get('resets_at', ''))}")
    sd = quota.get("seven_day", {})
    if sd:
        lines.append(f"- 7d (todos modelos): {sd.get('utilization', 0):.0f}% usado, reset em {utc_to_local(sd.get('resets_at', ''))}")
    sds = quota.get("seven_day_sonnet", {})
    if sds and sds.get("utilization") is not None:
        lines.append(f"- 7d Sonnet: {sds.get('utilization', 0):.0f}% usado, reset em {utc_to_local(sds.get('resets_at', ''))}")

    lines += ["", "## Sessões ativas", f"Total: {len(sessions)} sessão(ões)"]
    for s in sessions:
        model, tokens, ctx_pct = get_transcript_usage(s)
        cost = get_session_cost(s)
        extras = get_session_extras(s)
        lines.append(
            f"- Projeto: {short_project(s.get('cwd',''))} | cwd: {s.get('cwd','')} | "
            f"status: {s.get('status','?')} | modelo: {model} | "
            f"ctx: {ctx_pct}% ({fmt_tokens(tokens)} tokens) | custo: ${cost:.3f} | "
            f"CLAUDE.md: {fmt_bytes(extras['claude_md'])} | "
            f"memória: {extras['mem_files']} arquivos / {extras['mem_kb']}kb | "
            f"agentes lançados: {extras['agents']} | skills invocadas: {extras['skills']} | "
            f"última atualização: {ago(s.get('updatedAt',0))} atrás"
        )

    lines += ["", "Responda em português. Seja direto e objetivo."]
    return "\n".join(lines)


class ChatPane(Vertical):
    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
        yield Input(placeholder="mensagem... (Enter envia · /help pra comandos)", id="chat-input")

    def on_mount(self) -> None:
        self._client: Optional[Anthropic] = None
        self._history: list[dict] = []
        self._sessions: list[dict] = []
        self._quota: dict = {}
        self._focus_session_id: Optional[str] = None
        token = get_oauth_token()
        log = self.query_one("#chat-log", RichLog)
        if token:
            self._client = Anthropic(
                base_url="https://api.anthropic.com",
                auth_token=token,
                default_headers={"anthropic-beta": "oauth-2025-04-20"},
            )
            log.write("[dim]Chat pronto — ciente do estado do painel. Modelo: haiku-4-5[/]")
            log.write("[dim]Selecione uma linha da tabela e Enter pra focar numa sessão. /help pra comandos.[/]")
        else:
            log.write("[red]Token OAuth não encontrado. Chat indisponível.[/]")

    def update_context(self, sessions: list[dict], quota: dict) -> None:
        self._sessions = sessions
        self._quota = quota

    def set_focus(self, session_id: Optional[str]) -> None:
        self._focus_session_id = session_id
        self._history = []
        log = self.query_one("#chat-log", RichLog)
        if session_id:
            s = next((x for x in self._sessions if x.get("sessionId") == session_id), None)
            proj = short_project(s.get("cwd", "")) if s else "?"
            log.write(f"[bold magenta]→ focado em:[/] {proj} [dim]({session_id[:8]})[/]")
            log.write("[dim]histórico anterior limpo. /clear pra voltar ao modo geral.[/]")
        else:
            log.write("[bold]→ modo geral[/] [dim](perguntas sobre todas as sessões)[/]")

    def _handle_command(self, cmd: str, log: RichLog) -> None:
        cmd = cmd.strip()
        if cmd == "/clear":
            self.set_focus(None)
        elif cmd == "/help":
            log.write("[bold]comandos:[/]")
            log.write("  [cyan]/clear[/]  → volta ao modo geral (sem foco)")
            log.write("  [cyan]/help[/]   → esta ajuda")
            log.write("[dim]Para focar numa sessão: navegue na tabela com ↑/↓ e Enter.[/]")
        else:
            log.write(f"[red]comando desconhecido:[/] {cmd}  [dim](tente /help)[/]")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        msg = event.value.strip()
        if not msg:
            return
        event.input.value = ""
        log = self.query_one("#chat-log", RichLog)

        if msg.startswith("/"):
            self._handle_command(msg, log)
            return

        if not self._client:
            log.write("[red]chat indisponível (sem token oauth)[/]")
            return

        log.write(f"[bold cyan]você:[/] {msg}")
        self._history.append({"role": "user", "content": msg})
        try:
            focus = None
            if self._focus_session_id:
                focus = next(
                    (x for x in self._sessions if x.get("sessionId") == self._focus_session_id),
                    None,
                )
            if focus:
                system = build_session_prompt(focus, self._sessions, self._quota)
            else:
                system = build_system_prompt(self._sessions, self._quota)
            chat_cfg = CONFIG.get("chat", {})
            response = self._client.messages.create(
                model=chat_cfg.get("model", "claude-haiku-4-5-20251001"),
                max_tokens=chat_cfg.get("max_tokens", 1024),
                system=system,
                messages=self._history,
            )
            reply = response.content[0].text
            self._history.append({"role": "assistant", "content": reply})
            log.write(f"[bold green]claude:[/] {reply}")
        except Exception as e:
            log.write(f"[red]erro:[/] {e}")


# ── App ───────────────────────────────────────────────────────────────────────

class MonitorApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    #quota-section {
        height: auto;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    #quota-label {
        text-style: bold;
        color: $accent;
    }
    #session-section {
        height: 12;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    #session-label {
        text-style: bold;
        color: $accent;
    }
    #chat-section {
        border: solid $primary;
        padding: 0 1;
        height: 1fr;
    }
    #chat-label {
        text-style: bold;
        color: $accent;
    }
    #chat-log {
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Sair"),
        Binding("r", "refresh", "Refresh"),
    ]

    TITLE = f"{CONFIG.get('title', 'INEMA Claude Monitor')} {VERSION}"

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Vertical(id="quota-section"):
                yield Label("● Cota", id="quota-label")
                yield QuotaBar(id="quota-bar")
            with Vertical(id="session-section"):
                yield Label("● Sessões ativas", id="session-label")
                yield SessionTable(id="session-table-widget")
            with Vertical(id="chat-section"):
                yield Label("● Chat", id="chat-label")
                yield ChatPane(id="chat-pane")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(CONFIG.get("refresh_interval_seconds", 10), self.refresh_data)

    def refresh_data(self) -> None:
        quota = load_quota()
        self.query_one(QuotaBar).update_quota(quota)

        sessions = load_sessions()
        self.query_one(SessionTable).update_sessions(sessions)
        self.query_one(ChatPane).update_context(sessions, quota)

        now = datetime.now().strftime("%H:%M:%S")
        self.sub_title = f"atualizado {now} · {len(sessions)} sessão(ões)"

    def action_refresh(self) -> None:
        self.refresh_data()

    def on_session_table_session_selected(self, event: SessionTable.SessionSelected) -> None:
        self.query_one(ChatPane).set_focus(event.session_id)


if __name__ == "__main__":
    MonitorApp().run()
