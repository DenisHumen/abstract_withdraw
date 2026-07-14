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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.browser import relay_flow
from src.browser.wallet_provider import make_injector
from src.config import AppConfig
from src.core.errors import ManualError
from src.db.dao import Dao
from src.db.models import Wallet
from src.net.proxy import ProxyPool, normalize_proxy
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
        self.proxy_pool = ProxyPool(cfg.proxy, cfg.resolve(cfg.proxy.pool_file), dao)

    _ANTI_THROTTLE = [
        "--disable-blink-features=AutomationControlled",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]

    # ---- Фаза 1: получить AGW-адрес входом на relay.link (ТЯЖЁЛО, выполняем последовательно) ----

    def _try_login(self, wallet: Wallet, inject_js: str, signer, proxy_str: str | None,
                   headless: bool, fresh: bool) -> str | None:
        """Одна попытка входа с конкретной прокси. Возвращает AGW-адрес или None.
        fresh=True -> отдельный временный профиль (чтобы битая сессия от прошлой прокси не мешала)."""
        profile = self.profiles_dir / (wallet.address.lower() if not fresh else f"{wallet.address.lower()}_t")
        pw_proxy = _playwright_proxy(proxy_str)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=headless, proxy=pw_proxy,
                args=self._ANTI_THROTTLE, viewport={"width": 1280, "height": 900},
            )
            ctx.expose_binding("__walletSign", lambda source, arg: signer(arg))
            ctx.add_init_script(inject_js)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false});")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            login = relay_flow.login(page, ctx)
            ctx.close()
        return login.agw_address if login.ok else None

    def ensure_agw(self, wallet: Wallet) -> str | None:
        """Вернуть AGW-адрес: из БД (если уже есть) либо войти на relay.link и сохранить.

        КРИТИЧНО: вход ТОЛЬКО headful. Cloudflare режет headless-браузер через датацентр-прокси
        (headless -> 429/пустая модалка), а headful решает JS-челлендж и проходит — проверено вживую.
        Часть прокси всё же не проходят Cloudflare даже headful -> РОТАЦИЯ: пробуем свою прокси, затем
        случайные из пула, пока вход не удастся (httpx-health-check НЕ используем — он даёт 429 даже на
        рабочих в браузере прокси, т.е. вводит в заблуждение). Вход read-only (только адрес).
        Вход heavy+headful -> последовательно (фаза 1); DeBank (фаза 2) — headless/параллельно."""
        if wallet.agw_address:
            return wallet.agw_address
        pk = self.keys.get(wallet.address)
        if not pk:
            raise ManualError(f"нет приватного ключа для {wallet.address}")
        _, inject_js, signer = make_injector(pk)

        # кандидаты: своя прокси + случайные из пула (некоторые не проходят Cloudflare -> ротируем)
        candidates: list[str | None] = []
        own = normalize_proxy(wallet.proxy) if self.cfg.proxy.enabled else None
        if own:
            candidates.append(own)
        if self.cfg.proxy.enabled:
            for _ in range(max(1, self.cfg.execution.login_proxy_tries)):
                cand = self.proxy_pool.pick_random(exclude=None)
                if cand and cand not in candidates:
                    candidates.append(cand)
        if not candidates:
            candidates = [None]

        for i, proxy_str in enumerate(candidates):
            logger.info(f"вход на relay.link (headful, прокси {i + 1}/{len(candidates)}) ...",
                        wallet=wallet.address, step="LOGIN", proxy="on" if proxy_str else "off")
            try:
                agw = self._try_login(wallet, inject_js, signer, proxy_str, headless=False, fresh=(i > 0))
            except Exception as e:  # noqa: BLE001
                logger.warn(f"вход упал: {str(e)[:60]}", wallet=wallet.address, step="LOGIN")
                agw = None
            if agw:
                self.dao.set_wallet_agw(wallet.id, agw)
                if proxy_str and proxy_str != own and self.cfg.proxy.persist_assignment:
                    self.dao.set_wallet_proxy(wallet.id, proxy_str, "pool", "ok")
                logger.ok(f"AGW-адрес: {agw}", wallet=wallet.address, step="LOGIN")
                return agw
        raise ManualError(f"вход не удался (headful, {len(candidates)} прокси)")

    # ---- Фаза 2: DeBank-проверка по AGW-адресу (ЛЕГКО, публичная страница -> параллелим) ----

    def debank_check(self, wallet: Wallet, agw: str, headless: bool = True) -> None:
        """Открыть debank.com/profile/<agw> (без логина!) и сохранить протоколы. Параллель-безопасно."""
        from src.debank.checker import check_debank

        proxy = _playwright_proxy(wallet.proxy)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=self._ANTI_THROTTLE)
            ctx = browser.new_context(proxy=proxy, viewport={"width": 1280, "height": 900})
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false});")
            page = ctx.new_page()
            res = check_debank(page, agw)
            ctx.close()
            browser.close()
        if not res.ok:
            raise ManualError("DeBank не отдал данные (антибот/пустой профиль)")
        self.dao.clear_wallet_protocols(wallet.id)
        for pr in res.protocols:
            pid = self.dao.upsert_protocol(pr.debank_id, pr.name, pr.chain, pr.raw.get("site_url"))
            self.dao.upsert_wallet_protocol(
                wallet.id, pid, agw, pr.chain, pr.net_usd, pr.item_types,
                json.dumps(pr.raw, ensure_ascii=False)[:8000],
            )
        for tk in res.tokens:
            self.dao.upsert_wallet_token_debank(wallet.id, tk.chain, tk.symbol, tk.amount, tk.usd_value)
        logger.print_protocols(wallet, agw, res)

    def _debank_safe(self, wallet: Wallet, headless: bool) -> None:
        try:
            agw = wallet.agw_address or self.dao.get_wallet(wallet.address).agw_address  # type: ignore[union-attr]
            if not agw:
                logger.warn("нет AGW-адреса — пропуск DeBank", wallet=wallet.address, step="DEBANK")
                return
            self.debank_check(wallet, agw, headless=headless)
        except ManualError as e:
            logger.warn(f"пропуск: {e}", wallet=wallet.address, step="DEBANK")
        except Exception as e:  # noqa: BLE001 — изоляция кошельков
            logger.error(f"ошибка DeBank: {e}", wallet=wallet.address, step="DEBANK")

    def run(self, only_wallet: str | None = None, headless: bool = True, threads: int | None = None) -> None:
        wallets = self.dao.get_wallets(enabled_only=True)
        if only_wallet:
            wallets = [w for w in wallets if w.address.lower() == only_wallet.lower()]
        wallets = [w for w in wallets if w.address in self.keys]
        if not wallets:
            logger.warn("нет кошельков для проверки (sync?)")
            return

        n_threads = max(1, threads or self.cfg.execution.check_concurrency)
        n_threads = min(n_threads, len(wallets))

        # --- Фаза 1: последовательно получаем AGW-адреса (вход heavy, параллель ломает relay.link).
        #     Кошельки с уже сохранённым agw_address вход пропускают -> повторные прогоны идут сразу в фазу 2.
        need_login = [w for w in wallets if not w.agw_address]
        if need_login:
            logger.info(f"фаза 1: вход и получение AGW для {len(need_login)} кошельк(ов) "
                        f"(последовательно, headful — откроются окна браузера; адреса кэшируются в БД)")
            for idx, w in enumerate(need_login):
                try:
                    self.ensure_agw(w)
                except ManualError as e:
                    logger.warn(f"пропуск входа: {e}", wallet=w.address, step="LOGIN")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"ошибка входа: {e}", wallet=w.address, step="LOGIN")
                # пауза между входами -> не провоцируем рейт-лимит Cloudflare частыми входами с одного IP
                if idx + 1 < len(need_login):
                    time.sleep(self.cfg.execution.random_delay())

        # перечитываем кошельки (agw_address теперь проставлен)
        wallets = [self.dao.get_wallet(w.address) for w in wallets]
        ready = [w for w in wallets if w and w.agw_address]
        if not ready:
            logger.warn("ни у одного кошелька нет AGW-адреса — DeBank-проверка пропущена")
            return

        # --- Фаза 2: DeBank-проверка параллельно (публичные страницы, логин не нужен -> потоки безопасны).
        logger.info(f"фаза 2: DeBank-проверка {len(ready)} кошельк(ов), потоков={n_threads}")
        if n_threads == 1:
            for w in ready:
                self._debank_safe(w, headless)
        else:
            with ThreadPoolExecutor(max_workers=n_threads) as pool:
                futures = [pool.submit(self._debank_safe, w, headless) for w in ready]
                for _ in as_completed(futures):
                    pass
        logger.ok("проверка протоколов завершена")
