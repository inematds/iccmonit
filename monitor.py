#!/usr/bin/env python3
"""Claude Code Session Monitor — V1"""

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

VERSION = "v1.17.12"  # v1.xx.yy → xx=recurso, yy=bug (ambos sequenciais; só zeram quando muda a major)

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


def get_docker_info() -> dict:
    """Lista todos os containers via 'docker ps -a'."""
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}|{{.Image}}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {"available": False, "containers": [], "error": r.stderr.strip()[:100]}
        containers = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            name, status, ports, image = parts
            containers.append({
                "name": name,
                "status": status[:30],
                "ports": ports[:40],
                "image": image[:40],
                "running": status.startswith("Up"),
            })
        return {"available": True, "containers": containers}
    except FileNotFoundError:
        return {"available": False, "containers": [], "error": "docker não instalado"}
    except Exception as e:
        return {"available": False, "containers": [], "error": str(e)[:80]}


def get_boot_services() -> dict:
    """systemd services habilitados pra subir no boot, com estado atual."""
    try:
        r1 = subprocess.run(
            ["systemctl", "list-unit-files", "--state=enabled", "--type=service",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=5,
        )
        if r1.returncode != 0:
            return {"available": False, "services": []}
        enabled = []
        for line in r1.stdout.strip().splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".service"):
                enabled.append(parts[0])
        # 1 chamada pra pegar o estado de todos os services
        r2 = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all",
             "--no-pager", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=5,
        )
        sub_state: dict[str, str] = {}
        for line in r2.stdout.strip().splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 4:
                # unit, load, active, sub, [description]
                sub_state[parts[0]] = parts[3]
        services = []
        for u in enabled:
            state = sub_state.get(u, "unknown")
            services.append({
                "name": u.removesuffix(".service"),
                "state": state,
                "active": state == "running",
            })
        return {"available": True, "services": services}
    except FileNotFoundError:
        return {"available": False, "services": [], "error": "systemctl não disponível"}
    except Exception as e:
        return {"available": False, "services": [], "error": str(e)[:80]}


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


def load_quota() -> tuple[dict, Optional[float]]:
    """Retorna (quota_dict, cache_age_segundos).

    Sem mais cutoff de 2min — devolve o que tem mesmo se velho.
    Quem renderiza decide mostrar como 'stale' visualmente.
    """
    if not LIMITS_CACHE.exists():
        return {}, None
    try:
        age = time.time() - LIMITS_CACHE.stat().st_mtime
        return json.loads(LIMITS_CACHE.read_text()), age
    except Exception:
        return {}, None


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

    def update_quota(self, quota: dict, cache_age: Optional[float] = None) -> None:
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
        if not lines:
            self.update("[dim]aguardando API...[/]")
            return
        # marca staleness se o cache do statusline atrasou
        if cache_age is not None:
            if cache_age > 300:
                lines.append(f"[red]cache velho: {int(cache_age/60)}min — statusline parou?[/]")
            elif cache_age > 120:
                lines.append(f"[yellow]cache: {int(cache_age)}s atrás[/]")
        self.update("\n".join(lines))


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


class DockerPanel(Static):
    """Compacto: containers Docker rodando. Modal '5' mostra tudo."""

    def update_docker(self, docker: dict) -> None:
        if not docker.get("available"):
            self.update(f"[dim]Docker: {docker.get('error', 'indisponível')}[/]")
            return
        containers = docker["containers"]
        running = [c for c in containers if c["running"]]
        lines = [f"[bold]{len(running)}/{len(containers)} containers ativos[/]  [dim](use 5 ou /docker pra mais)[/]"]
        for c in running[:8]:
            ports = (c["ports"] or "-")[:35]
            lines.append(f"  [green]✓[/] {c['name'][:24]:<24} {c['status'][:18]:<18} {ports}")
        if len(running) > 8:
            lines.append(f"  [dim]... +{len(running)-8} ativos[/]")
        self.update("\n".join(lines))


class BootPanel(Static):
    """Compacto: services systemd habilitados no boot. Modal '6' mostra tudo."""

    def update_boot(self, boot: dict) -> None:
        if not boot.get("available"):
            self.update(f"[dim]systemd: {boot.get('error', 'indisponível')}[/]")
            return
        services = boot["services"]
        active = sum(1 for s in services if s["active"])
        active_services = [s for s in services if s["active"]]
        lines = [f"[bold]{active}/{len(services)} habilitados rodando[/]  [dim](use 6 pra lista completa)[/]"]
        for s in active_services[:8]:
            lines.append(f"  [green]✓[/] {s['name'][:38]}")
        if len(active_services) > 8:
            lines.append(f"  [dim]... +{len(active_services)-8} ativos[/]")
        self.update("\n".join(lines))


