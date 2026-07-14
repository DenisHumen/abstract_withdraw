"""Чекер протоколов через DeBank (portfolio tracker).

Логика: открываем debank.com/profile/<agw_address> в Playwright, перехватываем ответы
внутренних API (portfolio/project_list, token/balance_list, user/used_chains) — их DeBank
подписывает своим JS, поэтому прямой fetch не работает, а перехват ответов — работает.
Парсим протоколы (какие используются) + токены + чейны.
"""
