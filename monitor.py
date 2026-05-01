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

VERSION = "v1.08.03"  # v1.xx.yy → xx=recurso, yy=bug (ambos sequenciais; só zeram quando muda a major)

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIMITS_CACHE = Path(f"/tmp/cc_limits_{os.getuid()}.json")
CONTEXT_WINDOW = 1_000_000  # Opus/Sonnet 4.x
CONFIG_FILE = Path(__file__).parent / "config.json"
CHAT_LOG = Path(__file__).parent / "chat.log"


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


def load_sessions(include_dead: bool = False) -> list[dict]:
    """Lista sessões. Por padrão só as vivas; include_dead=True traz todas
    do filesystem, marcando _alive=False nas que o PID já não existe."""
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            try:
                os.kill(pid, 0)
                data["_alive"] = True
            except (ProcessLookupError, PermissionError):
                data["_alive"] = False
                if not include_dead:
                    continue
            sessions.append(data)
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s.get("updatedAt", 0), reverse=True)


# ── system monitor (CPU/RAM/disco/GPU) ────────────────────────────────────────

def get_cpu_info() -> dict:
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return {
            "cores": cpu_count,
            "load_1m": round(load1, 2),
            "load_5m": round(load5, 2),
            "load_15m": round(load15, 2),
            "usage_pct": round(min(load1 / cpu_count * 100, 100), 1),
        }
    except Exception:
        return {"cores": os.cpu_count() or 1, "usage_pct": 0, "load_1m": 0, "load_5m": 0, "load_15m": 0}


def get_memory_info() -> dict:
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"):
                    info[parts[0].rstrip(":")] = int(parts[1]) // 1024  # KB → MB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used = total - avail
        return {
            "total_mb": total,
            "available_mb": avail,
            "used_mb": used,
            "usage_pct": round((used / max(total, 1)) * 100, 1),
            "swap_used_mb": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
            "swap_total_mb": info.get("SwapTotal", 0),
        }
    except Exception:
        return {"total_mb": 0, "available_mb": 0, "used_mb": 0, "usage_pct": 0, "swap_used_mb": 0, "swap_total_mb": 0}


def get_disk_info(path: str = "/") -> dict:
    try:
        import shutil
        u = shutil.disk_usage(path)
        return {
            "total_gb": round(u.total / 1024**3, 1),
            "used_gb": round(u.used / 1024**3, 1),
            "free_gb": round(u.free / 1024**3, 1),
            "usage_pct": round(u.used / u.total * 100, 1),
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "usage_pct": 0}


def get_gpu_info() -> dict:
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,temperature.gpu,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {"available": False}
        # 1ª GPU
        parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
        used = int(parts[2]) if parts[2] not in ("[N/A]", "") else 0
        total = int(parts[3]) if parts[3] not in ("[N/A]", "") else 0
        return {
            "available": True,
            "name": parts[0],
            "temp_c": int(parts[1]) if parts[1] not in ("[N/A]", "") else None,
            "mem_used_mb": used,
            "mem_total_mb": total,
            "mem_pct": round(used / max(total, 1) * 100, 1) if total else 0.0,
            "util_pct": int(parts[4]) if parts[4] not in ("[N/A]", "") else 0,
        }
    except Exception:
        return {"available": False}


def get_system_status() -> dict:
    return {
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
        "gpu": get_gpu_info(),
    }


def get_top_processes(n: int = 10, sort_by: str = "cpu") -> list[dict]:
    """Top N processos por %CPU (sort_by='cpu') ou %MEM (sort_by='mem')."""
    sort_flag = "-%cpu" if sort_by == "cpu" else "-%mem"
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,user,pcpu,pmem,etime,comm", "--sort=" + sort_flag, "--no-headers"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.strip().splitlines()[:n]:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            try:
                out.append({
                    "pid": int(parts[0]),
                    "user": parts[1][:10],
                    "cpu_pct": float(parts[2]),
                    "mem_pct": float(parts[3]),
                    "etime": parts[4],
                    "cmd": parts[5][:40],
                })
            except ValueError:
                continue
        return out
    except Exception:
        return []


