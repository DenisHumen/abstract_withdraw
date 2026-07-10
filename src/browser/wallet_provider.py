"""Инжектируемый EIP-1193 провайдер, подписывающий ОДНИМ приватным ключом (без расширения).

Используется для входа на relay.link через Privy cross-app: наш window.ethereum отвечает
на eth_requestAccounts/eth_chainId локально, а personal_sign / eth_signTypedData проксирует
в Python-подписант (ключ живёт только в процессе). Сам bridge подписывает встроенный ключ Privy
в попапе — наш провайдер для этого НЕ нужен.

Критично: hexbytes v1 .hex() возвращает БЕЗ 0x — Privy отвергает подпись без префикса.
"""
from __future__ import annotations

import json

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

# JS выполняется в КАЖДОЙ странице контекста (в т.ч. в попапе privy.abs.xyz).
# %ADDR% подставляется адресом EOA.
INJECT_JS = r"""
(() => {
  const ADDRESS = "%ADDR%";
  const CHAIN_ID = "0x1";
  const emitter = {};
  const on = (e, cb) => { (emitter[e] ||= []).push(cb); };
  const provider = {
    isMetaMask: true, isConnected: () => true,
    chainId: CHAIN_ID, networkVersion: "1", selectedAddress: ADDRESS,
    request: async ({ method, params }) => {
      if (method === 'eth_requestAccounts' || method === 'eth_accounts') return [ADDRESS];
      if (method === 'eth_chainId') return CHAIN_ID;
      if (method === 'net_version') return "1";
      if (method === 'wallet_switchEthereumChain' || method === 'wallet_addEthereumChain') return null;
      if (method === 'wallet_requestPermissions') return [{ parentCapability: 'eth_accounts' }];
      if (method === 'wallet_getPermissions') return [{ parentCapability: 'eth_accounts' }];
      return await window.__walletSign(JSON.stringify({ method, params: params || [] }));
    },
    on, addListener: on, removeListener: () => {}, removeAllListeners: () => {},
    enable: async () => [ADDRESS],
    send: (m, p) => provider.request({ method: typeof m === 'string' ? m : m.method, params: p || (m && m.params) }),
    sendAsync: (payload, cb) => provider.request(payload)
      .then(r => cb(null, { id: payload.id, jsonrpc: '2.0', result: r })).catch(e => cb(e)),
  };
  try { Object.defineProperty(window, 'ethereum', { value: provider, configurable: true, writable: true }); }
  catch (e) { window.ethereum = provider; }
  const info = { uuid: (crypto.randomUUID && crypto.randomUUID()) || '11111111-1111-1111-1111-111111111111',
                 name: 'MetaMask',
                 icon: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=',
                 rdns: 'io.metamask' };
  const announce = () => window.dispatchEvent(new CustomEvent('eip6963:announceProvider',
    { detail: Object.freeze({ info, provider }) }));
  window.addEventListener('eip6963:requestProvider', announce);
  announce();
  try { Object.defineProperty(navigator, 'webdriver', { get: () => false }); } catch (e) {}
})();
"""


def _hex0x(v) -> str:
    h = v if isinstance(v, str) else v.hex()
    return h if h.startswith("0x") else "0x" + h


def make_injector(private_key: str):
    """Возвращает (inject_js, signer_callback) для Playwright add_init_script/expose_binding."""
    account = Account.from_key(private_key)
    inject_js = INJECT_JS.replace("%ADDR%", account.address)

    def signer(arg_json: str) -> str:
        arg = json.loads(arg_json)
        method = arg["method"]
        params = arg.get("params") or []
        if method in ("personal_sign", "eth_sign"):
            msg = params[0] if method == "personal_sign" else params[1]
            signable = encode_defunct(hexstr=msg) if isinstance(msg, str) and msg.startswith("0x") \
                else encode_defunct(text=msg)
            return _hex0x(account.sign_message(signable).signature)
        if method in ("eth_signTypedData_v4", "eth_signTypedData", "eth_signTypedData_v3"):
            data = params[1] if len(params) > 1 else params[0]
            if isinstance(data, str):
                data = json.loads(data)
            return _hex0x(account.sign_message(encode_typed_data(full_message=data)).signature)
        raise Exception(f"injected wallet: unsupported method {method}")

    return account.address, inject_js, signer
