"""Структурированное CLI-логирование: rich в терминал + JSONL в файл."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

console = Console(highlight=False)
_file_lock = threading.Lock()
_jsonl_path: Path | None = None

_STYLE = {
    "INFO": "cyan",
    "OK": "green",
    "WARN": "yellow",
    "ERROR": "bold red",
    "SKIP": "dim",
}


def setup_file_log(logs_dir: Path) -> None:
    global _jsonl_path
    logs_dir.mkdir(parents=True, exist_ok=True)
    _jsonl_path = logs_dir / f"run-{datetime.now():%Y%m%d}.jsonl"


def short_addr(addr: str | None) -> str:
    if not addr:
        return "-"
    return f"{addr[:6]}..{addr[-4:]}"


def log(
    level: str,
    msg: str,
    *,
    wallet: str | None = None,
    token: str | None = None,
    step: str | None = None,
    **extra,
) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    style = _STYLE.get(level, "white")
    parts = [f"[dim]{ts}[/dim]"]
    if wallet:
        parts.append(f"[magenta]{short_addr(wallet)}[/magenta]")
    if token:
        parts.append(f"[blue]{token}[/blue]")
    if step:
        parts.append(f"[bold]{step}[/bold]")
    parts.append(f"[{style}]{msg}[/{style}]")
    if extra:
        kv = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
        if kv:
            parts.append(f"[dim]{kv}[/dim]")
    console.print("  ".join(parts))

    if _jsonl_path is not None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "msg": msg,
            "wallet": wallet,
            "token": token,
            "step": step,
            **{k: str(v) for k, v in extra.items()},
        }
        with _file_lock:
            with open(_jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_protocols(wallet, agw: str, res) -> None:
    """Красивая таблица протоколов кошелька (rich)."""
    from rich.panel import Panel
    from rich.table import Table

    title = f"[bold]{wallet.label or short_addr(wallet.address)}[/bold]  AGW [cyan]{short_addr(agw)}[/cyan]  chains: {','.join(res.used_chains) or '-'}"
    table = Table(show_header=True, header_style="bold magenta", expand=False)
    table.add_column("Протокол", style="cyan", overflow="fold")
    table.add_column("Chain")
    table.add_column("Тип позиции")
    table.add_column("USD", justify="right", style="green")
    total = 0.0
    for pr in sorted(res.protocols, key=lambda x: -x.net_usd):
        total += pr.net_usd
        table.add_row(pr.name, pr.chain, ", ".join(pr.item_types) or "-", f"${pr.net_usd:,.2f}")
    if not res.protocols:
        table.add_row("[dim]протоколов не найдено[/dim]", "-", "-", "-")
    tok_usd = sum(t.usd_value for t in res.tokens)
    caption = f"протоколов: {len(res.protocols)}  •  в протоколах: ${total:,.2f}  •  токенов в кошельке: {len(res.tokens)} (${tok_usd:,.2f})"
    console.print(Panel(table, title=title, subtitle=caption, border_style="blue"))


def info(msg: str, **kw) -> None:
    log("INFO", msg, **kw)


def ok(msg: str, **kw) -> None:
    log("OK", msg, **kw)


def warn(msg: str, **kw) -> None:
    log("WARN", msg, **kw)


def error(msg: str, **kw) -> None:
    log("ERROR", msg, **kw)


def skip(msg: str, **kw) -> None:
    log("SKIP", msg, **kw)