def find_active_transcript(session: dict) -> Optional[Path]:
    """Acha o transcript em uso AGORA pelo PID da sessão.

    O ~/.claude/sessions/<pid>.json congela o sessionId no momento em que
    o Claude Code arrancou — após /clear, novo JSONL é criado mas o
    session.json não muda. Estratégia: pegar o JSONL mais recente (mtime)
    no diretório do projeto. Se o apontado pelo session.json for o mais
    novo, usa ele; senão, usa o mais novo.
    """
    cwd = session.get("cwd", "")
    session_id = session.get("sessionId", "")
    encoded = encode_cwd(cwd)
    project_dir = None
    for prefix in ["", "-"]:
        p = PROJECTS_DIR / f"{prefix}{encoded}"
        if p.exists():
            project_dir = p
            break
    if not project_dir:
        return None
    hinted = project_dir / f"{session_id}.jsonl"
    # sessão morta: confia só no sessionId congelado (último estado conhecido)
    if not session.get("_alive", True):
        return hinted if hinted.exists() else None
    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    newest = candidates[0]
    if hinted.exists() and hinted.stat().st_mtime >= newest.stat().st_mtime:
        return hinted
    return newest


def get_transcript_usage(session: dict) -> tuple[str, int, float]:
    """Returns (model, total_tokens, ctx_pct)"""
    transcript = find_active_transcript(session)
    if not transcript:
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
    transcript = find_active_transcript(session)
    if not transcript:
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


def get_oauth_expiry() -> Optional[float]:
    """Retorna segundos até expirar (None se desconhecido, negativo se expirado)."""
    creds = CLAUDE_DIR / ".credentials.json"
    try:
        data = json.loads(creds.read_text())
        exp_ms = data.get("claudeAiOauth", {}).get("expiresAt")
        if exp_ms:
            return (exp_ms / 1000) - time.time()
    except Exception:
        pass
    return None


