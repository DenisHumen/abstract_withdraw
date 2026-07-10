"""Relay Protocol REST-клиент. Все эндпоинты публичные, API-ключ не используется.

Эндпоинты (PLAN.md §1.2-1.3):
  POST /quote/v2                       — квота + шаги исполнения
  GET  /intents/status/v3?requestId=   — статус bridge-инстента
  POST /currencies/v1                  — токен-юниверс сети
  GET  /chains                         — поддерживаемые сети / health-check
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import RELAY_API_BASE, RetryCfg
from src.core.errors import RetryableError, classify_http_error
from src.relay.types import Currency, Quote
from src import logger


class RelayClient:
    def __init__(self, proxy: str | None = None, retry_cfg: RetryCfg | None = None, timeout: float = 30):
        self._retry = retry_cfg or RetryCfg()
        self._client = httpx.Client(
            base_url=RELAY_API_BASE,
            proxy=proxy,
            timeout=timeout,
            headers={"Content-Type": "application/json", "User-Agent": "abstract-withdraw/1.0"},
        )

    def close(self) -> None:
        self._client.close()

    # ---------- низкоуровневый вызов с ретраями ----------

    def _request(self, method: str, url: str, **kw) -> Any:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self._retry.max_attempts),
            wait=wait_exponential(multiplier=self._retry.backoff_base_sec, max=self._retry.backoff_max_sec),
            retry=retry_if_exception_type(RetryableError),
        )
        def _do():
            try:
                resp = self._client.request(method, url, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise RetryableError(f"relay network: {e}") from e
            if resp.status_code >= 400:
                raise classify_http_error(resp.status_code, resp.text)
            return resp.json()

        return _do()

    # ---------- API ----------

    def get_chains(self) -> list[dict]:
        data = self._request("GET", "/chains")
        return data.get("chains", data) if isinstance(data, dict) else data

    def get_currencies(
        self,
        chain_id: int,
        verified_only: bool = False,
        use_external_search: bool = False,
        limit: int = 5000,
    ) -> list[Currency]:
        """Полный список токенов сети. Публичный, без ключа.

        Пагинации (page/offset) у /currencies/v1 нет — проверено живым тестом;
        зато limit принимает большие значения (на Abstract ~1031 токен, забираем одним вызовом).
        """
        body: dict[str, Any] = {"chainIds": [chain_id], "limit": limit}
        if verified_only:
            body["verified"] = True
        if use_external_search:
            body["useExternalSearch"] = True
        data = self._request("POST", "/currencies/v1", content=json.dumps(body))
        out = _flatten_currencies(data)
        if len(out) >= limit:
            logger.warn(
                f"currencies: получено ровно {limit} — список может быть усечён, "
                f"увеличьте tokens.currencies_limit или добавьте токены в allowlist"
            )
        return out

    def quote(
        self,
        user: str,
        recipient: str,
        origin_chain_id: int,
        dest_chain_id: int,
        origin_currency: str,
        dest_currency: str,
        amount: int,
        trade_type: str = "EXACT_INPUT",
        slippage_bps: int | None = None,
    ) -> Quote:
        body: dict[str, Any] = {
            "user": user,
            "recipient": recipient,
            "originChainId": origin_chain_id,
            "destinationChainId": dest_chain_id,
            "originCurrency": origin_currency,
            "destinationCurrency": dest_currency,
            "amount": str(amount),
            "tradeType": trade_type,
        }
        if slippage_bps is not None:
            body["slippageTolerance"] = str(slippage_bps)
        data = self._request("POST", "/quote/v2", content=json.dumps(body))
        return Quote(**data)

    def get_status(self, request_id: str) -> dict:
        return self._request("GET", "/intents/status/v3", params={"requestId": request_id})

    def post_signature(self, endpoint: str, signature: str, body: dict | None = None, method: str = "POST") -> dict:
        """kind=signature: подпись отправляется на post.endpoint (query ?signature=...)."""
        url = endpoint if endpoint.startswith("http") else endpoint
        logger.info("отправка подписи в Relay", step="SIGNATURE", endpoint=url)
        return self._request(method, url, params={"signature": signature}, content=json.dumps(body or {}))

    def check_item(self, endpoint: str, method: str = "GET") -> dict:
        """Поллинг item.check-эндпоинта транзакционного шага."""
        return self._request(method, endpoint)


def _flatten_currencies(data: Any) -> list[Currency]:
    """/currencies/v1 может отдавать список объектов или список групп (списков)."""
    items: list[dict] = []
    raw = data.get("currencies", data) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    for el in raw:
        if isinstance(el, list):
            items.extend(x for x in el if isinstance(x, dict))
        elif isinstance(el, dict):
            items.append(el)
    out = []
    for it in items:
        try:
            out.append(Currency(**it))
        except Exception:  # noqa: BLE001 — не валим дискавери из-за одного кривого токена
            continue
    return out