class SectionLabel(Label):
    """Label do título de uma seção. Click abre o modal fullscreen daquela seção."""

    class Clicked(Message):
        def __init__(self, section: str) -> None:
            super().__init__()
            self.section = section

    def __init__(self, text: str, section: str, **kwargs):
        super().__init__(text, **kwargs)
        self.section = section
        # mostra que é clicável
        self.tooltip = "click ou tecla numérica → fullscreen"

    def on_click(self, event) -> None:
        self.post_message(self.Clicked(self.section))


class CloseButton(Static):
    """[ X ] clicável que fecha o overlay (ou modal) pai."""

    def __init__(self, **kwargs):
        super().__init__("[ X ]", **kwargs)
        self.tooltip = "fechar (Esc)"

    def on_click(self, event) -> None:
        # 1) tenta um PanelOverlay ancestor (modo overlay in-place)
        node = self.parent
        while node is not None:
            if isinstance(node, PanelOverlay):
                node.hide()
                return
            node = getattr(node, "parent", None)
        # 2) fallback: ModalScreen tradicional
        screen = self.screen
        if isinstance(screen, ModalScreen):
            screen.dismiss()


class PanelOverlay(Vertical):
    """Overlay que ocupa SÓ a coluna esquerda — chat fica visível à direita."""

    DEFAULT_CSS = """
    PanelOverlay {
        display: none;
        height: 1fr;
        border: heavy $accent;
        padding: 1;
        background: $surface;
        margin-bottom: 1;
    }
    PanelOverlay.-active {
        display: block;
    }
    PanelOverlay #overlay-header {
        height: 1;
        margin-bottom: 1;
    }
    PanelOverlay #overlay-title {
        text-style: bold;
        color: $accent;
        width: 1fr;
        height: 1;
    }
    PanelOverlay #overlay-close {
        width: auto;
        min-width: 7;
        height: 1;
        background: $error;
        color: white;
        text-style: bold;
        padding: 0 1;
        content-align: center middle;
    }
    PanelOverlay #overlay-close:hover {
        background: $error 60%;
    }
    PanelOverlay #overlay-body {
        height: 1fr;
    }
    PanelOverlay DataTable {
        height: 1fr;
    }
    PanelOverlay QuotaBar, PanelOverlay SystemMonitor {
        height: auto;
    }
    PanelOverlay ScrollableContainer {
        height: 1fr;
    }
    """

    TITLES = {
        "quota": "Cota",
        "system": "Máquina",
        "processes": "Processos",
        "sessions": "Sessões",
        "docker": "Docker — todos os containers",
        "boot": "Boot — systemd enabled",
    }

    def compose(self) -> ComposeResult:
        with Horizontal(id="overlay-header"):
            yield Label("", id="overlay-title")
            yield CloseButton(id="overlay-close")
        yield Vertical(id="overlay-body")

    def show_kind(self, kind: str, app) -> None:
        """Popula o overlay com o widget do kind solicitado e exibe."""
        self.kind = kind
        self.add_class("-active")
        self.query_one("#overlay-title", Label).update(
            f"● {self.TITLES.get(kind, '?')} — expandido  [dim](Esc/q fecha · chat à direita continua disponível)[/]"
        )
        body = self.query_one("#overlay-body", Vertical)
        # limpa anteriores
        for c in list(body.children):
            c.remove()
        # monta o widget apropriado
        if kind == "quota":
            w = QuotaBar(id="overlay-quota")
            body.mount(w)
            w.update_quota(getattr(app, "_last_quota", {}))
        elif kind == "system":
            w = SystemMonitor(id="overlay-system")
            body.mount(w)
            w.update("[dim]coletando dados da máquina...[/]")
            threading.Thread(target=lambda: self._load_system(w), daemon=True).start()
        elif kind == "processes":
            w = ProcessTable(id="overlay-procs")
            body.mount(w)
            threading.Thread(target=lambda: self._load_processes(w, app), daemon=True).start()
        elif kind == "sessions":
            w = SessionTable(id="overlay-sess")
            body.mount(w)
            enriched = getattr(app, "_last_enriched", None)
            if enriched is not None:
                w.update_sessions_enriched(enriched)
            else:
                w.update_sessions(getattr(app, "_last_sessions", []))
        elif kind in ("docker", "boot"):
            sc = ScrollableContainer()
            body.mount(sc)
            content = Static("[dim]coletando...[/]", id="overlay-services-content")
            sc.mount(content)
            target = self._load_docker if kind == "docker" else self._load_boot
            threading.Thread(target=lambda: target(content), daemon=True).start()

    def hide(self) -> None:
        self.remove_class("-active")
        # remove a classe da coluna pra reexibir os painéis
        try:
            self.app.query_one("#left-column").remove_class("-overlay-on")
        except Exception:
            pass
        body = self.query_one("#overlay-body", Vertical)
        for c in list(body.children):
            c.remove()

    # — workers ─────────────────────────────────────────────────────────────
    def _load_system(self, widget) -> None:
        try:
            status = get_system_status()
            self.app.call_from_thread(widget.update_status, status)
        except Exception as e:
            self.app.call_from_thread(widget.update, f"[red]erro: {e}[/]")

    def _load_processes(self, widget, app) -> None:
        try:
            n = CONFIG.get("top_processes_n", 10) * 3
            sort_by = getattr(app, "proc_sort", "cpu")
            procs = get_top_processes(n=n, sort_by=sort_by)
            self.app.call_from_thread(widget.update_processes, procs)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#overlay-title", Label).update,
                f"[red]erro processes: {e}[/]",
            )

    def _load_docker(self, content_widget) -> None:
        try:
            t0 = time.time()
            docker = get_docker_info()
            elapsed = time.time() - t0
            lines: list[str] = []
            if docker.get("available"):
                containers = docker["containers"]
                running = sum(1 for c in containers if c["running"])
                lines.append(
                    f"[bold magenta]── Docker — {running}/{len(containers)} ativos ──[/]  [dim](carregou em {elapsed:.2f}s)[/]"
                )
                lines.append("[dim]chat à direita: /docker <nome> start|stop|restart|logs[/]")
                lines.append("")
                for c in containers:
                    icon = "[green]✓[/]" if c["running"] else "[red]✗[/]"
                    ports = (c["ports"] or "-")[:36]
                    lines.append(
                        f"  {icon} [bold]{c['name'][:26]:<26}[/] {c['status'][:24]:<24} {ports:<36} [dim]{c['image']}[/]"
                    )
            else:
                lines.append(f"[red]Docker erro:[/] {docker.get('error', 'indisponível')}")
            self.app.call_from_thread(content_widget.update, "\n".join(lines))
        except Exception as e:
            self.app.call_from_thread(content_widget.update, f"[red]exceção docker: {e}[/]")

    def _load_boot(self, content_widget) -> None:
        try:
            t0 = time.time()
            boot = get_boot_services()
            elapsed = time.time() - t0
            lines: list[str] = []
            if boot.get("available"):
                services = boot["services"]
                active = sum(1 for s in services if s["active"])
                lines.append(
                    f"[bold magenta]── Boot — {active}/{len(services)} rodando ──[/]  [dim](carregou em {elapsed:.2f}s)[/]"
                )
                lines.append("")
                for s in sorted(services, key=lambda s: (not s["active"], s["name"])):
                    icon = "[green]✓[/]" if s["active"] else "[red]✗[/]"
                    state_color = "green" if s["active"] else "dim red"
                    lines.append(f"  {icon} [bold]{s['name'][:38]:<38}[/]  [{state_color}]{s['state']}[/]")
            else:
                lines.append(f"[red]systemd erro:[/] {boot.get('error', 'indisponível')}")
            self.app.call_from_thread(content_widget.update, "\n".join(lines))
        except Exception as e:
            self.app.call_from_thread(content_widget.update, f"[red]exceção boot: {e}[/]")



