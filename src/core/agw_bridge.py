"""Браузерный мост AGW: native ETH Abstract -> Base (свой EOA) -> биржевой адрес из XLSX.

Пошаговый алгоритм (по ТЗ пользователя):
  1) Вход на relay.link через Privy (существующий relay_flow.login — БЕЗ изменений) и мост
     ИМЕННО native ETH: Buy=ETH@Base, получатель = наш EOA (деривится из private_key в XLSX),
     адрес вставляется через дропдаун 'Paste wallet address' (relay_flow.bridge_native_eth).
  2) Мониторинг Base: ждём фактического зачисления ETH на наш EOA (watch_balance_increase, RPC).
  3) Отправка на биржу: target_address из XLSX. Есть адрес — подписываем ключом и шлём весь
     баланс минус резерв газа (steps.forward_transfer, идемпотентно). Адреса нет — job в
     WAITING_TARGET и режим ожидания: перечитываем XLSX каждые target_poll_interval_sec,
     появился адрес — подхватываем и отправляем.

Сканирование прочих токенов здесь НЕ выполняется вообще (native ETH — единственная цель).
Статусы job (token=native): DISCOVERED -> BRIDGED -> TRANSFERRED -> DONE | WAITING_TARGET | FAILED.
"""
from __future__ import annotations

import shutil
import time

from playwright.sync_api import sync_playwright
from web3 import Web3

from src.browser import relay_flow
from src.browser.wallet_provider import make_injector
from src.chains.base import BaseClient
from src.config import AppConfig, NATIVE_TOKEN
from src.core import steps as st
from src.core.errors import ManualError, PermanentError, RetryableError
from src.core.protocol_check import ANTI_THROTTLE_ARGS, _playwright_proxy
from src.data_io import excel
from src.db.dao import Dao
from src.db.models import Job, Wallet, WAITING_TARGET
from src.net.proxy import ProxyPool, normalize_proxy
from src.net.rpc import RpcPool
from src import logger


