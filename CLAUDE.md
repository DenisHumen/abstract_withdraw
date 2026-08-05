# CLAUDE.md — контекст проекта для ИИ-агентов

## Что это

Скрипт вывода активов: Abstract (2741) → relay.link → native ETH на Base (8453) → transfer на target-адрес.
Python 3.11+/3.12, стек: web3.py v7, httpx, openpyxl, sqlite3, typer+rich, tenacity.
Полная архитектура: [PLAN.md](PLAN.md). Пользовательская дока: [README.md](README.md).

## Жёсткие требования пользователя (НЕ нарушать)

1. **Никаких платных/ключевых API** (Etherscan, abscan, Relay API-key и т.п.). Только:
   публичный REST `https://api.relay.link`, публичные RPC, ончейн-вызовы (Multicall3).
   Единственный допустимый ключ — `ADSPOWER_API_KEY` (браузерный fallback).
2. **XLSX — источник истины для набора кошельков; SQLite (`data/state.db`) — источник истины по прогрессу.**
   При sync БД приводится к XLSX: убрали строку → `delete_wallets_not_in` удаляет кошелёк + каскад
   (jobs/token_balances/tx_log); добавили → upsert. Защита: пустой/битый XLSX не удаляет ничего.
3. **`target_address` опционален.** Пусто → задачи в статусе `WAITING_TARGET`, **ончейн-действий нет**;
   как появится адрес в XLSX — задачи возобновляются (гейт в `pipeline._run_wallet_jobs`). См. `forward_transfer`
   тоже защищён. Никогда не бриджить/слать без валидного target.
4. **Идемпотентность**: никакой шаг не должен выполняться повторно (см. `tx_log`, `request_id`).
4. **Прокси на кошелёк** формата `login:passwd@ip:port`; при сбое — ротация на случайную из
   `data/proxies.txt`.
5. Приватные ключи — только в памяти, в БД/логи не писать. Прокси-креды в логах маскировать.
6. Держать PLAN.md/README/CLAUDE.md актуальными при изменениях.
7. Отчётность по задачам вести в `reports/worklog.xlsx`
   (колонки: Дата, День недели, Направление, Задача, Часы (оц.)) — дополнять при новых работах.

## Карта кода

```
src/main.py            CLI: init-data|sync|discover|run|status|retry|check-protocols|report-protocols;
                       интерактивное меню (запуск без команды), цветной rich-вывод
src/config.py          .env (EnvSettings) + config.yaml (AppConfig); константы NATIVE_TOKEN, MULTICALL3
src/logger.py          rich + JSONL (logs/run-YYYYMMDD.jsonl)
src/db/                schema.sql, dao.py (thread-local sqlite, upsert-ы), models.py (статусы)
src/data_io/excel.py   чтение/шаблон wallets.xlsx, sync в БД (возвращает {addr: pk} в память)
src/net/proxy.py       ProxyPool: назначение sticky, health-check, ротация, маскирование
src/net/rpc.py         RpcPool: пул публичных нод, ротация при сбое, web3 через прокси
src/relay/client.py    quote/v2, intents/status/v3, currencies/v1, chains; post_signature
src/relay/types.py     pydantic-модели (extra=allow)
src/chains/evm.py      базовый клиент: EIP-1559 подпись/отправка/receipt, ERC20 ABI
src/chains/abstract.py AbstractClient (prio fee=0; fallback zksync2 type-113 — опц. пакет)
src/chains/base.py     BaseClient: transfer_native, watch_balance_increase (источник истины bridge)
src/chains/multicall.py batch balanceOf через Multicall3 (0xcA11...CA11)
src/chains/tokens.py   discovery: Relay currencies ⋂ Multicall3 (фильтр невалидных адресов!)
src/core/steps.py      QUOTE/APPROVE/DEPOSIT/BRIDGE/TRANSFER (идемпотентные)
src/core/pipeline.py   оркестратор EOA-пути: кошельки параллельно, изоляция ошибок, ротация прокси;
                       tokens.native_only=true -> ERC-20 не сканируем (ETH — единственная цель)
src/core/agw_bridge.py БРАУЗЕРНЫЙ мост (CLI bridge-agw): login+bridge_native_eth -> ETH AGW->Base
                       (recipient=свой EOA из ключа, 'Paste wallet address') -> watch Base ->
                       forward_transfer на биржевой target_address; нет адреса -> WAITING_TARGET
                       + поллинг XLSX (target_poll_interval_sec) до появления
src/core/protocol_check.py оркестратор чекера: 2 задачи/кошелёк (get_agw, check_protocols) в check_tasks;
                       адрес -> сразу DeBank; готовые AGW параллельно, входы последовательно
src/debank/checker.py  Playwright-перехват API DeBank (project_list/cache_balance_list/used_chains) -> протоколы
src/data_io/protocol_report.py экспорт отчёта по протоколам в Excel (листы protocols/summary/catalog)
src/core/errors.py     Retryable/Proxy/Rpc/Permanent/NoRoute/DustSkip/Manual
src/browser/           ЗАГЛУШКА fallback-ветки AdsPower+Playwright (PLAN.md §7)
```

