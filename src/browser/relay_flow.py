"""Playwright-автоматизация relay.link для AGW (вход одним ключом + мост Abstract->Base).

Поток:
  1) login(): Connect -> Abstract -> Privy popup -> 'Continue with a wallet' -> MetaMask
     -> SIWE (наш инжект-провайдер подписывает) -> Approve. Возвращает подключённый AGW-адрес.
  2) bridge_native_eth(): выставить ETH@Abstract -> ETH@Base, recipient = наш Base-EOA, MAX,
     нажать Bridge и подтвердить в Privy popup. При dry_confirm=True останавливается ДО подтверждения.

UI relay.link рендерится в shadow DOM (Dynamic) — Playwright-локаторы пронзают open shadow root.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from playwright.sync_api import Page, BrowserContext, TimeoutError as PWTimeout

from src import logger

RELAY_BRIDGE_URL = "https://relay.link/bridge/abstract?fromChainId=2741&toChainId=8453"
# Privy cross-app popup первый раз грузится ДОЛГО (со слов пользователя — прилично времени).
# Ждём и появления окна, и прорисовки его содержимого щедро — до 2 минут на каждый этап.
POPUP_APPEAR_SEC = 120   # сколько ждём, пока окно Privy вообще откроется (ретраим клик Abstract)
POPUP_LOAD_SEC = 120     # сколько ждём прорисовки содержимого попапа (кнопки 'Continue with a wallet' и т.д.)
# Тексты ошибок авторизации в Privy-попапе: если высветились — ждать бесполезно,
# сразу проваливаем попытку (ensure_agw ротирует прокси и пробует заново).
PRIVY_FAIL_TEXTS = ("Could not log in with wallet", "Unable to connect wallet")
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")


@dataclass
class LoginResult:
    agw_address: str | None
    ok: bool


def _click_first(page: Page, names: list[str], timeout: int = 4000) -> str | None:
    for name in names:
        try:
            btn = page.get_by_role("button", name=name, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=timeout)
                return name
        except Exception:
            continue
    return None


def login(page: Page, context: BrowserContext) -> LoginResult:
    """Полный вход как AGW. Наш инжект-провайдер должен быть уже установлен в контексте."""
    page.goto(RELAY_BRIDGE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    # уже подключены?
    agw = _read_connected_agw(page)
    if agw:
        logger.info(f"уже подключён AGW {agw[:10]}", step="BROWSER")
        return LoginResult(agw, True)

    # Жмём Connect, ПОКА не откроется модалка выбора кошелька. Критерий успеха — появление
    # поля поиска модалки (testid 'dynamic-auth-modal'), а НЕ «клик не бросил исключение»:
    # сам клик флейкует (анимация/оверлеи/чат-виджет Intercom перехватывает pointer). Поэтому
    # каждую секунду перепроверяем модалку и при её отсутствии снова жмём любую connect-кнопку.
    # Кнопки: testid 'widget-connect-wallet-button' (фиолетовая CONNECT WALLET в виджете),
    # затем 'Connect Wallet'/'Connect' (верхняя). Клик — обычный, при неудаче force (поверх оверлея).
    auth = page.get_by_test_id("dynamic-auth-modal")
    search = auth.get_by_placeholder("Search through", exact=False)

    def modal_open() -> bool:
        try:
            return search.count() > 0
        except Exception:  # noqa: BLE001
            return False

    def click_connect() -> bool:
        # прячем чат-виджет Intercom, если он перехватывает клики
        try:
            page.evaluate("() => { const f=document.querySelector('iframe[name^=intercom]');"
                          " if (f) f.style.pointerEvents='none'; }")
        except Exception:  # noqa: BLE001
            pass
        for getter in (
            lambda: page.get_by_test_id("widget-connect-wallet-button"),
            lambda: page.get_by_role("button", name="Connect Wallet", exact=False),
            lambda: page.get_by_role("button", name="Connect", exact=False),
        ):
            try:
                b = getter()
                if b.count() == 0:
                    continue
                try:
                    b.first.click(timeout=2500)
                except Exception:  # noqa: BLE001 — оверлей/анимация -> пробуем force
                    b.first.click(timeout=2500, force=True)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    populated = False
    for i in range(90):  # до ~90с: медленный рендер через прокси + флейковый клик
        agw = _read_connected_agw(page)
        if agw:
            return LoginResult(agw, True)
        if modal_open():
            populated = True
            break
        clicked = click_connect()
        if i % 5 == 0:
            logger.info(f"жду модалку кошельков (Connect {'нажат' if clicked else 'не найден'}, {i}с)",
                        step="BROWSER")
        page.wait_for_timeout(1000)
    if not populated:
        logger.warn("модалка кошельков не открылась за ~90с (Connect не сработал) — ротация прокси",
                    step="BROWSER")
        return LoginResult(None, False)
    logger.info("модалка кошельков загрузилась", step="BROWSER")
    # фильтруем список до одного кошелька Abstract, чтобы клик попадал точно в строку AGW
    try:
        search.first.fill("Abstract", timeout=6000)
        page.wait_for_timeout(1500)
    except Exception:
        pass

    # Клик строки Abstract флейкует (shadow DOM Dynamic) -> ретраим, пока не откроется Privy popup.
    # Строку берём внутри auth-модалки по точному тексту 'Abstract' (проверено: get_by_text exact).
    # Первая прогрузка окна Privy бывает долгой -> ждём его появления суммарно до POPUP_APPEAR_SEC (~2 мин).
    privy = None
    appear_deadline = time.time() + POPUP_APPEAR_SEC
    attempt = 0
    while time.time() < appear_deadline and not privy:
        attempt += 1
        clicked = False
        for loc in [
            auth.get_by_text("Abstract", exact=True),
            auth.get_by_test_id("wallet-icon-abstract"),
            page.get_by_text("Abstract", exact=True),
        ]:
            try:
                if loc.count() > 0:
                    loc.first.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue
        logger.info(f"Abstract клик попытка {attempt}: {'ok' if clicked else 'fail'}", step="BROWSER")
        # окно Privy обычно открывается за ~2с, но первая прогрузка/флейк -> ждём щедро (до 20с на попытку)
        privy = _wait_for_privy_popup(context, 20000)
        if privy:
            break
        page.wait_for_timeout(1500)
    if not privy:
        logger.warn("Privy popup не открылся (ждали до 2 мин)", step="BROWSER")
        return LoginResult(None, False)
    logger.info(f"Privy popup: {privy.url[:60]}", step="BROWSER")

    # Экран Privy 'Log in to Abstract' -> 'Continue with a wallet' -> MetaMask (наш инжект) ->
    # SIWE (провайдер подписывает) -> Approve/Continue. Пункты могут быть НЕ button-role (строки-иконки),
    # поэтому пробуем и role=button, и просто текст. СОДЕРЖИМОЕ попапа первый раз прорисовывается долго
    # -> крутим цикл до POPUP_LOAD_SEC (~2 мин), пока кнопки не появятся и не прожмём Approve.
    # КРИТИЧНО: после клика 'Approve' попап ЗАКРЫВАЕТСЯ (=успех, кросс-апп подключение прошло).
    # Любой последующий вызов на закрытом попапе кидает TargetClosedError -> НЕЛЬЗЯ давать ему
    # уронить login (иначе успешный вход рапортуется как провал). Всё взаимодействие с попапом —
    # в try/except; закрытие попапа = штатное завершение.
    approved = False
    drive_deadline = time.time() + POPUP_LOAD_SEC
    while time.time() < drive_deadline:
        try:
            if privy.is_closed():
                break
            privy.wait_for_timeout(2500)
        except Exception:
            break  # попап закрылся — подтверждение ушло
        # Ошибка авторизации в попапе -> fail-fast: не досиживаем таймауты, сразу ротация прокси.
        fail = _detect_privy_fail(privy)
        if fail:
            logger.warn(f"popup: «{fail}» — вход не удался, сразу ротация прокси", step="BROWSER")
            return LoginResult(None, False)
        for name in ["Continue with a wallet", "MetaMask", "Approve", "Continue", "Sign", "Confirm"]:
            clicked = False
            for loc in [
                privy.get_by_role("button", name=name, exact=False),
                privy.get_by_text(name, exact=False),
            ]:
                try:
                    if loc.count() > 0:
                        loc.first.click(timeout=3000)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                logger.info(f"popup: клик '{name}'", step="BROWSER")
                if name == "Approve":
                    approved = True
                    try:
                        privy.wait_for_timeout(1500)
                    except Exception:
                        pass  # попап закрылся сразу после Approve — это норма
                break
        if approved and privy.is_closed():
            break

    # Подключение завершается асинхронно после закрытия попапа -> читаем AGW терпеливо (до ~20с).
    agw = None
    for _ in range(20):
        agw = _read_connected_agw(page)
        if agw:
            break
        page.wait_for_timeout(1000)
    return LoginResult(agw, bool(agw))


def _detect_privy_fail(privy: Page) -> str | None:
    """Вернуть текст ошибки авторизации, если он высветился в попапе (иначе None).
    Закрывшийся попап — не ошибка (обрабатывается выше по is_closed)."""
    for t in PRIVY_FAIL_TEXTS:
        try:
            if privy.get_by_text(t, exact=False).count() > 0:
                return t
        except Exception:  # noqa: BLE001 — попап закрылся между проверками
            return None
    return None


def _wait_for_privy_popup(context: BrowserContext, timeout_ms: int):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for pg in list(context.pages):
            if "privy" in pg.url:
                return pg
        time.sleep(0.5)
    return None


def _read_connected_agw(page: Page) -> str | None:
    """Достаём подключённый AGW-адрес из wagmi store (relay.link использует Dynamic->wagmi)."""
    try:
        raw = page.evaluate("() => localStorage.getItem('wagmi.store')")
        if raw:
            m = re.search(r'"accounts":\s*\[\s*"(0x[a-fA-F0-9]{40})"', raw)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def bridge_native_eth(
    page: Page,
    context: BrowserContext,
    recipient: str,
    dry_confirm: bool = True,
    shots_dir=None,
) -> dict:
    """Выставить мост ETH Abstract->Base на recipient, MAX. dry_confirm=True — не подтверждать.

    Возвращает {'filled': bool, 'confirmed': bool, 'note': str}.
    Селекторы UI могут требовать подстройки — шаги залогированы + скрины.
    """
    def shot(name):
        if shots_dir:
            try:
                page.screenshot(path=str(shots_dir / name))
            except Exception:
                pass

    note = []
    page.wait_for_timeout(2000)
    shot("b01_form.png")

    # 1) Buy-токен = ETH на BASE. По умолчанию Buy тоже ETH@Abstract (тот же чейн -> "Invalid recipient").
    #    Кликаем селектор токена в панели Buy и выбираем сеть Base + ETH.
    set_base = _set_buy_base_eth(page)
    note.append(f"buy_base={'ok' if set_base else 'fail'}")
    page.wait_for_timeout(1500)
    shot("b02_buy_base.png")

    # 2) Сумма: MAX (по продаваемому ETH@Abstract)
    max_clicked = _click_first(page, ["MAX"], timeout=4000) or _click_by_text(page, "MAX")
    note.append(f"max={'ok' if max_clicked else 'fail'}")
    page.wait_for_timeout(1500)
    shot("b03_max.png")

    # 3) Recipient: адрес в панели Buy (наш Base-EOA). Обязателен: sender(AGW) != recipient.
    set_rcpt = _set_recipient(page, recipient)
    note.append(f"recipient={'ok' if set_rcpt else 'fail'}")
    page.wait_for_timeout(1200)
    shot("b04_recipient.png")

    if dry_confirm:
        note.append("dry: остановка до подтверждения")
        return {"filled": bool(max_clicked), "confirmed": False, "note": "; ".join(note)}

    # Нажать ГЛАВНУЮ кнопку действия. ВНИМАНИЕ: у стрелки смены направления accessible name
    # тоже 'Swap' -> нельзя матчить по name='Swap' (перевернёт маршрут!). Главная кнопка —
    # видимый текст РОВНО 'SWAP' (верхний регистр) внизу виджета.
    # accessible name главной кнопки = 'Swap' (визуально SWAP через CSS uppercase). У стрелки
    # смены направления name тоже 'Swap' и она ПЕРВАЯ в DOM -> берём ПОСЛЕДНЮЮ (главная внизу).
    pressed = None
    for getter in [
        lambda: page.get_by_role("button", name="Swap", exact=True),
        lambda: page.get_by_role("button", name=re.compile(r"^(Bridge|Review|Confirm swap)$", re.I)),
    ]:
        try:
            loc = getter()
            n = loc.count()
            if n > 0:
                loc.nth(n - 1).click(timeout=6000)  # последняя = главная кнопка действия
                pressed = f"SWAP(of {n})"
                break
        except Exception:
            continue
    note.append(f"submit={pressed or 'fail'}")
    page.wait_for_timeout(2500)
    shot("b05_after_swap.png")

    # Иногда relay.link показывает in-page 'Confirm' перед попапом Privy
    _click_first(page, ["Confirm swap", "Confirm bridge", "Confirm"], timeout=4000)
    page.wait_for_timeout(2000)

    # Подтвердить транзакцию в Privy popup (AGW подписывает встроенным ключом).
    # КРИТИЧНО: не крашиться при закрытии попапа и НЕ закрывать браузер сразу после Approve —
    # Privy подписывает/шлёт tx клиентски, нужно время. Ждём инкремент nonce снаружи (в раннере).
    privy = _wait_for_privy_popup(context, 30000)
    approved = False
    if privy:
        for i in range(20):
            try:
                if privy.is_closed():
                    break
                if shots_dir:
                    try:
                        privy.screenshot(path=str(shots_dir / f"b06_privy_tx{i}.png"))
                    except Exception:
                        pass
                if not approved:
                    if _click_first(privy, ["Approve", "Confirm", "Sign", "Send"], timeout=2500):
                        approved = True
                        logger.info("popup: подтверждение отправлено", step="BROWSER")
                privy.wait_for_timeout(2000)
            except Exception:
                break  # попап закрылся -> подтверждение ушло
    note.append(f"approved={approved}")
    # держим страницу открытой, чтобы Privy успел подписать и отправить deposit-tx
    page.wait_for_timeout(12000)
    shot("b07_final.png")
    return {"filled": bool(max_clicked), "approved": approved, "note": "; ".join(note)}


def _click_by_text(page: Page, text: str) -> bool:
    try:
        page.get_by_text(text, exact=True).last.click(timeout=3000)
        return True
    except Exception:
        return False


def _set_buy_base_eth(page: Page) -> bool:
    """Открыть селектор Buy-токена и выбрать сеть Base + ETH.

    UI relay.link: в панели Buy строка токена ('ETH / Abstract' со стрелкой). Открывает модалку
    выбора токена/сети; нужно выбрать Base и ETH. Селекторы требуют живой валидации.
    """
    try:
        # второй по счёту селектор токена на странице — это Buy (первый — Sell)
        page.get_by_text("Abstract", exact=False).nth(1).click(timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:
        return False
    # выбрать сеть Base (фильтр по названию сети), затем ETH
    for step in (["Base"], ["ETH", "Ether"]):
        done = False
        for name in step:
            try:
                el = page.get_by_text(name, exact=True)
                if el.count() > 0:
                    el.first.click(timeout=4000)
                    page.wait_for_timeout(1000)
                    done = True
                    break
            except Exception:
                continue
        if not done:
            return False
    return True


def _set_recipient(page: Page, recipient: str) -> bool:
    """Задать получателя в панели Buy (по скринам пользователя):
    дропдаун адреса в Buy -> 'Paste wallet address' -> поле 'Address or ENS' -> Save.
    Так НЕ нужно подключать второй кошелёк — просто вставляем наш Base-EOA.
    """
    # 1+2) открыть дропдаун адреса и нажать 'Paste wallet address'.
    # Усечение relay.link — через юникод-эллипсис (…), поэтому [.…]. Дропдаунов несколько
    # (панель кошелька, Sell, Buy) — пробуем каждый, пока не появится пункт 'Paste wallet address'.
    dd = page.get_by_text(re.compile(r"0x[0-9a-fA-F]{2,8}[.…]{1,3}[0-9a-fA-F]{4}"))
    n = dd.count()
    pasted = False
    for idx in range(n - 1, -1, -1):  # Buy-дропдаун обычно ниже -> идём с конца
        try:
            dd.nth(idx).click(timeout=2500)
            page.wait_for_timeout(700)
            paste = page.get_by_text("Paste wallet address", exact=False)
            if paste.count() > 0:
                paste.first.click(timeout=3000)
                pasted = True
                break
            # закрыть меню, если открылось не то
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            continue
    if not pasted:
        return False
    page.wait_for_timeout(800)

    # 3) ввести адрес в 'Address or ENS'
    filled = False
    for ph in ["Address or ENS", "Address", "Enter address"]:
        try:
            inp = page.get_by_placeholder(ph, exact=False)
            if inp.count() > 0:
                inp.first.fill(recipient, timeout=3000)
                filled = True
                break
        except Exception:
            continue
    if not filled:
        return False
    page.wait_for_timeout(600)

    # 4) Save
    try:
        page.get_by_role("button", name="Save", exact=False).first.click(timeout=4000)
    except Exception:
        _click_by_text(page, "Save")
    page.wait_for_timeout(800)
    return True