class AgwBridge:
    """Оркестратор браузерного моста. Кошельки последовательно (headful-браузер тяжёлый)."""

    def __init__(self, cfg: AppConfig, dao: Dao, keys: dict[str, str]):
        self.cfg = cfg
        self.dao = dao
        self.keys = keys  # address -> private_key (только в памяти)
        self.profiles_dir = cfg.resolve("data/.browser")
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.proxy_pool = ProxyPool(cfg.proxy, cfg.resolve(cfg.proxy.pool_file), dao)
        self.abstract_rpc = RpcPool("abstract", cfg.env.rpc_pool("abstract"), cfg.rpc)
        self.base_rpc = RpcPool("base", cfg.env.rpc_pool("base"), cfg.rpc)

    # ---------------------------------------------------------------- инфраструктура (переопределяемо в тестах)

    def _make_base(self, wallet: Wallet, proxy: str | None) -> BaseClient:
        pk = self.keys[wallet.address]
        w3 = self.base_rpc.healthy_web3(proxy, expected_chain_id=self.cfg.routing.dest_chain_id)
        return BaseClient(w3, pk)

    def _agw_balance(self, agw_address: str, proxy: str | None) -> int:
        """Баланс native ETH на AGW-контракте в Abstract (read-only, ключ не нужен)."""
        w3 = self.abstract_rpc.healthy_web3(proxy, expected_chain_id=self.cfg.routing.origin_chain_id)
        return w3.eth.get_balance(Web3.to_checksum_address(agw_address))

    shots_dir = None  # если задан (Path) — скриншоты стадий входа/моста для диагностики

    def _bridge_via_browser(self, wallet: Wallet, proxy_str: str | None) -> tuple[str | None, bool]:
        """Вход (существующий login, без изменений) + мост в ОДНОМ браузере.
        Возвращает (agw_address, approved). Свежий профиль на попытку (анти-Cloudflare-куки)."""
        pk = self.keys[wallet.address]
        _, inject_js, signer = make_injector(pk)
        profile = self.profiles_dir / f"{wallet.address.lower()}_bridge"
        shutil.rmtree(profile, ignore_errors=True)
        pw_proxy = _playwright_proxy(proxy_str)

        def shot(page, name):
            if self.shots_dir:
                try:
                    page.screenshot(path=str(self.shots_dir / name))
                except Exception:  # noqa: BLE001
                    pass

        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile), headless=False, proxy=pw_proxy,
                    args=ANTI_THROTTLE_ARGS, viewport={"width": 1280, "height": 900},
                )
                ctx.expose_binding("__walletSign", lambda source, arg: signer(arg))
                ctx.add_init_script(inject_js)
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false});")
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                login = relay_flow.login(page, ctx)
                shot(page, "a01_after_login.png")
                if not login.ok:
                    ctx.close()
                    return None, False
                logger.info(f"вход ок, AGW={login.agw_address}", wallet=wallet.address, step="BRIDGE")
                # Мост: Buy=ETH@Base -> recipient='Paste wallet address'(наш EOA) -> MAX -> SWAP -> Approve.
                res = relay_flow.bridge_native_eth(page, ctx, recipient=wallet.address,
                                                   dry_confirm=False, shots_dir=self.shots_dir)
                logger.info(f"мост-флоу: {res.get('note')}", wallet=wallet.address, step="BRIDGE")
                ctx.close()
                return login.agw_address, bool(res.get("approved"))
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    # ---------------------------------------------------------------- шаги

    def _forward_ctx(self, wallet: Wallet, base: BaseClient) -> st.WalletCtx:
        # forward_transfer использует только cfg/dao/wallet/base — abstract/relay ему не нужны
        return st.WalletCtx(cfg=self.cfg, dao=self.dao, wallet=wallet,
                            abstract=None, base=base, relay=None)  # type: ignore[arg-type]

    def _bridge_step(self, wallet: Wallet, job: Job, base: BaseClient, proxy: str | None,
                     dry_run: bool) -> bool:
        """Мост AGW->Base(наш EOA). True = средства на Base (можно пересылать)."""
        baseline = base.native_balance()
        agw = wallet.agw_address
        agw_bal = self._agw_balance(agw, proxy) if agw else None

        # Крэш-ресюм и пустые кошельки: AGW пуст -> мостить нечего.
        if agw_bal is not None and agw_bal < self.cfg.amounts.min_out:
            if baseline >= self.cfg.amounts.min_out:
                # средства уже на Base (мост прошёл в прошлый раз / были там) -> сразу к отправке
                logger.info("AGW пуст, но на Base уже есть средства — переходим к отправке",
                            wallet=wallet.address, step="BRIDGE", base_wei=baseline)
                self.dao.update_job(job.id, status="BRIDGED", amount_out=str(baseline))
                return True
            logger.skip(f"AGW пуст ({agw_bal} wei) и на Base пусто — мостить нечего (пропуск в этом прогоне)",
                        wallet=wallet.address, step="BRIDGE")
            return False

        if dry_run:
            logger.ok(f"[dry-run] мост ETH AGW->Base: AGW={agw or '?'} bal={agw_bal} wei -> "
                      f"recipient {wallet.address} (браузер не запускался)",
                      wallet=wallet.address, step="DRY")
            return False

        # Кандидаты прокси: своя + случайные из пула (Cloudflare режет часть) — как в чекере.
        candidates: list[str | None] = []
        own = normalize_proxy(wallet.proxy) if self.cfg.proxy.enabled else None
        if own:
            candidates.append(own)
        if self.cfg.proxy.enabled:
            for _ in range(max(1, self.cfg.execution.login_proxy_tries)):
                cand = self.proxy_pool.pick_random()
                if cand and cand not in candidates:
                    candidates.append(cand)
        if not candidates:
            candidates = [None]

        approved = False
        for i, proxy_str in enumerate(candidates):
            # Анти-двойной-мост: если AGW уже опустошён (прошлая попытка/прогон сбриджили, но Approve
            # мог не задетектиться) — НЕ мостим повторно, сразу к ожиданию зачисления на Base.
            if wallet.agw_address:
                try:
                    if self._agw_balance(wallet.agw_address, proxy) < self.cfg.amounts.min_out:
                        logger.info("AGW опустошён — мост уже прошёл; ждём зачисление на Base",
                                    wallet=wallet.address, step="BRIDGE")
                        approved = True
                        break
                except Exception:  # noqa: BLE001 — RPC мигнул, продолжаем обычным путём
                    pass
            if i > 0:
                time.sleep(self.cfg.execution.random_delay())
            logger.info(f"мост через браузер (headful, прокси {i + 1}/{len(candidates)}) ...",
                        wallet=wallet.address, step="BRIDGE", proxy="on" if proxy_str else "off")
            try:
                got_agw, approved = self._bridge_via_browser(wallet, proxy_str)
            except Exception as e:  # noqa: BLE001
                logger.warn(f"браузер упал: {str(e)[:80]}", wallet=wallet.address, step="BRIDGE")
                got_agw, approved = None, False
            if got_agw and got_agw != wallet.agw_address:
                self.dao.set_wallet_agw(wallet.id, got_agw)
                wallet.agw_address = got_agw
            if approved:
                break
        if not approved:
            # если после входа выяснилось, что AGW пуст — это пропуск, а не сбой
            if wallet.agw_address:
                bal = self._agw_balance(wallet.agw_address, proxy)
                if bal < self.cfg.amounts.min_out:
                    logger.skip("мост не подтверждён: AGW пуст — мостить нечего",
                                wallet=wallet.address, step="BRIDGE")
                    return False
            raise RetryableError(f"мост не подтверждён за {len(candidates)} попыток (Approve не прожат)")

        # Источник истины — зачисление на Base (RPC). Ждём прирост от baseline.
        expect = max(1, (agw_bal or 0) // 2)  # relayer удерживает комиссию -> ждём хотя бы половину
        new_bal = base.watch_balance_increase(
            baseline_wei=baseline, min_increase_wei=expect,
            timeout_sec=self.cfg.execution.status_timeout_sec,
            poll_interval_sec=self.cfg.execution.status_poll_interval_sec,
        )
        self.dao.update_job(job.id, status="BRIDGED", amount_out=str(new_bal - baseline))
        logger.ok(f"мост исполнен: +{(new_bal - baseline) / 1e18:.6f} ETH на {wallet.address}",
                  wallet=wallet.address, step="BRIDGE")
        return True

    def _forward_step(self, wallet: Wallet, job: Job, base: BaseClient) -> bool:
        """Отправка на биржевой адрес. True = DONE; False = ждём адрес (WAITING_TARGET)."""
        target = wallet.target_address
        if not target or not Web3.is_address(target):
            self.dao.update_job(job.id, status=WAITING_TARGET, last_error="нет биржевого адреса в XLSX")
            logger.warn("биржевой адрес не задан — кошелёк в режиме ожидания (поллинг XLSX)",
                        wallet=wallet.address, step="TRANSFER")
            return False
        st.forward_transfer(self._forward_ctx(wallet, base), job)
        return True

    # ---------------------------------------------------------------- обработка кошелька

    def _process_wallet(self, wallet: Wallet, dry_run: bool) -> str:
        """Возвращает исход: done | waiting | skipped | failed."""
        job = self.dao.ensure_job(wallet.id, NATIVE_TOKEN, "ETH")
        if job.status == "DONE":
            logger.skip("уже выполнено (DONE)", wallet=wallet.address, step="BRIDGE")
            return "done"
        if job.status == "FAILED":
            logger.warn("job FAILED — используйте retry для повтора", wallet=wallet.address, step="BRIDGE")
            return "failed"

        proxy = None
        if self.cfg.proxy.enabled:
            try:
                proxy = self.proxy_pool.assign(wallet.id, wallet.proxy, wallet.proxy)
            except Exception as e:  # noqa: BLE001 — мост важнее health-check: пробуем свою прокси как есть
                logger.warn(f"прокси: {e} — работаем без назначения", wallet=wallet.address)
                proxy = normalize_proxy(wallet.proxy)

        try:
            base = self._make_base(wallet, proxy)
            bridged = job.status in ("BRIDGED", "TRANSFERRED", WAITING_TARGET)
            if not bridged:
                bridged = self._bridge_step(wallet, job, base, proxy, dry_run)
                if not bridged:
                    return "skipped"
            job = self.dao.get_job(wallet.id, NATIVE_TOKEN)  # refresh
            assert job is not None
            done = self._forward_step(wallet, job, base)
            return "done" if done else "waiting"
        except (RetryableError, ManualError) as e:
            self.dao.update_job(job.id, status="FAILED", last_error=str(e)[:500], error_class="retryable")
            logger.error(f"мост/отправка не удались: {e}", wallet=wallet.address, step="BRIDGE")
            return "failed"
        except PermanentError as e:
            self.dao.update_job(job.id, status="FAILED", last_error=str(e)[:500], error_class="permanent")
            logger.error(str(e), wallet=wallet.address, step="BRIDGE")
            return "failed"
        except Exception as e:  # noqa: BLE001 — изоляция кошельков
            self.dao.update_job(job.id, status="FAILED", last_error=str(e)[:500], error_class="retryable")
            logger.error(f"непредвиденная ошибка: {e}", wallet=wallet.address, step="BRIDGE")
            return "failed"

    # ---------------------------------------------------------------- режим ожидания биржевых адресов

    def _poll_waiting_targets(self, waiting: list[Wallet]) -> None:
        """Средства на Base, адреса в XLSX нет: перечитываем файл каждые N сек, появился — отправляем.
        Ctrl+C безопасен: при следующем запуске WAITING_TARGET-кошельки продолжат с этого места."""
        xlsx = self.cfg.resolve(self.cfg.paths.wallets_xlsx)
        interval = max(5, self.cfg.execution.target_poll_interval_sec)
        pending = {w.address for w in waiting}
        logger.info(f"режим ожидания: {len(pending)} кошельк(ов) ждут биржевой адрес; "
                    f"проверяю {xlsx.name} каждые {interval}с (Ctrl+C — выйти, прогресс сохранён)")
        while pending:
            time.sleep(interval)
            try:
                targets = {w.address: w.target_address for w in excel.read_wallets(xlsx)}
            except Exception as e:  # noqa: BLE001 — файл открыт/битый -> попробуем в следующий раз
                logger.warn(f"XLSX недоступен: {str(e)[:60]} — повторю через {interval}с")
                continue
            for addr in sorted(pending):
                target = targets.get(addr)
                if not target or not Web3.is_address(target):
                    continue
                logger.ok(f"биржевой адрес появился в XLSX: {target}", wallet=addr, step="TRANSFER")
                wallet = self.dao.get_wallet(addr)
                if not wallet:
                    pending.discard(addr)
                    continue
                self.dao.set_wallet_target(wallet.id, Web3.to_checksum_address(target))
                wallet.target_address = Web3.to_checksum_address(target)
                job = self.dao.get_job(wallet.id, NATIVE_TOKEN)
                try:
                    base = self._make_base(wallet, normalize_proxy(wallet.proxy))
                    st.forward_transfer(self._forward_ctx(wallet, base), job)
                    pending.discard(addr)
                except Exception as e:  # noqa: BLE001 — не роняем поллинг из-за одного кошелька
                    logger.error(f"отправка не удалась: {e} (повторю на следующем цикле)",
                                 wallet=addr, step="TRANSFER")

    # ---------------------------------------------------------------- запуск

    def run(self, only_wallet: str | None = None, dry_run: bool = False) -> None:
        wallets = self.dao.get_wallets(enabled_only=True)
        if only_wallet:
            wallets = [w for w in wallets if w.address.lower() == only_wallet.lower()]
        wallets = [w for w in wallets if w.address in self.keys]
        if not wallets:
            logger.warn("нет кошельков (sync?)")
            return

        logger.info(f"браузерный мост AGW->Base->биржа: {len(wallets)} кошельк(ов), "
                    f"только native ETH, последовательно, dry_run={dry_run}")
        outcomes: dict[str, list[Wallet]] = {"done": [], "waiting": [], "skipped": [], "failed": []}
        for i, w in enumerate(wallets):
            outcome = self._process_wallet(w, dry_run)
            outcomes[outcome].append(w)
            if i + 1 < len(wallets):
                time.sleep(self.cfg.execution.random_delay())

        logger.ok(f"итог: отправлено {len(outcomes['done'])}, ждут адрес {len(outcomes['waiting'])}, "
                  f"пусто {len(outcomes['skipped'])}, ошибок {len(outcomes['failed'])}")

        # Вариант Б из ТЗ: средства пришли, адреса нет -> не завершаемся, ждём адрес из XLSX.
        if outcomes["waiting"] and not dry_run:
            try:
                self._poll_waiting_targets(outcomes["waiting"])
                logger.ok("все ожидавшие кошельки отправлены на биржу")
            except KeyboardInterrupt:
                logger.warn("ожидание прервано (Ctrl+C) — при следующем запуске продолжится с WAITING_TARGET")
