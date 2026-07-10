"""Типизированные исключения + классификация retryable/permanent/manual. PLAN.md §8."""
from __future__ import annotations


class PipelineError(Exception):
    error_class = "retryable"


class RetryableError(PipelineError):
    """Транзиентная ошибка: RPC timeout/5xx, 429, quote expired, tx not mined."""

    error_class = "retryable"


class ProxyError(RetryableError):
    """Проблема с прокси: connect/timeout/407/5xx от прокси. Триггерит ротацию прокси."""


class RpcError(RetryableError):
    """Публичная нода упала/лимитит. Триггерит ротацию RPC внутри пула."""


class PermanentError(PipelineError):
    """No route, insufficient funds, revert, невалидный адрес — без ретраев."""

    error_class = "permanent"


class NoRouteError(PermanentError):
    """Relay не имеет маршрута для токена."""


class DustSkip(Exception):
    """Не ошибка: выход меньше порога пыли -> SKIPPED."""


class ManualError(PipelineError):
    """Нужен человек: AGW/браузер, аномальный slippage."""

    error_class = "manual"


class NeedsBrowser(ManualError):
    """Средства, вероятно, в AGW-контракте: прямой EOA-путь не видит баланс."""


def classify_http_error(status_code: int, text: str = "") -> PipelineError:
    """Классификация ответов Relay API."""
    if status_code == 429 or status_code >= 500:
        return RetryableError(f"HTTP {status_code}: {text[:200]}")
    low = text.lower()
    if "no routes" in low or "no route" in low or "route not found" in low:
        return NoRouteError(text[:200])
    return PermanentError(f"HTTP {status_code}: {text[:300]}")
