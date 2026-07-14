"""Оркестратор проверки протоколов: relay.link login -> AGW-адрес -> DeBank -> в БД.

Для каждого кошелька:
  1) Playwright + инжект-провайдер (один ключ) -> вход на relay.link -> получаем AGW-адрес
  2) навигация на debank.com/profile/<agw> -> перехват протоколов/токенов
  3) сохранение: каталог протоколов (растёт) + позиции кошелька + токены
Один браузерный контекст на кошелёк, через прокси кошелька (если задан).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.browser import relay_flow
from src.browser.wallet_provider import make_injector
from src.config import AppConfig
from src.core.errors import ManualError
from src.db.dao import Dao
from src.db.models import Wallet
from src.net.proxy import normalize_proxy
from src import logger


def _playwright_proxy(raw: str | None) -> dict | None:
    """login:passwd@ip:port -> playwright proxy dict."""
    p = normalize_proxy(raw)
    if not p:
        return None
    # http://login:passwd@ip:port
    rest = p.split("://", 1)[-1]
    creds, _, host = rest.rpartition("@")
    server_scheme = p.split("://", 1)[0]
    if creds:
        user, _, pwd = creds.partition(":")
        return {"server": f"{server_scheme}://{host}", "username": user, "password": pwd}
    return {"server": f"{server_scheme}://{host}"}


class ProtocolChecker:
    def __init__(self, cfg: AppConfig, dao: Dao, keys: dict[str, str]):
        self.cfg = cfg
        self.dao = dao
        self.keys = keys
        self.profiles_dir = cfg.resolve("data/.browser")
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    def check_wallet(self, wallet: Wallet, headless: bool = False) -> dict:
        pk = self.keys.get(wallet.address)
        if not pk:
            raise ManualError(f"нет приватного ключа для {wallet.address}")
        addr, inject_js, signer = make_injector(pk)
        proxy = _playwright_proxy(wallet.proxy)
        profile = self.profiles_dir / addr.lower()

        logger.info("старт проверки", wallet=wallet.address, step="CHECK",
                    proxy="on" if proxy else "off")

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                proxy=proxy,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )
            ctx.expose_binding("__walletSign", lambda source, arg: signer(arg))
            ctx.add_init_script(inject_js)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false});")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 1) вход -> AGW-адрес
            login = relay_flow.login(page, ctx)
            if not login.ok or not login.agw_address:
                ctx.close()
                raise ManualError("не удалось войти на relay.link / получить AGW-адрес")
            agw = login.agw_address
            self.dao.set_wallet_agw(wallet.id, agw)
            logger.ok(f"AGW-адрес получен: {agw}", wallet=wallet.address, step="CHECK")

            # 2) DeBank
            from src.debank.checker import check_debank
            res = check_debank(page, agw)
            ctx.close()

        if not res.ok:
            raise ManualError("DeBank не отдал данные (возможен антибот/пустой профиль)")

        # 3) сохранение
        self.dao.clear_wallet_protocols(wallet.id)
        for pr in res.protocols:
            pid = self.dao.upsert_protocol(pr.debank_id, pr.name, pr.chain, pr.raw.get("site_url"))
            self.dao.upsert_wallet_protocol(
                wallet.id, pid, agw, pr.chain, pr.net_usd, pr.item_types,
                json.dumps(pr.raw, ensure_ascii=False)[:8000],
            )
        for tk in res.tokens:
            self.dao.upsert_wallet_token_debank(wallet.id, tk.chain, tk.symbol, tk.amount, tk.usd_value)

        summary = {
            "agw": agw,
            "protocols": [(pr.name, pr.chain, round(pr.net_usd, 2), ",".join(pr.item_types)) for pr in res.protocols],
            "tokens": len(res.tokens),
            "chains": res.used_chains,
        }
        logger.print_protocols(wallet, agw, res)
        return summary

    def run(self, only_wallet: str | None = None, headless: bool = False) -> None:
        wallets = self.dao.get_wallets(enabled_only=True)
        if only_wallet:
            wallets = [w for w in wallets if w.address.lower() == only_wallet.lower()]
        wallets = [w for w in wallets if w.address in self.keys]
        if not wallets:
            logger.warn("нет кошельков для проверки (sync?)")
            return
        logger.info(f"проверка протоколов: {len(wallets)} кошельк(ов)")
        for w in wallets:
            try:
                self.check_wallet(w, headless=headless)
            except ManualError as e:
                logger.warn(f"пропуск: {e}", wallet=w.address, step="CHECK")
            except Exception as e:  # noqa: BLE001 — изоляция кошельков
                logger.error(f"ошибка проверки: {e}", wallet=w.address, step="CHECK")
            time.sleep(self.cfg.execution.random_delay())
        logger.ok("проверка протоколов завершена")