## Проверенные факты (живые тесты 10.07.2026, не переоткрывать)

- Relay `/chains`: 72 сети, Abstract(2741) и Base(8453) присутствуют.
- Relay `POST /currencies/v1` `{chainIds:[2741], limit:N}`: **пагинации нет** (page/offset
  игнорируются), но limit до 5000+ работает; на Abstract всего **~1031 токен**, забираем одним вызовом.
  В списке встречаются **невалидные адреса** (`0x`) — фильтруются `Web3.is_address`.
  `verified:true` режет до ~55 токенов и не включает даже USDC — по умолчанию выключено.
- Native-квота Abstract→Base работает: один шаг `deposit` (kind=transaction),
  `to=0x4cd00e387622c35bddb9b4c962c136462338bc31`, gas ≈ 295k → `DEPOSIT_GAS_HINT=600k` с запасом.
- gasPrice Abstract ≈ 0.045 gwei, Base ≈ 0.006 gwei (динамично, оценивать по RPC).
- Скам-токены раздают фейковые балансы **любому** адресу (пустой кошелёк «держит» ~34 шт).
  Их квоты дают `NO_SWAP_ROUTES_FOUND` → статус `SKIPPED` (не FAILED, чтобы retry их не гонял).
- Etherscan-style recalc.py (skill xlsx) не работает на Windows (AF_UNIX) — формулы в отчёте
  считает сам Excel при открытии.

## Состояние (15.07.2026)

- [x] Полный прямой EOA-путь реализован и проверен dry-run на живых публичных API.
- [x] **Чекер протоколов: две задачи на кошелёк** (`check_tasks`: get_agw + check_protocols),
      «адрес -> сразу DeBank», скип входа при известном AGW; вход чинён (краш после Approve),
      Privy popup ждём до 2 мин. Покрыто детерминированными тестами (logic + parallel).
      Живой вход подтверждён 14-15.07 (AGW за 46с); массовый прогон упирается только в
      Cloudflare-рейтлимит при частых входах с одного IP (пейсинг login_delay_sec).
- [x] `WAITING_TARGET` (очередь без адреса) + XLSX-as-master (add/delete/target-fill) — реализовано,
      покрыто детерминированными тестами (6 проверок) и живым прогоном.
- [x] Реальный прогон на боевом кошельке `0x47Af..3148` через прокси: работает безупречно.
      **Диагностика: у этого EOA на Abstract 0 ETH, nonce 0, только скам-токены без маршрутов** —
      бриджить нечего. Для боевого теста самого бриджа нужен кошелёк с реальными средствами на Abstract
      + заполненный `target_address`.
- [x] **AGW-ветка (браузер, один ключ) — РАБОТАЕТ END-TO-END НА РЕАЛЬНЫХ СРЕДСТВАХ.**
      Тест-кошелёк оказался AGW. Целевой AGW = `0xF094BE0c..a671`, им управляет MetaMask-ключ
      (НЕ `0xdF6d..Ae43` — другой логин). Проверено вживую 2026-07-11: мост 0.003146 ETH
      Abstract->Base, funds landed на `0x47Af..3148` (AGW 0.003243->0.000076, Base +0.003146).
      Полная цепочка: login -> Buy=ETH@Base -> recipient=paste Base-EOA -> MAX -> SWAP ->
      Approve в Privy popup -> deposit-tx из AGW -> relayer -> Base.
- [x] **AGW-ветка интегрирована (17.07.2026): `bridge-agw` / меню п.3** — src/core/agw_bridge.py:
      только native ETH (tokens.native_only=true, сканирование ERC-20 отключено); recipient=свой EOA
      из ключа через 'Paste wallet address'; источник истины — зачисление на Base (RPC);
      затем forward на биржевой target_address; адреса нет -> WAITING_TARGET + поллинг XLSX
      каждые target_poll_interval_sec (Ctrl-C безопасен, ре-ран идемпотентен — проверено
      детерминированным тестом test_agw_bridge, 11 проверок). Живой прогон ещё не делался.
- [ ] Обобщить на ERC-20 (сейчас native ETH) и на несколько кошельков (профили/сессии).
- [ ] Возможное расширение функционала (пользователь упоминал будущие доработки).

