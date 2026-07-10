"""Пул HTTP-прокси: назначение per-wallet (sticky), health-check, ротация при сбое.

Формат строки прокси: login:passwd@ip:port  (нормализуется в http://login:passwd@ip:port)
Пул: data/proxies.txt, по одной на строку, '#' — комментарий.
"""
from __future__ import annotations

import random
import threading
from pathlib import Path

import httpx

from src.config import RELAY_API_BASE, ProxyCfg
from src.core.errors import ProxyError
from src.db.dao import Dao
from src import logger


def normalize_proxy(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://", "socks5://")):
        return raw
    return f"http://{raw}"


class ProxyPool:
    """Потокобезопасный пул прокси из файла + учёт dead."""

    def __init__(self, cfg: ProxyCfg, pool_file: Path, dao: Dao):
        self.cfg = cfg
        self.dao = dao
        self._lock = threading.Lock()
        self._dead: set[str] = set()
        self._pool: list[str] = []
        if pool_file.exists():
            for line in pool_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    p = normalize_proxy(line)
                    if p:
                        self._pool.append(p)

    @property
    def size(self) -> int:
        return len(self._pool)

    def alive(self) -> list[str]:
        with self._lock:
            return [p for p in self._pool if p not in self._dead]

    def mark_dead(self, proxy: str) -> None:
        with self._lock:
            self._dead.add(proxy)
        logger.warn("прокси помечена dead", step="PROXY", proxy=_mask(proxy))

    def pick_random(self, exclude: str | None = None) -> str | None:
        candidates = [p for p in self.alive() if p != exclude]
        return random.choice(candidates) if candidates else None

    def health_check(self, proxy: str) -> bool:
        """Быстрый GET публичного Relay /chains через прокси."""
        try:
            with httpx.Client(proxy=proxy, timeout=self.cfg.health_check_timeout_sec) as client:
                r = client.get(f"{RELAY_API_BASE}/chains", params={"limit": 1})
                return r.status_code < 500
        except Exception:
            return False

    def assign(self, wallet_id: int, current: str | None, from_xlsx: str | None) -> str | None:
        """Выбор прокси для кошелька: XLSX -> сохранённая -> случайная из пула."""
        if not self.cfg.enabled:
            return None
        # 1) прокси из XLSX (приоритет)
        p = normalize_proxy(from_xlsx) if self.cfg.per_wallet_from_xlsx else None
        source = "xlsx" if p else None
        # 2) ранее назначенная (sticky)
        if not p and self.cfg.sticky:
            p = normalize_proxy(current)
            source = "pool" if p else None
        # 3) из пула
        if not p:
            p = self.pick_random()
            source = "pool"
        if not p:
            return None

        if self.cfg.health_check and not self.health_check(p):
            self.mark_dead(p)
            p = self.pick_random(exclude=p)
            source = "pool"
            if p is None:
                raise ProxyError("нет живых прокси в пуле")

        if self.cfg.persist_assignment:
            self.dao.set_wallet_proxy(wallet_id, p, source or "pool", "ok")
        return p

    def rotate(self, wallet_id: int, dead_proxy: str | None) -> str:
        """Ротация при сбое: пометить dead, взять случайную рабочую, сохранить в БД."""
        if dead_proxy:
            self.mark_dead(dead_proxy)
            self.dao.set_wallet_proxy_status(wallet_id, "dead")
        new = self.pick_random(exclude=dead_proxy)
        if new is None:
            raise ProxyError("пул прокси исчерпан (все dead)")
        if self.cfg.persist_assignment:
            self.dao.set_wallet_proxy(wallet_id, new, "pool", "ok")
        logger.info("прокси заменена", step="PROXY", new=_mask(new))
        return new


def _mask(proxy: str | None) -> str:
    """login:passwd@ip:port -> ***@ip:port (не светим креды в логах)."""
    if not proxy:
        return "-"
    tail = proxy.rsplit("@", 1)[-1]
    return f"***@{tail}"


def is_proxy_error(exc: Exception) -> bool:
    """Эвристика: ошибка вызвана прокси/сетью до апстрима."""
    if isinstance(exc, httpx.ProxyError):
        return True
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("proxy", "407", "tunnel", "connection refused", "connect timeout"))
