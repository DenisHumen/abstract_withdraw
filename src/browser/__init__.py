"""FALLBACK-ветка: AdsPower + Playwright (PLAN.md §7).

НЕ реализована в текущей версии: прямой EOA-путь покрывает основной сценарий.
Активируется в будущем, если средства окажутся в AGW-смарт-кошельке
(признак: пустой баланс EOA на Abstract при ожидаемых средствах -> статус NEEDS_BROWSER).

Контракт будущей реализации:
  adspower.py  — GET {ADSPOWER_BASE_URL}/api/v1/browser/start?user_id=<profile>
                 -> CDP ws-endpoint -> playwright.chromium.connect_over_cdp()
  relay_ui.py  — сценарий на relay.link/bridge: Abstract->Base, max, подтверждение в кошельке.
"""