def log_chat(role: str, text: str, *, focus: Optional[str] = None,
             error_status: Optional[int] = None, error_body: Optional[str] = None) -> None:
    """Append a chat event to the log file."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        focus_tag = f" [focus={focus[:8]}]" if focus else " [geral]"
        with CHAT_LOG.open("a") as f:
            if error_status is not None:
                f.write(f"[{ts}]{focus_tag} ERROR {error_status} ({role}): {text[:300]}\n")
                if error_body:
                    f.write(f"  body: {str(error_body)[:1500]}\n")
            else:
                f.write(f"[{ts}]{focus_tag} {role}: {text}\n")
    except Exception:
        pass


def tail_chat_log(n: int = 20) -> list[str]:
    if not CHAT_LOG.exists():
        return ["(log vazio — nenhuma conversa registrada ainda)"]
    try:
        with CHAT_LOG.open() as f:
            lines = f.readlines()
        return [ln.rstrip() for ln in lines[-n:]]
    except Exception as e:
        return [f"(erro lendo log: {e})"]


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

    # count Agent tool uses and skills invoked in transcript ATIVO (pós /clear inclusive)
    transcript = find_active_transcript(session)
    if transcript:
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


class SystemMonitor(Static):
    """Painel de uso da máquina: CPU, RAM, disco, GPU."""

    def render_bar(self, label: str, pct: float, suffix: str) -> str:
        filled = int(max(0, min(pct, 100)) / 2)
        color = color_for(pct, "system_pct")
        bar = "█" * filled + "░" * (50 - filled)
        return f"[bold]{label}[/] [{color}]{bar} {pct:>4.0f}%[/] {suffix}"

    def update_status(self, status: dict) -> None:
        cpu = status["cpu"]
        mem = status["memory"]
        disk = status["disk"]
        gpu = status["gpu"]
        lines = [
            self.render_bar(
                "CPU ", cpu["usage_pct"],
                f"{cpu['cores']}c · load {cpu['load_1m']}/{cpu['load_5m']}/{cpu['load_15m']}",
            ),
            self.render_bar(
                "RAM ", mem["usage_pct"],
                f"{mem['used_mb']:,}/{mem['total_mb']:,} MB"
                + (f" · swap {mem['swap_used_mb']:,}MB" if mem.get("swap_used_mb") else ""),
            ),
            self.render_bar(
                "Disk", disk["usage_pct"],
                f"{disk['used_gb']}/{disk['total_gb']} GB · livre {disk['free_gb']} GB",
            ),
        ]
        if gpu.get("available"):
            temp = f" · {gpu['temp_c']}°C" if gpu.get("temp_c") is not None else ""
            lines.append(
                self.render_bar(
                    "GPU ", float(gpu["util_pct"]),
                    f"{gpu['name'][:20]} · VRAM {gpu['mem_used_mb']:,}/{gpu['mem_total_mb']:,}MB ({gpu['mem_pct']:.0f}%){temp}",
                )
            )
        self.update("\n".join(lines))


class ProcessTable(Static):
    """Top N processos por CPU/RAM. Toggle com 'p'."""

    def compose(self) -> ComposeResult:
        yield DataTable(id="proc-table", show_cursor=False)

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("PID", "User", "CPU%", "RAM%", "Tempo", "Comando")

    def update_processes(self, procs: list[dict]) -> None:
        t = self.query_one(DataTable)
        t.clear()
        for p in procs:
            cpu_color = color_for(p["cpu_pct"], "system_pct")
            mem_color = color_for(p["mem_pct"], "system_pct")
            t.add_row(
                str(p["pid"]),
                p["user"],
                colored(f"{p['cpu_pct']:.1f}", cpu_color),
                colored(f"{p['mem_pct']:.1f}", mem_color),
                p["etime"],
                p["cmd"],
            )


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
            alive = s.get("_alive", True)
            if not alive:
                status_str = "[dim red]morta[/]"
            else:
                status = s.get("status", "?")
                status_str = "[green]idle[/]" if status == "idle" else "[yellow]busy[/]"
            ctx_color = color_for(ctx_pct, "context_pct")
            mem_color = color_for(extras["mem_kb"], "memory_kb")
            cmd_color = color_for(extras["claude_md"], "claude_md_bytes")
            agt_color = color_for(extras["agents"], "agents_per_session")
            mem_str = f"{extras['mem_files']}f/{extras['mem_kb']}k" if extras["mem_files"] else "-"
            project_label = short_project(s.get("cwd", ""))
            if not alive:
                project_label = f"[dim]{project_label}[/]"
            table.add_row(
                project_label,
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
    """Extrai turns user/assistant recentes do transcript ATIVO da sessão."""
    transcript = find_active_transcript(session)
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


def recommend_skills(sessions: list[dict]) -> dict[str, list[str]]:
    """Pra cada skill, retorna lista de sessões em que deveria rodar agora.

    Regras:
    - session-handoff   → ctx amarelo/vermelho (risco de auto-compaction)
    - memory-audit      → memória amarela/vermelha OU CLAUDE.md vermelho
    - session-statusline → recomendação geral (sem sessão específica)
    """
    rec: dict[str, list[str]] = {
        "session-handoff": [],
        "memory-audit": [],
        "session-statusline": [],
    }
    for s in sessions:
        proj = short_project(s.get("cwd", ""))
        _, _, ctx_pct = get_transcript_usage(s)
        extras = get_session_extras(s)
        ctx_color = color_for(ctx_pct, "context_pct")
        mem_color = color_for(extras["mem_kb"], "memory_kb")
        cmd_color = color_for(extras["claude_md"], "claude_md_bytes")

        if ctx_color in ("yellow", "red"):
            tag = "🔴" if ctx_color == "red" else "🟡"
            rec["session-handoff"].append(f"{tag} {proj} (ctx {ctx_pct:.0f}%)")

        if mem_color in ("yellow", "red") or cmd_color == "red":
            worst = "red" if (mem_color == "red" or cmd_color == "red") else "yellow"
            tag = "🔴" if worst == "red" else "🟡"
            details = []
            if mem_color in ("yellow", "red"):
                details.append(f"mem {extras['mem_kb']}KB")
            if cmd_color == "red":
                details.append(f"CLAUDE.md {extras['claude_md']//1024}KB")
            rec["memory-audit"].append(f"{tag} {proj} ({', '.join(details)})")
    return rec


def diagnose_panel(sessions: list[dict], quota: dict) -> list[str]:
    """Roda regras simples sobre painel + cota e devolve achados (yellow/red)."""
    out: list[str] = []

    # ── cota ─────────────────────────────────────────────────────────────────
    for label, key in (("5h", "five_hour"), ("7d", "seven_day"), ("7d Sonnet", "seven_day_sonnet")):
        q = quota.get(key, {}) or {}
        pct = q.get("utilization")
        if pct is None:
            continue
        color = color_for(pct, "quota_pct")
        if color == "red":
            out.append(f"[red]🔴 cota {label} em {pct:.0f}%[/] — reset {utc_to_local(q.get('resets_at',''))}")
        elif color == "yellow":
            out.append(f"[yellow]🟡 cota {label} em {pct:.0f}%[/] — reset {utc_to_local(q.get('resets_at',''))}")

    # ── por sessão ───────────────────────────────────────────────────────────
    for s in sessions:
        proj = short_project(s.get("cwd", ""))
        _, _, ctx_pct = get_transcript_usage(s)
        extras = get_session_extras(s)

        ctx_color = color_for(ctx_pct, "context_pct")
        if ctx_color == "red":
            out.append(f"[red]🔴 {proj}[/] ctx {ctx_pct:.0f}% — risco de auto-compaction. Considere [cyan]session-handoff[/] antes de /clear.")
        elif ctx_color == "yellow":
            out.append(f"[yellow]🟡 {proj}[/] ctx {ctx_pct:.0f}% — atenção, ainda dá pra continuar.")

        mem_color = color_for(extras["mem_kb"], "memory_kb")
        if mem_color == "red":
            out.append(f"[red]🔴 {proj}[/] memória {extras['mem_kb']}KB ({extras['mem_files']} arquivos) — rode [cyan]memory-audit[/].")
        elif mem_color == "yellow":
            out.append(f"[yellow]🟡 {proj}[/] memória {extras['mem_kb']}KB — considere auditar.")

        cmd_color = color_for(extras["claude_md"], "claude_md_bytes")
        if cmd_color == "red":
            out.append(f"[red]🔴 {proj}[/] CLAUDE.md {extras['claude_md']//1024}KB — muito grande, pode pesar contexto.")

        agt_color = color_for(extras["agents"], "agents_per_session")
        if agt_color == "red":
            out.append(f"[red]🔴 {proj}[/] {extras['agents']} agentes lançados — sessão pesada, considere dividir.")

    if not out:
        out.append("[green]✓ tudo verde — nenhum threshold atingido.[/]")
    return out


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
            log.write("[bold magenta]── ajuda — iccmonit " + VERSION + " ──[/]")
            log.write("")
            log.write("[bold]TUI[/]")
            log.write("  [cyan]↑/↓ + Enter[/]  na tabela → foca o chat numa sessão")
            log.write("  [cyan]r[/]            refresh manual do painel")
            log.write("  [cyan]q[/]            sair")
            log.write("")
            log.write("[bold]chat — comandos[/]")
            log.write("  [cyan]/help[/]    esta ajuda")
            log.write("  [cyan]/diag[/]    diagnóstico do painel (cota + sessões em alerta)")
            log.write("  [cyan]/skills[/]  recomenda qual sessão deve rodar qual skill")
            log.write("            [dim](session-statusline · memory-audit · session-handoff)[/]")
            log.write("  [cyan]/clear[/]   volta ao modo geral (limpa foco e histórico)")
            log.write("  [cyan]/log[/]     mostra últimas linhas do log de chat")
            log.write("  [cyan]/where[/]   mostra path do log e do config")
            log.write("  [dim]/fork[/]    [dim]V2 — abrir nova sessão Claude Code continuando a focada (não disponível)[/]")
            log.write("")
            log.write("[bold]modos do chat[/]")
            log.write("  [yellow]geral[/]   responde sobre todas sessões (estado do painel)")
            log.write("  [yellow]focado[/]  responde sobre UMA sessão (transcript carregado, read-only)")
            log.write("")
            log.write("[bold]docs[/]  [dim]https://github.com/inematds/iccmonit[/]")
        elif cmd == "/diag":
            log.write("[bold magenta]── diagnóstico do painel ──[/]")
            for line in diagnose_panel(self._sessions, self._quota):
                log.write(f"  {line}")
        elif cmd in ("/skills", "/skill"):
            rec = recommend_skills(self._sessions)
            log.write("[bold magenta]── skills úteis (skillmanager3x) — análise do painel ──[/]")
            log.write("")

            log.write("[bold cyan]session-statusline[/] → checkpoint operacional rápido")
            log.write("  [dim]Triggers: 'onde estamos', 'checkpoint', 'organiza a sessão'[/]")
            log.write("  [dim green]→ rode em qualquer sessão ativa pra snapshot do que está em andamento[/]")
            log.write("")

            log.write("[bold cyan]memory-audit[/] → auditoria de memória/CLAUDE.md")
            log.write("  [dim]Triggers: 'analise a memória', 'memória inchada', 'o que remover'[/]")
            if rec["memory-audit"]:
                log.write("  [bold yellow]→ recomendado em:[/]")
                for r in rec["memory-audit"]:
                    log.write(f"    {r}")
            else:
                log.write("  [dim green]→ nenhuma sessão precisa agora[/]")
            log.write("")

            log.write("[bold cyan]session-handoff[/] → resumo final antes de /clear")
            log.write("  [dim]Triggers: 'vou dar /clear', 'handoff', 'encerrar sessão'[/]")
            if rec["session-handoff"]:
                log.write("  [bold yellow]→ recomendado em:[/]")
                for r in rec["session-handoff"]:
                    log.write(f"    {r}")
            else:
                log.write("  [dim green]→ nenhuma sessão precisa agora[/]")
            log.write("")
            log.write("[dim]Pra rodar: foque a sessão alvo no monitor, abra o terminal dela e digite o trigger ou /<skill-name>.[/]")
        elif cmd == "/log":
            log.write(f"[bold]── últimas 20 entradas de {CHAT_LOG.name} ──[/]")
            for ln in tail_chat_log(20):
                log.write(f"  [dim]{ln}[/]")
        elif cmd == "/where":
            log.write(f"[bold]chat log:[/]   {CHAT_LOG}")
            log.write(f"[bold]config:[/]     {CONFIG_FILE}")
            log.write(f"[bold]projects:[/]   {PROJECTS_DIR}")
        elif cmd == "/fork":
            log.write("[yellow]/fork[/] ainda não implementado — está no roadmap V2.")
            log.write("[dim]A ideia: abrir 'claude --resume <sessionId>' num subprocess paralelo,[/]")
            log.write("[dim]forkando a conversa selecionada num novo terminal.[/]")
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
        log_chat("user", msg, focus=self._focus_session_id)
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
            log_chat("claude", reply, focus=self._focus_session_id)
        except Exception as e:
            status = getattr(e, "status_code", None)
            body = getattr(e, "body", None)
            log_chat(
                "user", msg, focus=self._focus_session_id,
                error_status=status or 0, error_body=str(body) if body else str(e),
            )
            if status == 401:
                exp = get_oauth_expiry()
                exp_msg = ""
                if exp is not None:
                    exp_msg = f" (token expira em {exp/3600:.1f}h)" if exp > 0 else " (token EXPIRADO)"
                log.write(f"[red]401 — OAuth não aceito{exp_msg}.[/]")
                log.write("[dim]Logue de novo no Claude Code (claude logout/login) ou aguarde refresh automático.[/]")
                log.write(f"[dim]Detalhes em {CHAT_LOG.name} (use /log).[/]")
            else:
                log.write(f"[red]erro {status or ''}:[/] {str(e)[:300]}")
                log.write(f"[dim]Detalhes em {CHAT_LOG.name} (use /log).[/]")
            # tira a user msg que falhou pra não envenenar o histórico
            if self._history and self._history[-1].get("role") == "user":
                self._history.pop()


# ── App ───────────────────────────────────────────────────────────────────────

class MonitorApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    #main-row {
        height: 1fr;
    }
    #left-column {
        width: 2fr;
        height: 1fr;
    }
    #right-column {
        width: 1fr;
        height: 1fr;
    }
    #quota-section, #system-section {
        height: auto;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    #process-section {
        height: 12;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
        display: none;
    }
    #process-section.-visible {
        display: block;
    }
    #quota-label, #system-label, #process-label, #session-label, #chat-label {
        text-style: bold;
        color: $accent;
    }
    #session-section {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    #chat-section {
        border: solid $primary;
        padding: 0 1;
        height: 1fr;
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
        Binding("a", "toggle_all", "Todas/Ativas"),
        Binding("p", "toggle_processes", "Processos"),
        Binding("s", "toggle_sort", "Sort CPU/RAM"),
    ]

    show_all_sessions: reactive[bool] = reactive(False)
    show_processes: reactive[bool] = reactive(False)
    proc_sort: reactive[str] = reactive("cpu")

    TITLE = f"{CONFIG.get('title', 'INEMA Claude Monitor')} {VERSION}"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-row"):
            with Vertical(id="left-column"):
                with Vertical(id="quota-section"):
                    yield Label("● Cota", id="quota-label")
                    yield QuotaBar(id="quota-bar")
                with Vertical(id="system-section"):
                    yield Label("● Máquina", id="system-label")
                    yield SystemMonitor(id="system-monitor")
                with Vertical(id="process-section"):
                    yield Label("● Processos", id="process-label")
                    yield ProcessTable(id="process-table-widget")
                with Vertical(id="session-section"):
                    yield Label("● Sessões ativas", id="session-label")
                    yield SessionTable(id="session-table-widget")
            with Vertical(id="right-column"):
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
        self.query_one(SystemMonitor).update_status(get_system_status())

        sessions = load_sessions(include_dead=self.show_all_sessions)
        self.query_one(SessionTable).update_sessions(sessions)
        self.query_one(ChatPane).update_context(sessions, quota)

        # processos só atualizam se o painel está visível (evita ps a cada refresh)
        if self.show_processes:
            n = CONFIG.get("top_processes_n", 10)
            procs = get_top_processes(n=n, sort_by=self.proc_sort)
            self.query_one(ProcessTable).update_processes(procs)
            sort_label = "CPU" if self.proc_sort == "cpu" else "RAM"
            self.query_one("#process-label", Label).update(f"● Processos — top {n} por {sort_label}")

        # label da seção de sessões
        scope = "todas (vivas + mortas)" if self.show_all_sessions else "ativas"
        self.query_one("#session-label", Label).update(f"● Sessões {scope}")

        now = datetime.now().strftime("%H:%M:%S")
        alive_count = sum(1 for s in sessions if s.get("_alive", True))
        if self.show_all_sessions:
            extra = f"{len(sessions)} ({alive_count} vivas)"
        else:
            extra = f"{len(sessions)} viva(s)"
        self.sub_title = f"atualizado {now} · {extra}"

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_toggle_all(self) -> None:
        self.show_all_sessions = not self.show_all_sessions
        self.refresh_data()

    def action_toggle_processes(self) -> None:
        self.show_processes = not self.show_processes
        section = self.query_one("#process-section")
        if self.show_processes:
            section.add_class("-visible")
        else:
            section.remove_class("-visible")
        self.refresh_data()

    def action_toggle_sort(self) -> None:
        if not self.show_processes:
            return
        self.proc_sort = "mem" if self.proc_sort == "cpu" else "cpu"
        self.refresh_data()

    def on_session_table_session_selected(self, event: SessionTable.SessionSelected) -> None:
        self.query_one(ChatPane).set_focus(event.session_id)


if __name__ == "__main__":
    MonitorApp().run()
