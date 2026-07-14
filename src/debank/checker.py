"""Перехват DeBank-API через Playwright и парсинг протоколов кошелька."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from playwright.sync_api import Page

from src import logger

DEBANK_PROFILE = "https://debank.com/profile/{addr}"
# ключи URL внутренних API DeBank, ответы которых перехватываем.
# token/cache_balance_list — токены по ВСЕМ чейнам (вид 'All Chain'); token/balance_list — по одному.
_API_KEYS = (
    "portfolio/project_list",
    "token/cache_balance_list",
    "token/balance_list",
    "user/used_chains",
)


@dataclass
class ProtocolUsage:
    debank_id: str
    name: str
    chain: str
    net_usd: float
    item_types: list[str]
    raw: dict


@dataclass
class TokenHolding:
    chain: str
    symbol: str
    amount: float
    usd_value: float


@dataclass
class DebankResult:
    address: str
    used_chains: list[str] = field(default_factory=list)
    protocols: list[ProtocolUsage] = field(default_factory=list)
    tokens: list[TokenHolding] = field(default_factory=list)
    ok: bool = False


def check_debank(page: Page, address: str, timeout_sec: int = 40) -> DebankResult:
    """Открыть профиль DeBank и вернуть протоколы/токены/чейны кошелька."""
    captured: dict[str, dict] = {}

    def on_response(resp):
        url = resp.url
        for key in _API_KEYS:
            if key in url and key not in captured:
                try:
                    captured[key] = resp.json()
                except Exception:  # noqa: BLE001 — не JSON/ошибка парсинга
                    pass

    page.on("response", on_response)
    try:
        page.goto(DEBANK_PROFILE.format(addr=address), wait_until="domcontentloaded")
        # ждём подгрузку портфеля (project_list) либо таймаут
        for _ in range(timeout_sec):
            page.wait_for_timeout(1000)
            if "portfolio/project_list" in captured:
                page.wait_for_timeout(1500)  # добираем token/used_chains
                break
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass

    result = DebankResult(address=address)
    result.used_chains = _parse_used_chains(captured.get("user/used_chains"))
    result.protocols = _parse_projects(captured.get("portfolio/project_list"))
    result.tokens = _parse_tokens(
        captured.get("token/cache_balance_list") or captured.get("token/balance_list")
    )
    result.ok = any(k in captured for k in ("portfolio/project_list", "token/cache_balance_list", "token/balance_list"))

    logger.info(
        f"DeBank: протоколов {len(result.protocols)}, токенов {len(result.tokens)}, чейны {result.used_chains}",
        wallet=address, step="DEBANK",
    )
    return result


def _data(obj) -> object:
    if isinstance(obj, dict):
        return obj.get("data", obj)
    return obj


def _parse_used_chains(obj) -> list[str]:
    d = _data(obj)
    if isinstance(d, dict):
        ch = d.get("chains") or d.get("used_chains") or []
        if isinstance(ch, list):
            return [c if isinstance(c, str) else c.get("id", "") for c in ch]
    if isinstance(d, list):
        return [c if isinstance(c, str) else c.get("id", "") for c in d]
    return []


def _parse_projects(obj) -> list[ProtocolUsage]:
    d = _data(obj)
    projects = d if isinstance(d, list) else (d.get("project_list", []) if isinstance(d, dict) else [])
    out: list[ProtocolUsage] = []
    for prj in projects:
        if not isinstance(prj, dict):
            continue
        items = prj.get("portfolio_item_list", []) or []
        net = 0.0
        types: list[str] = []
        for it in items:
            stats = it.get("stats") or {}
            net += float(stats.get("net_usd_value", 0) or 0)
            t = it.get("name")
            if t and t not in types:
                types.append(t)
        out.append(
            ProtocolUsage(
                debank_id=str(prj.get("id", "")),
                name=str(prj.get("name", prj.get("id", "?"))),
                chain=str(prj.get("chain", "")),
                net_usd=net,
                item_types=types,
                raw=prj,
            )
        )
    return out


def _parse_tokens(obj) -> list[TokenHolding]:
    d = _data(obj)
    tokens = d if isinstance(d, list) else (d.get("token_list", []) if isinstance(d, dict) else [])
    out: list[TokenHolding] = []
    for t in tokens:
        if not isinstance(t, dict):
            continue
        amount = float(t.get("amount", 0) or 0)
        price = float(t.get("price", 0) or 0)
        if amount <= 0:
            continue
        out.append(
            TokenHolding(
                chain=str(t.get("chain", "")),
                symbol=str(t.get("optimized_symbol") or t.get("symbol") or "?"),
                amount=amount,
                usd_value=amount * price,
            )
        )
    return out
