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
from dataclasses import dataclass

from playwright.sync_api import Page, BrowserContext, TimeoutError as PWTimeout

from src import logger

RELAY_BRIDGE_URL = "https://relay.link/bridge/abstract?fromChainId=2741&toChainId=8453"
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


def login(page: Page, context: BrowserContext, timeout_ms: int = 60000) -> LoginResult:
    """Полный вход как AGW. Наш инжект-провайдер должен быть уже установлен в контексте."""
    page.goto(RELAY_BRIDGE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    # уже подключены?
    agw = _read_connected_agw(page)
    if agw:
        logger.info(f"уже подключён AGW {agw[:10]}", step="BROWSER")
        return LoginResult(agw, True)

    # Connect
    try:
        page.get_by_role("button", name="Connect", exact=False).first.click(timeout=8000)
    except Exception:
        page.get_by_text("Connect", exact=False).first.click(timeout=8000)
    page.wait_for_timeout(3000)

    # выбрать Abstract и дождаться попапа Privy; UI флейков -> ретраим клик, пока попап не откроется
    modal = page.locator("[data-testid='dynamic-modal']")
    try:
        modal.get_by_placeholder("Search through", exact=False).fill("Abstract", timeout=6000)
        page.wait_for_timeout(2000)
    except Exception:
        pass

    privy = None
    for attempt in range(4):
        clicked = False
        for loc in [
            page.get_by_text("Abstract", exact=True),          # .last = строка в модалке (проверено в PoC)
            modal.get_by_text("Abstract", exact=True),
        ]:
            try:
                loc.last.click(timeout=5000)
                clicked = True
                break
            except Exception:
                continue
        logger.info(f"Abstract клик попытка {attempt + 1}: {'ok' if clicked else 'fail'}", step="BROWSER")
        privy = _wait_for_privy_popup(context, 12000)
        if privy:
            break
        page.wait_for_timeout(1500)
    if not privy:
        logger.warn("Privy popup не открылся", step="BROWSER")
        return LoginResult(None, False)
    logger.info(f"Privy popup: {privy.url[:60]}", step="BROWSER")

    # драйвим попап тем же приоритетом кнопок, что и в проверенном PoC:
    # Continue with a wallet -> MetaMask -> (наш SIWE) -> Approve. Каждую итерацию просто
    # кликаем первую доступную кнопку из списка (Approve появится на экране согласия).
    for _ in range(10):
        if privy.is_closed():
            break
        privy.wait_for_timeout(2500)
        for name in ["Continue with a wallet", "MetaMask", "Approve", "Continue", "Sign", "Confirm"]:
            try:
                btn = privy.get_by_role("button", name=name, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=3000)
                    logger.info(f"popup: клик '{name}'", step="BROWSER")
                    if name == "Approve":
                        privy.wait_for_timeout(1500)
                    break
            except Exception:
                pass

    page.wait_for_timeout(4000)
    agw = _read_connected_agw(page)
    return LoginResult(agw, bool(agw))


def _wait_for_privy_popup(context: BrowserContext, timeout_ms: int):
    import time
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
                return page.evaluate("(a)=>a", m.group(1))
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