## Чекер протоколов (DeBank) — как работает
- Адрес для чека = AGW/Privy-адрес из входа на relay.link (relay_flow.login -> сохраняем wallets.agw_address).
- DeBank API (api.debank.com) подписывается их JS -> прямой fetch НЕ работает ({} / 403). Решение:
  Playwright перехватывает ОТВЕТЫ (page.on('response')) на portfolio/project_list, token/cache_balance_list
  (все чейны; token/balance_list — по одному), user/used_chains. DeBank грузится в свежем Playwright без
  Cloudflare-блока. Проверено вживую на 0xF094..a671: протоколы Aborean Finance, Witty (chains op,abs).
- Хранение: check_tasks (две задачи/статусы на кошелёк) + protocols (каталог, дедуп по debank_id, РАСТЁТ
  при новых протоколах) + wallet_protocols (позиции кошелька, clear перед свежим чеком) + wallet_tokens_debank.
  Отчёт: reports/protocols_report.xlsx
  (в отчёте кошельки разделены: чередующаяся заливка групп + толстая линия между кошельками).

## Чекер: ДВЕ ЗАДАЧИ на кошелёк + правило параллелизма (важно, не ломать)
- В БД на КАЖДЫЙ кошелёк ровно ДВЕ задачи (таблица `check_tasks`, UNIQUE(wallet_id,task)):
  1) `get_agw` — получить Privy/AGW-адрес входом на relay.link; 2) `check_protocols` — DeBank по этому адресу.
  Статусы: PENDING|RUNNING|DONE|FAILED. Если `agw_address` уже есть -> get_agw сразу DONE (входа нет).
  Провал входа -> обе задачи FAILED (без AGW проверять нечего). Сводка печатается в конце `run()`.
- **Логика потока (ProtocolChecker.run): получили адрес -> СРАЗУ ЖЕ его DeBank.** Реализовано двумя группами:
  - **Группа 1 — кошельки с готовым AGW**: get_agw=DONE моментально; DeBank идёт ПАРАЛЛЕЛЬНО
    (`--threads`/`execution.check_concurrency`) — входов нет, headless-потоки безопасны (2+ DeBank ок).
  - **Группа 2 — нужен вход**: ПАРАЛЛЕЛЬНО в `--login-threads`/`execution.login_concurrency` потоков
    (по требованию пользователя, для скорости). Кошельки делятся round-robin на чанки; каждый воркер идёт
    по чанку последовательно `login -> сразу его debank -> следующий`; старты воркеров растянуты
    (_LOGIN_STAGGER_SEC=8с — не запускать все Chromium одновременно); `login_delay_sec` — пауза внутри потока.
- **Нагрузка — главный враг входа**: вход = headful Chromium (тяжёлый), и рендер relay.link душится
  машинной нагрузкой (проверено: 9 браузеров = модалка не грузится за 60с, 1 = за 2с). Раннее наблюдение
  «2 параллельных входа падают» было на СТАРОМ коде (без терпеливых таймаутов и с крашем после Approve);
  после фикса параллельность включена, но default умеренный (login_concurrency=2). Если входы массово
  падают на модалке (>60с) — снижать login_threads вплоть до 1 (прежний надёжный режим).
- sync_playwright() создаётся на КАЖДЫЙ вызов внутри _try_login/debank_check -> потокобезопасно
  (у каждого потока свой драйвер).
- Анти-троттлинг args обязательны (фон. окна Chrome душит рендер). Зомби-процессы Chromium от упавших
  прогонов копятся и грузят машину -> при отладке чистить: Stop-Process chrome.exe с CommandLine *ms-playwright*.
- Профиль браузера — СВЕЖИЙ на каждую попытку входа (data/.browser/<addr>_aN, удаляется в finally):
  флагнутые Cloudflare-куки не тянутся между попытками. Кэш повторных прогонов — agw_address в БД, не сессия.

## КРИТИЧНЫЕ уроки браузерной автоматизации relay.link (не переоткрывать)
- Кнопка SWAP: accessible name = 'Swap' (визуально SWAP через CSS uppercase), ТАКОЙ ЖЕ у стрелки
  смены направления -> брать `.last` (главная внизу DOM), НЕ `name='Swap'` первую (перевернёт маршрут!).
- Recipient: усечение адреса юникод-эллипсисом '…' (не '...'); флоу: дропдаун Buy -> 'Paste wallet
  address' -> 'Address or ENS' -> Save. Перебирать дропдауны до появления 'Paste wallet address'.
