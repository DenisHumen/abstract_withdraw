"""Оркестратор проверки протоколов: relay.link login -> AGW-адрес -> DeBank -> в БД.

На КАЖДЫЙ кошелёк — ДВЕ задачи в БД (check_tasks): get_agw и check_protocols.
Логика: получили AGW-адрес -> СРАЗУ ЖЕ его DeBank-проверка. Если agw_address уже известен,
задача 1 моментально DONE и кошелёк идёт прямиком на DeBank (параллельно с другими такими же).
Входы на relay.link — строго последовательно (heavy headful, параллель ломает shadow-DOM).
Сохранение: каталог протоколов (растёт) + позиции кошелька + токены. Прокси — кошелька.
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
from src.db.models import (
    Wallet, TASK_GET_AGW, TASK_CHECK_PROTOCOLS,
    CHECK_PENDING, CHECK_RUNNING, CHECK_DONE, CHECK_FAILED,
)
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


# Анти-троттлинг Chromium: без этих флагов фоновые окна душат рендер тяжёлого SPA relay.link.
# Используется и чекером, и браузерным мостом (agw_bridge).
ANTI_THROTTLE_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]


class ProtocolChecker:
    def __init__(self, cfg: AppConfig, dao: Dao, keys: dict[str, str]):
        self.cfg = cfg
        self.dao = dao
        self.keys = keys
        self.profiles_dir = cfg.resolve("data/.browser")
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.proxy_pool = ProxyPool(cfg.proxy, cfg.resolve(cfg.proxy.pool_file), dao)

    _ANTI_THROTTLE = ANTI_THROTTLE_ARGS

    # ---- Задача 1 (get_agw): AGW-адрес входом на relay.link (ТЯЖЕЛО, выполняем последовательно) ----

    def _try_login(self, wallet: Wallet, inject_js: str, signer, proxy_str: str | None,
                   attempt: int) -> str | None:
        """Одна попытка входа headful с конкретной прокси. Возвращает AGW-адрес или None.
        КРИТИЧНО: СВЕЖИЙ профиль на каждую попытку — иначе флагнутые Cloudflare-куки от неудачной
        попытки тянутся в следующую и валят ротацию (проверено: fresh-профиль каждый раз = ok)."""
        import shutil

        profile = self.profiles_dir / f"{wallet.address.lower()}_a{attempt}"
        shutil.rmtree(profile, ignore_errors=True)
        pw_proxy = _playwright_proxy(proxy_str)
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile), headless=False, proxy=pw_proxy,
                    args=self._ANTI_THROTTLE, viewport={"width": 1280, "height": 900},
                )
                ctx.expose_binding("__walletSign", lambda source, arg: signer(arg))
                ctx.add_init_script(inject_js)
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false});")
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                login = relay_flow.login(page, ctx)
                ctx.close()
            return login.agw_address if login.ok else None
        finally:
            shutil.rmtree(profile, ignore_errors=True)  # чистим за собой (в т.ч. флагнутые куки)

    def ensure_agw(self, wallet: Wallet) -> str | None:
        """Вернуть AGW-адрес: из БД (если уже есть) либо войти на relay.link и сохранить.

        КРИТИЧНО: вход ТОЛЬКО headful. Cloudflare режет headless-браузер через датацентр-прокси
        (headless -> 429/пустая модалка), а headful решает JS-челлендж и проходит — проверено вживую.
        Часть прокси всё же не проходят Cloudflare даже headful -> РОТАЦИЯ: пробуем свою прокси, затем
        случайные из пула, пока вход не удастся (httpx-health-check НЕ используем — он даёт 429 даже на
        рабочих в браузере прокси, т.е. вводит в заблуждение). Вход read-only (только адрес).
        Вход heavy+headful -> вызывается строго последовательно (см. run)."""
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
            if i > 0:
                time.sleep(self.cfg.execution.random_delay())  # пауза между ротациями (не хаммерим Cloudflare)
            logger.info(f"вход на relay.link (headful, прокси {i + 1}/{len(candidates)}) ...",
                        wallet=wallet.address, step="LOGIN", proxy="on" if proxy_str else "off")
            try:
                agw = self._try_login(wallet, inject_js, signer, proxy_str, attempt=i)
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

    # ---- Задача 2 (check_protocols): DeBank по AGW-адресу (ЛЕГКО, публичная страница -> параллелим) ----

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

    def _task_check_protocols(self, wallet: Wallet, agw: str, headless: bool) -> None:
        """ЗАДАЧА 2: DeBank-проверка по AGW-адресу. Обновляет статус задачи в БД. Параллель-безопасно."""
        self.dao.set_check_task(wallet.id, TASK_CHECK_PROTOCOLS, CHECK_RUNNING)
        try:
            self.debank_check(wallet, agw, headless=headless)
            self.dao.set_check_task(wallet.id, TASK_CHECK_PROTOCOLS, CHECK_DONE)
        except ManualError as e:
            self.dao.set_check_task(wallet.id, TASK_CHECK_PROTOCOLS, CHECK_FAILED, str(e)[:200])
            logger.warn(f"DeBank пропуск: {e}", wallet=wallet.address, step="DEBANK")
        except Exception as e:  # noqa: BLE001 — изоляция кошельков
            self.dao.set_check_task(wallet.id, TASK_CHECK_PROTOCOLS, CHECK_FAILED, str(e)[:200])
            logger.error(f"ошибка DeBank: {e}", wallet=wallet.address, step="DEBANK")

    def _task_get_agw(self, wallet: Wallet) -> str | None:
        """ЗАДАЧА 1: получить AGW-адрес. Если он уже есть в БД — задача СРАЗУ DONE (входа нет).
        Иначе вход на relay.link (serial). Возвращает AGW или None (при провале задача 2 -> FAILED)."""
        if wallet.agw_address:
            self.dao.set_check_task(wallet.id, TASK_GET_AGW, CHECK_DONE)
            logger.ok(f"AGW уже есть: {wallet.agw_address} — сразу к проверке протоколов",
                      wallet=wallet.address, step="AGW")
            return wallet.agw_address
        self.dao.set_check_task(wallet.id, TASK_GET_AGW, CHECK_RUNNING)
        try:
            agw = self.ensure_agw(wallet)  # вход heavy+headful -> строго последовательно
            self.dao.set_check_task(wallet.id, TASK_GET_AGW, CHECK_DONE)
            return agw
        except Exception as e:  # noqa: BLE001 — ManualError и прочее
            self.dao.set_check_task(wallet.id, TASK_GET_AGW, CHECK_FAILED, str(e)[:200])
            self.dao.set_check_task(wallet.id, TASK_CHECK_PROTOCOLS, CHECK_FAILED, "нет AGW-адреса")
            logger.warn(f"вход не удался: {e}", wallet=wallet.address, step="AGW")
            return None

    _LOGIN_STAGGER_SEC = 8.0  # растяжка стартов login-потоков: не запускаем все Chromium одновременно

    def _login_chunk(self, chunk: list[Wallet], headless: bool, start_delay: float = 0.0) -> None:
        """Воркер группы 2: свой список кошельков ПОСЛЕДОВАТЕЛЬНО, «вход -> сразу его DeBank -> следующий».
        start_delay растягивает старты потоков (одновременный запуск N Chromium душит CPU и срывает входы)."""
        if start_delay > 0:
            time.sleep(start_delay)
        for idx, w in enumerate(chunk):
            # пауза между входами ВНУТРИ потока (анти-рейтлимит Cloudflare; у кошельков свои прокси,
            # но пул ротации общий -> щадящий темп на поток)
            if idx > 0:
                time.sleep(self.cfg.execution.login_delay())
            agw = self._task_get_agw(w)  # задача 1: вход
            if not agw:
                continue
            self.dao.set_check_task(w.id, TASK_CHECK_PROTOCOLS, CHECK_PENDING)
            self._task_check_protocols(w, agw, headless)  # задача 2: СРАЗУ, тем же потоком

    def run(self, only_wallet: str | None = None, headless: bool = True,
            threads: int | None = None, login_threads: int | None = None) -> None:
        """На КАЖДЫЙ кошелёк — две задачи в БД: (1) получить AGW, (2) проверить протоколы.
        Логика: получили адрес -> СРАЗУ ЖЕ его DeBank-проверка. Две группы:
          • have_agw (адрес уже есть): задача1=DONE моментально, задача2 (DeBank) — ПАРАЛЛЕЛЬНО (--threads);
          • need_login (нужен вход): ПАРАЛЛЕЛЬНО в login_threads потоков (--login-threads /
            execution.login_concurrency); каждый поток ведёт свои кошельки последовательно:
            login -> сразу его DeBank -> следующий. Старты потоков растянуты (_LOGIN_STAGGER_SEC).
        ВНИМАНИЕ: каждый вход = headful Chromium (тяжёлый). Слишком много login-потоков перегружает
        машину -> модалка relay.link не грузится за 60с и входы падают. Если входы массово падают —
        уменьшите login_threads (в пределе 1 = прежний надёжный последовательный режим).
        Кошельки с известным agw_address задачу 1 пропускают (сразу DONE) -> прямиком на проверку."""
        wallets = self.dao.get_wallets(enabled_only=True)
        if only_wallet:
            wallets = [w for w in wallets if w.address.lower() == only_wallet.lower()]
        wallets = [w for w in wallets if w.address in self.keys]
        if not wallets:
            logger.warn("нет кошельков для проверки (sync?)")
            return

        n_threads = max(1, threads or self.cfg.execution.check_concurrency)
        n_login = max(1, login_threads or self.cfg.execution.login_concurrency)

        # гарантируем наличие ДВУХ задач у каждого кошелька (идемпотентно, статусы не сбрасываем)
        for w in wallets:
            self.dao.ensure_check_task(w.id, TASK_GET_AGW)
            self.dao.ensure_check_task(w.id, TASK_CHECK_PROTOCOLS)

        have_agw = [w for w in wallets if w.agw_address]        # адрес готов -> задача1 сразу DONE
        need_login = [w for w in wallets if not w.agw_address]  # нужен вход (heavy headful)
        logger.info(f"чекер: {len(wallets)} кошельк(ов). С готовым AGW: {len(have_agw)} "
                    f"(DeBank параллельно, потоков={min(n_threads, len(have_agw) or 1)}); "
                    f"нужен вход: {len(need_login)} (потоков входа={min(n_login, len(need_login) or 1)}, "
                    f"в каждом: вход -> сразу DeBank).")

        # ---- Группа 1: кошельки с готовым AGW. Задача1 = DONE моментально; задача2 (DeBank) ПАРАЛЛЕЛЬНО.
        #      Входов тут нет -> параллельные headless-браузеры безопасны (проверено: 2+ DeBank ок).
        if have_agw:
            for w in have_agw:
                self._task_get_agw(w)  # проставит get_agw=DONE (agw уже есть)
                self.dao.set_check_task(w.id, TASK_CHECK_PROTOCOLS, CHECK_PENDING)
            workers = min(n_threads, len(have_agw))
            if workers == 1:
                for w in have_agw:
                    self._task_check_protocols(w, w.agw_address, headless)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(self._task_check_protocols, w, w.agw_address, headless)
                               for w in have_agw]
                    for _ in as_completed(futures):
                        pass

        # ---- Группа 2: нужен вход. ПАРАЛЛЕЛЬНО в n_login потоков: кошельки делятся round-robin
        #      на чанки, каждый воркер идёт по своему чанку последовательно (login -> сразу debank).
        #      Так одновременно открыто не больше n_login браузеров, а старты растянуты stagger-ом.
        #      sync_playwright() создаётся внутри _try_login на каждый вызов -> потокобезопасно.
        if need_login:
            workers = min(n_login, len(need_login))
            if workers == 1:
                self._login_chunk(need_login, headless)
            else:
                chunks = [need_login[i::workers] for i in range(workers)]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(self._login_chunk, chunk, headless, i * self._LOGIN_STAGGER_SEC)
                               for i, chunk in enumerate(chunks)]
                    for _ in as_completed(futures):
                        pass

        logger.ok("проверка протоколов завершена")
        self._print_check_summary()

    _SUMMARY_DETAIL_LIMIT = 30  # полную таблицу печатаем только до этого числа кошельков

    def _print_check_summary(self) -> None:
        """Сводка по двум задачам чекера. На малом числе кошельков — полная таблица;
        на большом (1000+) — агрегат + только проблемные строки (иначе вывод нечитаем)."""
        from rich.table import Table

        rows = self.dao.check_tasks_summary()
        if not rows:
            return
        mark = {CHECK_DONE: "[green]DONE[/green]", CHECK_FAILED: "[red]FAILED[/red]",
                CHECK_RUNNING: "[yellow]RUNNING[/yellow]", CHECK_PENDING: "[dim]PENDING[/dim]"}

        def is_problem(r) -> bool:
            return r["get_agw"] != CHECK_DONE or r["check_protocols"] != CHECK_DONE

        problems = [r for r in rows if is_problem(r)]
        agw_done = sum(1 for r in rows if r["get_agw"] == CHECK_DONE)
        proto_done = sum(1 for r in rows if r["check_protocols"] == CHECK_DONE)
        logger.info(f"итог: кошельков {len(rows)}; задача 1 (AGW) DONE у {agw_done}; "
                    f"задача 2 (протоколы) DONE у {proto_done}; проблемных {len(problems)}")

        detail = rows if len(rows) <= self._SUMMARY_DETAIL_LIMIT else problems[: self._SUMMARY_DETAIL_LIMIT]
        if not detail:
            return
        title = ("Задачи чекера протоколов" if len(rows) <= self._SUMMARY_DETAIL_LIMIT
                 else f"Проблемные кошельки (первые {len(detail)} из {len(problems)})")
        table = Table(title=title, title_style="bold cyan")
        table.add_column("Кошелёк")
        table.add_column("1) AGW-адрес")
        table.add_column("2) Протоколы")
        table.add_column("AGW", overflow="fold")
        table.add_column("Найдено", justify="right")
        table.add_column("Ошибка", overflow="fold")
        for r in detail:
            short = f"{r['address'][:6]}..{r['address'][-4:]}"
            err = (r["proto_error"] or r["agw_error"] or "")[:60]
            table.add_row(
                short,
                mark.get(r["get_agw"], "[dim]—[/dim]"),
                mark.get(r["check_protocols"], "[dim]—[/dim]"),
                (r["agw_address"] or "—"),
                str(r["n_protocols"] or 0),
                err,
            )
        logger.console.print(table)
