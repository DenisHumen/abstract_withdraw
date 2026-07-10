"""Типы строк БД."""
from __future__ import annotations

from dataclasses import dataclass


# Порядок статусов state machine (для resume)
JOB_FLOW = [
    "PENDING",
    "DISCOVERED",
    "QUOTED",
    "APPROVED",
    "DEPOSITED",
    "BRIDGED",
    "TRANSFERRED",
    "DONE",
]
TERMINAL_STATUSES = {"DONE", "SKIPPED"}
FAILED_STATUSES = {"FAILED", "REFUNDED", "NEEDS_BROWSER"}
# Статусы-«точки входа» в конвейер (начинаем/продолжаем обработку с них)
ENTRY_STATUSES = {"PENDING", "DISCOVERED", "QUOTED", "APPROVED", "WAITING_TARGET"}
# Ожидание внешнего условия (target_address) — ончейн-действий нет
WAITING_TARGET = "WAITING_TARGET"


@dataclass
class Wallet:
    id: int
    address: str
    target_address: str | None  # None = адрес назначения ещё не задан в XLSX
    proxy: str | None
    proxy_source: str | None
    proxy_status: str
    adspower_profile: str | None
    label: str | None
    enabled: bool
    # приватный ключ живёт только в памяти процесса (из XLSX), в БД не пишется
    private_key: str | None = None


@dataclass
class Job:
    id: int
    wallet_id: int
    token_addr: str
    symbol: str | None
    amount_in: str | None
    status: str
    request_id: str | None
    amount_out: str | None
    attempts: int
    last_error: str | None