class SortIndicator(Static):
    """Toggle visual CPU/RAM dentro do painel Processos. Click alterna."""

    class Toggled(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tooltip = "click ou s pra trocar"

    def update_sort(self, sort_by: str) -> None:
        if sort_by == "cpu":
            self.update("[reverse] CPU [/]  RAM   [dim](click ou s)[/]")
        else:
            self.update(" CPU  [reverse] RAM [/]  [dim](click ou s)[/]")

    def on_click(self, event) -> None:
        self.post_message(self.Toggled())


class ModeIndicator(Static):
    """Toggle visual ativas/todas dentro do painel Sessões. Click alterna."""

    class Toggled(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tooltip = "click ou a pra trocar"

    def update_mode(self, show_all: bool) -> None:
        if show_all:
            self.update(" ativas  [reverse] todas [/]  [dim](click ou a)[/]")
        else:
            self.update("[reverse] ativas [/]  todas   [dim](click ou a)[/]")

    def on_click(self, event) -> None:
        self.post_message(self.Toggled())


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
        """Aceita lista crua — útil quando chamado fora do worker (modal)."""
        enriched = []
        for s in sessions:
            model, tokens, ctx_pct = get_transcript_usage(s)
            extras = get_session_extras(s)
            enriched.append({
                "session": s, "model": model, "tokens": tokens,
                "ctx_pct": ctx_pct, "extras": extras,
            })
        self.update_sessions_enriched(enriched)

    def update_sessions_enriched(self, enriched: list[dict]) -> None:
        """Aceita dados já pré-computados (transcripts já lidos numa thread)."""
        table = self.query_one(DataTable)
        table.clear()
        for item in enriched:
            s = item["session"]
            model = item["model"]
            ctx_pct = item["ctx_pct"]
            extras = item["extras"]
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
            log.write("  [cyan]a[/]            sessões ativas ↔ todas (vivas + mortas)")
            log.write("  [cyan]p[/]            liga/desliga painel Processos")
            log.write("  [cyan]d[/]            liga/desliga painel Docker")
            log.write("  [cyan]b[/]            liga/desliga painel Boot (systemd)")
            log.write("  [cyan]s[/]            sort processos: CPU% ↔ RAM%")
            log.write("  [cyan], . =[/]        redimensionar split")
            log.write("  [cyan]1-6[/]          fullscreen: Cota / Máquina / Procs / Sessões / Docker / Boot")
            log.write("  [cyan]click no título[/]   também abre fullscreen")
            log.write("  [cyan]q[/]            sair")
            log.write("")
            log.write("[bold]chat — comandos[/]")
            log.write("  [cyan]/help[/]    esta ajuda")
            log.write("  [cyan]/diag[/]    diagnóstico do painel (cota + sessões em alerta)")
            log.write("  [cyan]/skills[/]  recomenda qual sessão deve rodar qual skill")
            log.write("            [dim](session-statusline · memory-audit · session-handoff)[/]")
            log.write("  [cyan]/clear[/]   volta ao modo geral (limpa foco e histórico)")
            log.write("  [cyan]/docker[/]  lista containers · /docker <nome> start|stop|restart|logs")
            log.write("  [cyan]/codex[/]   probe rate limit OpenAI (precisa OPENAI_API_KEY ou ~/.codex/auth.json)")
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
        elif cmd.startswith("/docker"):
            self._handle_docker(cmd, log)
        elif cmd == "/codex":
            log.write("[dim]consultando OpenAI rate limits...[/]")
            self._codex_quota_async()
        else:
            log.write(f"[red]comando desconhecido:[/] {cmd}  [dim](tente /help)[/]")

    def _handle_docker(self, cmd: str, log: RichLog) -> None:
        parts = cmd.split()
        # /docker = lista compacta dos rodando
        if len(parts) == 1:
            log.write("[bold]listando containers...[/]")
            self._docker_list_async()
            return
        # /docker <nome> <action>
        if len(parts) >= 3:
            name = parts[1]
            action = parts[2]
            if action in ("start", "stop", "restart", "logs"):
                log.write(f"[dim]docker {action} {name}...[/]")
                self._docker_action_async(name, action)
                return
        log.write("[yellow]uso:[/]")
        log.write("  [cyan]/docker[/]                       lista containers rodando")
        log.write("  [cyan]/docker <nome> start[/]          inicia container")
        log.write("  [cyan]/docker <nome> stop[/]           para container")
        log.write("  [cyan]/docker <nome> restart[/]        reinicia container")
        log.write("  [cyan]/docker <nome> logs[/]           últimas 30 linhas")

    @work(thread=True, exclusive=False)
    def _docker_list_async(self) -> None:
        try:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
                capture_output=True, text=True, timeout=5,
            )
            ok = r.returncode == 0
            out = r.stdout if ok else r.stderr
            self.app.call_from_thread(self._on_docker_list, ok, out)
        except FileNotFoundError:
            self.app.call_from_thread(self._on_docker_list, False, "docker não instalado")
        except Exception as e:
            self.app.call_from_thread(self._on_docker_list, False, str(e))

    def _on_docker_list(self, ok: bool, out: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        if not ok:
            log.write(f"[red]erro:[/] {out[:200]}")
            return
        lines = out.strip().splitlines()
        if not lines:
            log.write("[dim]nenhum container rodando[/]")
            return
        for ln in lines:
            parts = ln.split("|", 2)
            if len(parts) < 3:
                continue
            name, status, ports = parts
            log.write(f"  [green]✓[/] [bold]{name[:28]:<28}[/] {status[:22]:<22} [dim]{ports[:40]}[/]")

    @work(thread=True, exclusive=False)
    def _docker_action_async(self, name: str, action: str) -> None:
        try:
            if action == "logs":
                r = subprocess.run(
                    ["docker", "logs", "--tail", "30", name],
                    capture_output=True, text=True, timeout=10,
                )
            else:
                r = subprocess.run(
                    ["docker", action, name],
                    capture_output=True, text=True, timeout=20,
                )
            ok = r.returncode == 0
            out = (r.stdout + r.stderr).strip()
            self.app.call_from_thread(self._on_docker_action, name, action, ok, out)
        except FileNotFoundError:
            self.app.call_from_thread(self._on_docker_action, name, action, False, "docker não instalado")
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self._on_docker_action, name, action, False, "timeout")
        except Exception as e:
            self.app.call_from_thread(self._on_docker_action, name, action, False, str(e))

    @work(thread=True, exclusive=False)
    def _codex_quota_async(self) -> None:
        """Probe codex/OpenAI. Detecta se está logado em modo ChatGPT ou API key.

        - auth_mode='chatgpt' → relata login via Plus/Pro (cota não exposta via API)
        - api key disponível  → GET em /v1/models e lê headers de rate limit
        """
        info: dict = {"mode": None, "source": None, "key": None,
                       "auth_mode": None, "account_id": None,
                       "last_refresh": None, "headers": None, "error": None}
        try:
            # 1) tenta env
            key = os.environ.get("OPENAI_API_KEY")
            if key:
                info["mode"] = "api_key"
                info["source"] = "$OPENAI_API_KEY"
                info["key"] = key
            else:
                # 2) tenta ~/.codex/auth.json
                codex_auth = Path.home() / ".codex" / "auth.json"
                if codex_auth.exists():
                    info["source"] = str(codex_auth)
                    try:
                        d = json.loads(codex_auth.read_text())
                        info["auth_mode"] = d.get("auth_mode")
                        info["last_refresh"] = d.get("last_refresh")
                        toks = d.get("tokens") or {}
                        info["account_id"] = toks.get("account_id")
                        # OPENAI_API_KEY pode estar null em modo chatgpt
                        api_key = d.get("OPENAI_API_KEY") or d.get("api_key")
                        if api_key:
                            info["mode"] = "api_key"
                            info["key"] = api_key
                        elif info["auth_mode"] == "chatgpt":
                            info["mode"] = "chatgpt"
                    except Exception as e:
                        info["error"] = f"erro lendo auth.json: {e}"
            # 3) se modo api_key: tenta probe
            if info["mode"] == "api_key":
                import urllib.request
                req = urllib.request.Request(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {info['key']}"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        info["headers"] = dict(resp.headers)
                        _ = resp.read(2048)
                except Exception as e:
                    info["error"] = f"GET /v1/models: {e}"
            self.app.call_from_thread(self._on_codex_result, info)
        except Exception as e:
            info["error"] = str(e)[:200]
            self.app.call_from_thread(self._on_codex_result, info)

    def _on_codex_result(self, info: dict) -> None:
        log = self.query_one("#chat-log", RichLog)
        mode = info.get("mode")
        if mode is None:
            log.write("[red]codex não autenticado.[/]")
            log.write("[dim]Esperado: $OPENAI_API_KEY ou ~/.codex/auth.json com auth_mode válido.[/]")
            if info.get("error"):
                log.write(f"[dim]{info['error']}[/]")
            return
        if mode == "chatgpt":
            log.write(f"[green]✓ logado no codex via ChatGPT[/]  [dim]({info['source']})[/]")
            if info.get("account_id"):
                log.write(f"  [cyan]account:[/]      {info['account_id']}")
            if info.get("last_refresh"):
                log.write(f"  [cyan]token refresh:[/] {info['last_refresh']}")
            log.write("[yellow]Cota indisponível por API:[/] o modo ChatGPT (Plus/Pro)")
            log.write("[dim]conta por mensagens, não por tokens. O 'restante' só aparece quando[/]")
            log.write("[dim]você bate o limite. Pra ver uso/limite, abra chatgpt.com/settings.[/]")
            return
        # mode == api_key
        log.write(f"[green]✓ codex com API key[/]  [dim]({info['source']})[/]")
        if info.get("error"):
            log.write(f"[red]erro probe:[/] {info['error']}")
            return
        headers = info.get("headers") or {}
        log.write("[bold]OpenAI rate limits (janela atual):[/]")
        keys = [
            ("x-ratelimit-limit-requests", "req limit"),
            ("x-ratelimit-remaining-requests", "req restantes"),
            ("x-ratelimit-reset-requests", "req reset"),
            ("x-ratelimit-limit-tokens", "tok limit"),
            ("x-ratelimit-remaining-tokens", "tok restantes"),
            ("x-ratelimit-reset-tokens", "tok reset"),
        ]
        any_found = False
        for k, label in keys:
            v = headers.get(k) or headers.get(k.title())
            if v is not None:
                log.write(f"  [cyan]{label:<14}[/] {v}")
                any_found = True
        if not any_found:
            log.write("[dim]nenhum header de rate limit retornado[/]")

    def _on_docker_action(self, name: str, action: str, ok: bool, out: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        log.write(f"{icon} [bold]docker {action} {name}[/]")
        if out:
            # logs pode ser longo — limita a 60 linhas no chat
            for ln in out.splitlines()[:60]:
                log.write(f"  [dim]{ln[:200]}[/]")
            extra = len(out.splitlines()) - 60
            if extra > 0:
                log.write(f"  [dim italic]... +{extra} linha(s) (rode no terminal pra ver tudo)[/]")

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

        if getattr(self, "_pending", False):
            log.write("[yellow]aguarde a resposta anterior...[/]")
            return

        # Render imediato — não trava enquanto a API responde
        log.write(f"[bold cyan]você:[/] {msg}")
        log.write("[dim italic]aguardando claude...[/]")
        log_chat("user", msg, focus=self._focus_session_id)
        self._history.append({"role": "user", "content": msg})
        self._pending = True
        # snapshot dos dados — worker não toca em self._sessions etc.
        focus_id = self._focus_session_id
        sessions_snap = list(self._sessions)
        quota_snap = dict(self._quota)
        history_snap = list(self._history)
        self._send_to_api(msg, focus_id, sessions_snap, quota_snap, history_snap)

    @work(thread=True, exclusive=False)
    def _send_to_api(self, msg: str, focus_id: Optional[str],
                    sessions: list, quota: dict, history: list) -> None:
        """Roda numa thread — não bloqueia a TUI."""
        try:
            focus = None
            if focus_id:
                focus = next((s for s in sessions if s.get("sessionId") == focus_id), None)
            if focus:
                system = build_session_prompt(focus, sessions, quota)
            else:
                system = build_system_prompt(sessions, quota)
            chat_cfg = CONFIG.get("chat", {})
            response = self._client.messages.create(
                model=chat_cfg.get("model", "claude-haiku-4-5-20251001"),
                max_tokens=chat_cfg.get("max_tokens", 1024),
                system=system,
                messages=history,
            )
            reply = response.content[0].text
            self.app.call_from_thread(self._handle_chat_reply, reply)
        except Exception as e:
            status = getattr(e, "status_code", None)
            body = getattr(e, "body", None)
            self.app.call_from_thread(self._handle_chat_error, msg, e, status, body)

    def _handle_chat_reply(self, reply: str) -> None:
        self._pending = False
        self._history.append({"role": "assistant", "content": reply})
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold green]claude:[/] {reply}")
        log_chat("claude", reply, focus=self._focus_session_id)

    def _handle_chat_error(self, msg: str, e: Exception, status, body) -> None:
        self._pending = False
        log = self.query_one("#chat-log", RichLog)
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
        # remove user msg que falhou pra não envenenar próximas requests
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
        width: 50%;
        height: 1fr;
    }
    #right-column {
        width: 50%;
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
    #docker-section, #boot-section {
        height: auto;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
        display: none;
    }
    #docker-section.-visible, #boot-section.-visible {
        display: block;
    }
    #quota-label, #system-label, #process-label, #docker-label, #boot-label, #session-label, #chat-label {
        text-style: bold;
        color: $accent;
    }
    .section-header {
        height: 1;
        width: 100%;
    }
    .section-header SectionLabel {
        width: 1fr;
    }
    .section-header SortIndicator,
    .section-header ModeIndicator {
        width: auto;
        height: 1;
    }
    .section-header SortIndicator:hover,
    .section-header ModeIndicator:hover {
        color: $accent;
    }
    /* quando overlay ativo na esquerda, esconde os painéis normais */
    #left-column.-overlay-on .data-section {
        display: none;
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
        # ── básicos ──
        Binding("q", "quit", "Sair"),
        Binding("r", "refresh", "↻"),
        # ── toggle de blocos opcionais ──
        Binding("p", "toggle_processes", "Procs"),
        Binding("d", "toggle_docker", "Docker"),
        Binding("b", "toggle_boot", "Boot"),
        # ── fullscreen modal ──
        Binding("1", "open_modal('quota')", "Cota"),
        Binding("2", "open_modal('system')", "Máq"),
        Binding("3", "open_modal('processes')", "Proc"),
        Binding("4", "open_modal('sessions')", "Sess"),
        Binding("5", "open_modal('docker')", "Dock"),
        Binding("6", "open_modal('boot')", "Boot"),
        # ── layout ──
        Binding("comma", "shrink_left", "←"),
        Binding("full_stop", "grow_left", "→"),
        Binding("equals_sign", "reset_split", "60/40"),
        # ── internos (já têm widget visual no painel) ──
        Binding("a", "toggle_all", "Todas/Ativas", show=False),
        Binding("s", "toggle_sort", "Sort CPU/RAM", show=False),
        Binding("escape", "close_overlay", "Fechar overlay", show=False),
    ]

    show_all_sessions: reactive[bool] = reactive(False)
    show_processes: reactive[bool] = reactive(False)
    show_docker: reactive[bool] = reactive(False)
    show_boot: reactive[bool] = reactive(False)
    proc_sort: reactive[str] = reactive("cpu")
    left_ratio: reactive[int] = reactive(60)

    TITLE = f"{CONFIG.get('title', 'INEMA Claude Monitor')} {VERSION}"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-row"):
            with Vertical(id="left-column"):
                with Vertical(id="quota-section", classes="data-section"):
                    yield SectionLabel("● Cota", "quota", id="quota-label")
                    yield QuotaBar(id="quota-bar")
                with Vertical(id="system-section", classes="data-section"):
                    yield SectionLabel("● Máquina", "system", id="system-label")
                    yield SystemMonitor(id="system-monitor")
                with Vertical(id="process-section", classes="data-section"):
                    with Horizontal(classes="section-header"):
                        yield SectionLabel("● Processos", "processes", id="process-label")
                        yield SortIndicator(id="sort-indicator")
                    yield ProcessTable(id="process-table-widget")
                with Vertical(id="docker-section", classes="data-section"):
                    yield SectionLabel("● Docker", "docker", id="docker-label")
                    yield DockerPanel(id="docker-panel")
                with Vertical(id="boot-section", classes="data-section"):
                    yield SectionLabel("● Boot (systemd)", "boot", id="boot-label")
                    yield BootPanel(id="boot-panel")
                with Vertical(id="session-section", classes="data-section"):
                    with Horizontal(classes="section-header"):
                        yield SectionLabel("● Sessões", "sessions", id="session-label")
                        yield ModeIndicator(id="mode-indicator")
                    yield SessionTable(id="session-table-widget")
                yield PanelOverlay(id="panel-overlay")
            with Vertical(id="right-column"):
                with Vertical(id="chat-section"):
                    yield Label("● Chat", id="chat-label")
                    yield ChatPane(id="chat-pane")
        yield Footer()

    def on_mount(self) -> None:
        self._last_quota: dict = {}
        self._last_sessions: list = []
        self._last_refresh_at: float = 0
        # estado inicial dos indicadores
        self.query_one(SortIndicator).update_sort(self.proc_sort)
        self.query_one(ModeIndicator).update_mode(self.show_all_sessions)
        self.refresh_data()
        self.set_interval(CONFIG.get("refresh_interval_seconds", 10), self.refresh_data)
        self.set_interval(1.0, self._update_subtitle)

    # — handlers dos indicadores clicáveis ─────────────────────────────────
    def on_sort_indicator_toggled(self, event: SortIndicator.Toggled) -> None:
        self.action_toggle_sort()

    def on_mode_indicator_toggled(self, event: ModeIndicator.Toggled) -> None:
        self.action_toggle_all()

    def refresh_data(self) -> None:
        """Dispara coleta em background. Valores antigos ficam na tela
        até o worker terminar."""
        self._refresh_data_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh_data_worker(self) -> None:
        """Pesado (lê transcripts, roda subprocess) — fora da main thread.

        Paraleliza coleta de system/docker/boot/procs em ThreadPoolExecutor
        — antes era em série (~3-4s); agora roda em ~o tempo do mais lento.
        """
        quota, quota_age = load_quota()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            f_sys = ex.submit(get_system_status)
            f_sessions = ex.submit(load_sessions, self.show_all_sessions)
            f_docker = ex.submit(get_docker_info) if self.show_docker else None
            f_boot = ex.submit(get_boot_services) if self.show_boot else None
            f_procs = (
                ex.submit(get_top_processes, CONFIG.get("top_processes_n", 10), self.proc_sort)
                if self.show_processes else None
            )

            sys_status = f_sys.result()
            sessions = f_sessions.result()
            docker = f_docker.result() if f_docker else None
            boot = f_boot.result() if f_boot else None
            procs = f_procs.result() if f_procs else None

        enriched = []
        for s in sessions:
            model, tokens, ctx_pct = get_transcript_usage(s)
            extras = get_session_extras(s)
            enriched.append({
                "session": s, "model": model, "tokens": tokens,
                "ctx_pct": ctx_pct, "extras": extras,
            })

        self.app.call_from_thread(
            self._apply_refresh,
            quota, quota_age, sys_status, sessions, enriched, procs, docker, boot,
        )

    def _apply_refresh(self, quota, quota_age, sys_status, sessions, enriched, procs, docker, boot) -> None:
        """Roda na main thread — atualiza widgets atomicamente.

        Cota: só sobrescreve _last_quota se vier dado novo, senão mantém
        os valores anteriores (e marca staleness via quota_age).
        """
        if quota:
            self._last_quota = quota
        self._last_sessions = sessions
        self._last_enriched = enriched
        self._last_docker = docker
        self._last_boot = boot
        self._last_refresh_at = time.time()

        self.query_one(QuotaBar).update_quota(self._last_quota or {}, cache_age=quota_age)
        self.query_one(SystemMonitor).update_status(sys_status)
        self.query_one(SessionTable).update_sessions_enriched(enriched)
        self.query_one(ChatPane).update_context(sessions, self._last_quota or {})

        if self.show_processes and procs is not None:
            self.query_one(ProcessTable).update_processes(procs)
        # sincroniza indicadores (sempre)
        try:
            self.query_one(SortIndicator).update_sort(self.proc_sort)
            self.query_one(ModeIndicator).update_mode(self.show_all_sessions)
        except Exception:
            pass

        if self.show_docker and docker is not None:
            self.query_one(DockerPanel).update_docker(docker)
        if self.show_boot and boot is not None:
            self.query_one(BootPanel).update_boot(boot)

        self._update_subtitle()

    def _update_subtitle(self) -> None:
        split = f"split {self.left_ratio}/{100 - self.left_ratio}"
        if not self._last_refresh_at:
            self.sub_title = f"carregando... · {split}"
            return
        delta = int(time.time() - self._last_refresh_at)
        sessions = self._last_sessions
        alive = sum(1 for s in sessions if s.get("_alive", True))
        if self.show_all_sessions:
            count = f"{len(sessions)} ({alive} vivas)"
        else:
            count = f"{len(sessions)} viva(s)"
        self.sub_title = f"atualizado há {delta}s · {count} · {split}"

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

    def action_toggle_docker(self) -> None:
        self.show_docker = not self.show_docker
        section = self.query_one("#docker-section")
        if self.show_docker:
            section.add_class("-visible")
        else:
            section.remove_class("-visible")
        self.refresh_data()

    def action_toggle_boot(self) -> None:
        self.show_boot = not self.show_boot
        section = self.query_one("#boot-section")
        if self.show_boot:
            section.add_class("-visible")
        else:
            section.remove_class("-visible")
        self.refresh_data()

    def action_toggle_sort(self) -> None:
        if not self.show_processes:
            return
        self.proc_sort = "mem" if self.proc_sort == "cpu" else "cpu"
        self.refresh_data()

    # ── divisor lateral ─────────────────────────────────────────────────────
    def watch_left_ratio(self, value: int) -> None:
        try:
            self.query_one("#left-column").styles.width = f"{value}%"
            self.query_one("#right-column").styles.width = f"{100 - value}%"
        except Exception:
            pass

    def action_shrink_left(self) -> None:
        self.left_ratio = max(20, self.left_ratio - 5)
        self._update_subtitle()

    def action_grow_left(self) -> None:
        self.left_ratio = min(80, self.left_ratio + 5)
        self._update_subtitle()

    def action_reset_split(self) -> None:
        self.left_ratio = 60
        self._update_subtitle()

    # ── overlay no left-column (não cobre o chat à direita) ─────────────────
    def action_open_modal(self, kind: str) -> None:
        left = self.query_one("#left-column")
        left.add_class("-overlay-on")
        self.query_one(PanelOverlay).show_kind(kind, self)

    def action_close_overlay(self) -> None:
        try:
            left = self.query_one("#left-column")
            left.remove_class("-overlay-on")
            self.query_one(PanelOverlay).hide()
        except Exception:
            pass

    def on_section_label_clicked(self, event: SectionLabel.Clicked) -> None:
        self.action_open_modal(event.section)

    def on_session_table_session_selected(self, event: SessionTable.SessionSelected) -> None:
        self.query_one(ChatPane).set_focus(event.session_id)


if __name__ == "__main__":
    MonitorApp().run()
