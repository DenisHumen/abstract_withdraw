"""Браузерная ветка вывода из AGW-кошельков (Abstract Global Wallet) одним приватным ключом.

Зачем: если средства лежат в AGW (смарт-кошелёк Privy), их он-чейн подписант — встроенный
Privy-ключ, а не наш MetaMask-ключ. Прямой RPC/agw-client НЕ подходит. Но relay.link + Privy
cross-app позволяют войти MetaMask-ключом (SIWE), после чего Privy подписывает мост сам.

Модули:
  wallet_provider.py — инжектируемый EIP-1193 провайдер, подписывающий одним ключом (без расширения).
  relay_flow.py      — Playwright-автоматизация relay.link.

Статус (2026-07-12):
  ✅ login() — ПРОВЕРЕНО вживую: вход как AGW 0xF094..a671 одним ключом (Abstract->Privy->
     Continue with a wallet->MetaMask->SIWE->Approve). Крит. фикс: 0x-префикс подписи (hexbytes v1).
  ⚠️ bridge_native_eth() — шаги известны из UI (Buy->Base, recipient!=sender, MAX, Confirm),
     но селекторы формы требуют живой валидации; реальный вывод НЕ запускать без подтверждения.

Важно: MetaMask-логин детерминированно даёт AGW, соответствующий этому ключу (0xF094..a671),
а не произвольный адрес. Финальный transfer на Base — обычной программной tx (MetaMask EOA).
"""
