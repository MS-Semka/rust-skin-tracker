"""
╔══════════════════════════════════════════════════════════════╗
║        RUST Skin Tracker V6 — parse_steam_sell.py            ║
║   Берёт skin_name из prices.db, запрашивает страницу         ║
║   листинга Steam и записывает обратно:                       ║
║     • parse_steam_sell — HIGHEST BUY ORDER в рублях          ║
║                                                              ║
║   ⚠️  КЛЮЧЕВОЕ ИЗМЕНЕНИЕ (V6):                               ║
║   Ранее использовался priceoverview API → lowest_price       ║
║   (минимальная цена продажи = цена ПОКУПКИ скина).           ║
║   Теперь парсится страница листинга → highest buy order      ║
║   (максимальная заявка на покупку = цена ПРОДАЖИ скина).     ║
║   Цель проекта: купить на lis-skins, продать на Steam.       ║
║   Значит нужна именно цена buy order, а не sell listing.     ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import re
import sqlite3
import time
from typing import Optional
from urllib.parse import quote

import requests

import config

# ══════════════════════════════════════════════════════════════
#   ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════════

logger = logging.getLogger("SteamSell")


def _setup_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(getattr(logging, config.LOG_LEVEL))
    fmt = logging.Formatter(config.LOG_FORMAT)
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, config.LOG_LEVEL))
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)


_setup_logging()


# ══════════════════════════════════════════════════════════════
#   РАБОТА С БД
# ══════════════════════════════════════════════════════════════

def get_skins_to_process() -> list[str]:
    """Читает skin_name для всех скинов с заполненной ценой lis-skins."""
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT skin_name FROM skin_prices "
        "WHERE parse_lisskins_buy IS NOT NULL "
        "ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()
    names = [r[0] for r in rows]
    logger.info(f"Скинов для запроса в Steam: {len(names)}")
    return names


def save_steam_price(skin_name: str, price: Optional[float]) -> None:
    """Обновляет parse_steam_sell для конкретного скина."""
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE skin_prices SET parse_steam_sell = ? WHERE skin_name = ?",
        (price, skin_name),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════
#   НОРМАЛИЗАЦИЯ ИМЕНИ ДЛЯ STEAM
# ══════════════════════════════════════════════════════════════

def normalize_steam_name(name: str) -> str:
    """
    Приводит название скина с lis-skins к формату Steam Market.
    Таблица known-cases + нормализация разделителя " - " => " | ".
    Fallback на оригинал — в get_steam_price().
    """
    name = name.strip()

    special_cases = {
        "Glory AK47":               "AK-47 | Glory",
        "Victoria AK-47":           "AK-47 | Victoria",
        "After Death AR":           "Assault Rifle | After Death",
        "Tempered Mp5":             "MP5A1 | Tempered",
        "Military Camo MP5":        "MP5A1 | Military Camo",
        "Digital Camo MP5":         "MP5A1 | Digital Camo",
        "Banana Eoka":              "Eoka Pistol | Banana",
        "Tempered Mask":            "Metal Facemask | Tempered",
        "Legendary Gold Facemask":  "Metal Facemask | Legendary Gold",
        "Stainless Facemask":       "Metal Facemask | Stainless",
        "Desert Raiders Facemask":  "Metal Facemask | Desert Raiders",
        "Faded SAP":                "Salvaged Axe | Faded",
        "Direct Threat SAP":        "Salvaged Axe | Direct Threat",
        "Welded Hammer":            "Salvaged Hammer | Welded",
        "Halloween Bat":            "Baseball Bat | Halloween",
        "AK-47 Victoria":           "AK-47 | Victoria",
        "Space Rocket Work Gloves": "Gloves | Space Rocket",
        "Plate Carrier - Black":    "Plate Carrier | Black",
        "Christmas Tree Door":      "Armored Double Door | Christmas Tree",
    }

    name_lower = name.lower()
    for original, steam_name in special_cases.items():
        if original.lower() == name_lower:
            return steam_name

    return name.replace(" - ", " | ").replace(" — ", " | ")


# ══════════════════════════════════════════════════════════════
#   ПАРСИНГ ЦЕНЫ
# ══════════════════════════════════════════════════════════════

def parse_price_str(raw: str) -> Optional[float]:
    """
    Конвертирует строку цены со страницы листинга Steam в float.

    Поддерживаемые форматы (уже в рублях, НЕ в копейках):
        "281,46 руб."   -> 281.46
        "1 234,56 руб." -> 1234.56
        "150,30 руб."   -> 150.30

    Алгоритм:
        1. Убрать всё кроме цифр, запятых и точек
        2. ВАЖНО: убрать крайние . и , (точка в "руб." захватывалась regex!)
        3. Заменить запятую на точку
        4. Если точек > 1 — убрать все кроме последней (тысячный разделитель)
        5. Привести к float

    ЛОВУШКА (баг): "281,46 руб." после regex -> "281,46."
    Точка в конце слова "руб." захватывается [^\d.,].
    После replace(","->".") -> "281.46." -> split -> ["281","46",""]
    -> join -> "28146." -> float -> 28146.0  (НЕВЕРНО!)
    Исправление: cleaned.strip(".,") перед replace снимает крайние точки/запятые.

    ВАЖНО: цены на странице листинга уже в РУБЛЯХ.
    В отличие от priceoverview API (который отдавал копейки),
    здесь деление на 100 НЕ нужно.
    """
    if not raw or not isinstance(raw, str):
        return None

    # Шаг 1: оставляем только цифры, запятые, точки
    cleaned = re.sub(r"[^\d.,]", "", raw.strip())
    if not cleaned:
        return None

    # Шаг 2: убираем крайние . и , (точка из "руб." попадает сюда)
    # "281,46." -> "281,46"  |  "1 234,56." -> "1234,56"
    cleaned = cleaned.strip(".,")
    if not cleaned:
        return None

    # Шаг 3: заменяем десятичный разделитель-запятую на точку
    cleaned = cleaned.replace(",", ".")

    # Шаг 4: если точек > 1 — убираем тысячные разделители (бывает "1.234.56")
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]

    try:
        value = float(cleaned)
        return value if value > 0 else None
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════
#   ЗАПРОС К СТРАНИЦЕ ЛИСТИНГА STEAM
# ══════════════════════════════════════════════════════════════

# Regex для поиска highest buy order в HTML страницы листинга.
# Паттерн ищет текст "Заявок на покупку по цене" и следующий <span> с ценой.
# Используем текст интерфейса, а не CSS-классы — классы обфусцированы и могут
# меняться при деплоях, русский текст интерфейса стабилен.
#
# HTML-структура (из Page source code.txt):
#   Заявок на покупку по цене
#   <span class="NI9oaXH36YQ-" style="--text-color:var(--color-text-body-title)">
#     281,46 руб.
#   </span>
#   и ниже: <span ...>144</span>
#
_BUY_ORDER_RE = re.compile(
    r"Заявок на покупку по цене\s*<span[^>]*>([^<]+)</span>",
    re.IGNORECASE,
)

# Fallback regex: первая строка таблицы заявок на покупку.
# Ищет первый <td> в <tbody> таблицы после заголовка "Цена".
_BUY_TABLE_RE = re.compile(
    r"<th><span[^>]*>Цена</span></th>.*?<tbody>"
    r".*?<td><span[^>]*>([^<]+руб[^<]*)</span></td>",
    re.DOTALL | re.IGNORECASE,
)


def _fetch_listing_page(
    session: requests.Session,
    skin_name: str,
) -> Optional[str]:
    """
    Загружает HTML страницы листинга Steam Market для скина.

    URL: https://steamcommunity.com/market/listings/252490/{encoded_name}

    Страница является SSR (server-side rendered) — данные о заявках на покупку
    присутствуют в исходном HTML без JavaScript. Это подтверждено анализом
    view-source страницы: таблица buy orders видна в исходнике.

    Возвращает HTML-строку или None при ошибке.
    """
    encoded = quote(skin_name, safe="")
    url = f"https://steamcommunity.com/market/listings/252490/{encoded}"

    for attempt in range(config.MAX_RETRIES):
        try:
            resp = session.get(
                url,
                headers=config.HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )

            if resp.status_code == 429:
                wait = 15 + attempt * 5   # 15, 20, 25 сек
                logger.warning(
                    f"  429 Too Many Requests для '{skin_name}'. "
                    f"Пауза {wait} сек... (попытка {attempt + 1}/{config.MAX_RETRIES})"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                logger.debug(f"  404 — скин не найден: '{skin_name}'")
                return None

            if resp.status_code != 200:
                logger.warning(f"  HTTP {resp.status_code} для '{skin_name}'")
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(config.RETRY_DELAY)
                    continue
                return None

            return resp.text

        except requests.exceptions.Timeout:
            logger.warning(
                f"  Таймаут для '{skin_name}' "
                f"(попытка {attempt + 1}/{config.MAX_RETRIES})"
            )
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_DELAY)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"  Ошибка соединения для '{skin_name}': {e}")
            return None
        except Exception as e:
            logger.error(
                f"  Неожиданная ошибка для '{skin_name}': "
                f"{type(e).__name__} — {e}"
            )
            return None

    return None


def _parse_buy_order_from_html(html: str) -> Optional[float]:
    """
    Извлекает highest buy order из HTML страницы листинга Steam.

    Стратегия парсинга (два уровня надёжности):

    1. Основной (_BUY_ORDER_RE):
       Ищет "Заявок на покупку по цене X руб." — заголовок блока buy orders.
       Это самое точное место: цена здесь = лучшая активная заявка.

    2. Fallback (_BUY_TABLE_RE):
       Первая строка <tbody> таблицы заявок на покупку.
       Пропускает строки вида "X руб. и менее" (агрегированные).

    Оба regex используют текст интерфейса, а не CSS-классы.
    Возвращает цену в рублях (float) или None если заявок нет.
    """

    # Попытка 1: заголовок блока buy orders
    m = _BUY_ORDER_RE.search(html)
    if m:
        raw = m.group(1).strip()
        price = parse_price_str(raw)
        if price is not None:
            logger.debug(f"  [primary] buy order: '{raw}' -> {price} руб.")
            return price

    # Попытка 2: первая строка таблицы buy orders
    m2 = _BUY_TABLE_RE.search(html)
    if m2:
        raw = m2.group(1).strip()
        # Пропускаем агрегированную строку "254,64 руб. и менее"
        if "и менее" in raw:
            logger.debug(f"  [fallback] пропуск агрегированной строки: '{raw}'")
        else:
            price = parse_price_str(raw)
            if price is not None:
                logger.debug(f"  [fallback] buy order из таблицы: '{raw}' -> {price} руб.")
                return price

    return None


# ══════════════════════════════════════════════════════════════
#   ПОЛУЧЕНИЕ ЦЕНЫ ОДНОГО СКИНА
# ══════════════════════════════════════════════════════════════

def get_steam_price(
    session: requests.Session,
    skin_name: str,
) -> Optional[float]:
    """
    Получает highest buy order price скина со страницы листинга Steam.

    ЧТО ИЗМЕНИЛОСЬ В V6:
    V5 и ранее:
        GET priceoverview API -> lowest_price (в копейках, делилось на 100)
        -> это минимальная цена ЛИСТИНГА ПРОДАЖИ
        -> отвечает на вопрос "по сколько стоит КУПИТЬ скин"

    V6 (текущий):
        GET страница листинга -> "Заявок на покупку по цене X руб."
        -> это HIGHEST BUY ORDER (максимальная заявка на покупку)
        -> отвечает на вопрос "по сколько получу, если ПРОДАМ скин"

    Почему это правильно:
        Стратегия проекта — купить на lis-skins, продать на Steam.
        Продать быстро = принять лучшую заявку на покупку.
        Highest buy order и есть цена мгновенной продажи.

    Цены на странице листинга уже в РУБЛЯХ — деление на 100 не нужно.

    Стратегия двух попыток:
        1. normalize_steam_name(skin_name) — основное имя для Steam
        2. skin_name оригинальный — fallback (как в V5)

    Возвращает цену в РУБЛЯХ (float) или None.
    """

    names_to_try = [normalize_steam_name(skin_name)]
    if names_to_try[0] != skin_name:
        names_to_try.append(skin_name)

    for attempt_name in names_to_try:
        logger.debug(f"  Загружаю страницу листинга: '{attempt_name}'")

        html = _fetch_listing_page(session, attempt_name)
        if html is None:
            logger.warning(f"  Не удалось загрузить страницу для '{attempt_name}'")
            continue

        price = _parse_buy_order_from_html(html)

        if price is not None:
            if attempt_name != skin_name:
                logger.info(f"  Fallback сработал: '{skin_name}' -> '{attempt_name}'")
            return price
        else:
            logger.debug(f"  Нет заявок на покупку для '{attempt_name}'")

    return None


# ══════════════════════════════════════════════════════════════
#   ГЛАВНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════

def update_steam_prices(progress_callback=None) -> None:
    """
    Полный цикл Steam-парсера:
        1. Читает skin_name из prices.db
        2. Для каждого скина загружает страницу листинга Steam
        3. Парсит highest buy order (цену мгновенной продажи)
        4. Записывает parse_steam_sell (в рублях) обратно в БД

    Прогресс: 40% -> 90%.
    Задержка STEAM_DELAY сек между запросами (защита от 429).

    ВНИМАНИЕ: страница листинга тяжелее priceoverview API.
    При частых 429 — увеличьте STEAM_DELAY в config.py до 7-10 сек.
    """

    def _progress(pct: int, msg: str) -> None:
        logger.info(f"[{pct}%] {msg}")
        if progress_callback:
            progress_callback(pct, msg)

    skins = get_skins_to_process()
    if not skins:
        _progress(90, "Нет скинов для запроса в Steam")
        return

    session = requests.Session()
    total         = len(skins)
    success_count = 0
    no_data_count = 0

    for i, skin_name in enumerate(skins):
        pct   = int(40 + (i / total) * 50)
        short = skin_name[:45] + "..." if len(skin_name) > 45 else skin_name
        _progress(pct, f"Steam [{i + 1}/{total}]: {short}")

        price = get_steam_price(session, skin_name)

        if price is not None:
            save_steam_price(skin_name, price)
            success_count += 1
            logger.info(f"  OK  {skin_name[:45]}  ->  buy order: {price:.2f} руб.")
        else:
            save_steam_price(skin_name, None)
            no_data_count += 1
            logger.info(f"  --  {skin_name[:45]}  ->  нет заявок на покупку")

        if i < total - 1:
            time.sleep(config.STEAM_DELAY)

    _progress(
        90,
        f"Steam готово: найдено={success_count}, нет данных={no_data_count}",
    )
    logger.info(
        f"Steam парсер завершён | успешно={success_count} | нет данных={no_data_count}"
    )


# ══════════════════════════════════════════════════════════════
#   ЗАПУСК НАПРЯМУЮ (для отладки)
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  parse_steam_sell.py — тестовый запуск (V6)")
    print("  Режим: highest buy order со страницы листинга")
    print("=" * 60)
    print(f"  Задержка   : {config.STEAM_DELAY} сек между запросами")
    print("=" * 60)

    update_steam_prices()

    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT skin_name, parse_lisskins_buy, parse_steam_sell "
        "FROM skin_prices ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()

    if rows:
        print(f"\n  {'No':<4} {'Скин':<45} {'lis-skins':>10} {'Steam buy':>12}")
        print("  " + "-" * 74)
        for i, (name, lis, steam) in enumerate(rows, 1):
            lis_str   = f"{lis:.2f} руб."   if lis   is not None else "—"
            steam_str = f"{steam:.2f} руб." if steam is not None else "нет заявок"
            print(f"  {i:<4} {name[:45]:<45} {lis_str:>12} {steam_str:>14}")
    else:
        print("\n  Нет данных в БД.")
