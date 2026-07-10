"""Пул публичных RPC на сеть: health-check, ротация при сбое/лимите, web3 через прокси."""
from __future__ import annotations

import threading

from web3 import HTTPProvider, Web3

from src.config import RpcCfg
from src.core.errors import RpcError
from src import logger


class RpcPool:
    """Пул публичных нод одной сети. Ротация: сбой -> следующая нода."""

    def __init__(self, name: str, urls: list[str], cfg: RpcCfg):
        if not urls:
            raise RpcError(f"пустой пул RPC для {name}")
        self.name = name
        self.urls = urls
        self.cfg = cfg
        self._idx = 0
        self._lock = threading.Lock()

    @property
    def current(self) -> str:
        with self._lock:
            return self.urls[self._idx % len(self.urls)]

    def rotate(self) -> str:
        with self._lock:
            self._idx += 1
            url = self.urls[self._idx % len(self.urls)]
        logger.warn(f"RPC {self.name}: переключение ноды", step="RPC", url=url)
        return url

    def make_web3(self, proxy: str | None = None, url: str | None = None) -> Web3:
        request_kwargs: dict = {"timeout": self.cfg.timeout_sec}
        if proxy:
            request_kwargs["proxies"] = {"http": proxy, "https": proxy}
        return Web3(HTTPProvider(url or self.current, request_kwargs=request_kwargs))

    def healthy_web3(self, proxy: str | None = None, expected_chain_id: int | None = None) -> Web3:
        """web3 с ротацией: до max_rotations_per_call попыток найти живую ноду."""
        last_exc: Exception | None = None
        for _ in range(max(1, self.cfg.max_rotations_per_call)):
            w3 = self.make_web3(proxy)
            if not self.cfg.health_check:
                return w3
            try:
                cid = w3.eth.chain_id
                if expected_chain_id is not None and cid != expected_chain_id:
                    raise RpcError(f"{self.name}: chainId {cid} != {expected_chain_id}")
                return w3
            except Exception as e:  # noqa: BLE001 — нода мертва/лимитит, пробуем следующую
                last_exc = e
                if self.cfg.rotate_on_failure:
                    self.rotate()
                else:
                    break
        raise RpcError(f"нет живых RPC для {self.name}: {last_exc}")


def call_with_rpc_rotation(pool: RpcPool, fn, *args, **kwargs):
    """Выполнить fn(w3, ...) с ротацией нод при RpcError/сетевых сбоях."""
    last_exc: Exception | None = None
    for attempt in range(max(1, pool.cfg.max_rotations_per_call)):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            text = str(e).lower()
            transient = any(
                k in text
                for k in ("timeout", "429", "too many", "503", "502", "connection", "temporarily")
            )
            if transient and pool.cfg.rotate_on_failure and attempt < pool.cfg.max_rotations_per_call - 1:
                pool.rotate()
                continue
            raise
    raise RpcError(f"RPC {pool.name} исчерпан: {last_exc}")