- Buy по умолчанию = ETH@Abstract (тот же чейн) -> надо сменить на Base, иначе 'Invalid recipient'.
- Confirm tx: попап 'Approve transaction' с кнопкой 'Approve'. НЕ крашиться при закрытии попапа и
  НЕ закрывать браузер сразу — Privy подписывает/шлёт клиентски (~10с). Ждать инкремент nonce AGW.
- Abstract-клик в модалке Dynamic флейкует (shadow DOM) -> ретраить, пока не откроется Privy popup.
- **ВХОД (login) — точные селекторы, проверено вживую 2026-07-14 (не гадать!):**
  - Connect: testid `widget-connect-wallet-button` (name='Connect' матчит 3 РАЗНЫЕ кнопки -> .first не та).
    КРИТИЧНО: критерий успеха клика — ПОЯВЛЕНИЕ auth-модалки, а НЕ «click не бросил исключение».
    Клик флейкует (анимация/оверлеи/чат Intercom перехватывает pointer) -> цикл жмёт Connect
    (testid -> 'Connect Wallet' -> 'Connect', с force-фолбэком, пряча Intercom-iframe) КАЖДУЮ секунду,
    пока модалка не откроется (до ~90с). Проверено 3/3 входа подряд (Connect с 0с). Раньше код считал
    успехом сам клик и мог «не нажать» (клик проглочен оверлеем) -> модалка не открывалась.
  - Список кошельков — в модалке testid `dynamic-auth-modal` (НЕ `dynamic-modal`!); поле поиска —
    placeholder `Search through N wallets...`. Строка Abstract: `dynamic-auth-modal` >> text=Abstract exact.
  - Privy popup всегда `https://privy.abs.xyz/cross-app/connect?...` (содержит 'privy' -> детект по url ок).
  - Экран попапа: 'Log in to Abstract' -> 'Continue with a wallet' -> MetaMask -> SIWE -> Approve.
  - **Fail-fast**: тексты 'Could not log in with wallet' / 'Unable to connect wallet' в попапе
    (PRIVY_FAIL_TEXTS) = ошибка авторизации -> НЕ досиживать таймауты, сразу LoginResult(None,False)
    -> ensure_agw ротирует прокси и пробует заново.
  - **ГЛАВНЫЙ БАГ (был): после 'Approve' попап ЗАКРЫВАЕТСЯ (это УСПЕХ). Любой вызов на закрытом попапе
    (`wait_for_timeout` и т.п.) кидает TargetClosedError -> НЕ ловить = успешный вход рапортуется провалом!**
    Всё взаимодействие с попапом — в try/except; закрытие = штатное завершение; AGW читать терпеливо (до ~20с).
  - Тайминги через прокси: модалка грузится 2–60с (зависит от загрузки машины!), popup ~2–26с. Вход ~46с.
  - **Зомби Chromium душат рендер**: 9 процессов -> модалка не грузится за 60с (ложный «не загрузилась»).
    Один чистый браузер = модалка за 2с. Перед прогоном чистить зомби (см. §Многопоточность).

## AGW-ветка (src/browser) — как это работает
- `wallet_provider.make_injector(pk)` -> (addr, inject_js, signer). Инжектим window.ethereum,
  подписываем personal_sign/typedData локально ОДНИМ ключом (без MetaMask-расширения). Крит.:
  подпись 0x-префиксить (hexbytes v1 .hex() без 0x -> Privy 'Could not log in').
- `relay_flow.login(page, ctx)` -> LoginResult(agw_address). Проверено. Retry клика Abstract, пока
  не откроется Privy popup; в попапе: Continue with a wallet -> MetaMask -> Approve.
- Playwright: launch_persistent_context(headless=False), ctx.expose_binding('__walletSign', signer),
  ctx.add_init_script(inject_js). relay.link — shadow DOM (Dynamic), локаторы пронзают open shadow.
- Тест: scratchpad/poc_login.py (эталон, стабильно логинится) и test_bridge_flow.py.

## Как тестировать без средств

E2E на случайном пустом кошельке (без транзакций): создать XLSX со сгенерированным ключом,
`Pipeline.run(dry_run=True)` — пройдёт preflight/discover/quote и корректно проставит SKIPPED.
Смоук Relay/RPC/Multicall: см. историю — `/chains`, `/currencies/v1`, quote от богатого адреса
(`0xf70da97812CB96acDF810712Aa562db8dfA3dbEF` — Relay solver).

## Конвенции

- Комментарии/логи/CLI — на русском; код — английские идентификаторы.
- Ошибки классифицировать через `src/core/errors.py`; новые транзиентные случаи → RetryableError.
- Любые новые сетевые вызовы кошелька — строго через его прокси и RpcPool.
- Секреты не коммитить: `.env`, `data/wallets.xlsx`, `data/proxies.txt`, `data/state.db` в .gitignore.
