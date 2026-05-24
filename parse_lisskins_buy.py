"""
╔══════════════════════════════════════════════════════════════╗
║        RUST Skin Tracker V5 — parse_lisskins_buy.py          ║
║   Парсит lis-skins.com и записывает в prices.db:             ║
║     • skin_name        — название скина                      ║
║     • parse_lisskins_buy — цена покупки в рублях             ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import os
import re
import sqlite3
import time
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config

# ══════════════════════════════════════════════════════════════
#   ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════════

logger = logging.getLogger("LisSkins")


def _setup_logging() -> None:
    """Настраивает логгер если он ещё не настроен."""
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
#   БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════

def init_database() -> None:
    """
    Создаёт таблицу skin_prices если её нет.
    Вызывается один раз перед началом парсинга.
    """
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS skin_prices (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            skin_name             TEXT    NOT NULL UNIQUE,
            parse_lisskins_buy    REAL    DEFAULT NULL,
            parse_steam_sell      REAL    DEFAULT NULL,
            lisskins_buy_commission REAL  DEFAULT NULL,
            steam_sell_commission REAL    DEFAULT NULL,
            price_difference      REAL    DEFAULT NULL,
            percentage_indicator  REAL    DEFAULT NULL,
            color_indicator       TEXT    DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("БД инициализирована")


def clear_table() -> None:
    """
    Полностью очищает таблицу перед новым парсингом.
    Все производные поля тоже сбрасываются.
    """
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM skin_prices")
    # Сбрасываем счётчик автоинкремента
    cur.execute(
        "DELETE FROM sqlite_sequence WHERE name='skin_prices'"
    )
    conn.commit()
    conn.close()
    logger.info("Таблица skin_prices очищена")


def save_skins(items: list[dict]) -> None:
    """
    Записывает список скинов в БД.
    Каждый элемент: {"skin_name": str, "parse_lisskins_buy": float}
    При конфликте имени — обновляет цену.
    """
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    for item in items:
        cur.execute(
            """
            INSERT INTO skin_prices (skin_name, parse_lisskins_buy)
            VALUES (?, ?)
            ON CONFLICT(skin_name) DO UPDATE SET
                parse_lisskins_buy = excluded.parse_lisskins_buy
            """,
            (item["skin_name"], item["parse_lisskins_buy"]),
        )
    conn.commit()
    conn.close()
    logger.info(f"Записано в БД: {len(items)} скинов")


# ══════════════════════════════════════════════════════════════
#   ПАРСИНГ ЦЕНЫ
# ══════════════════════════════════════════════════════════════

def parse_price_str(raw: str) -> Optional[float]:
    """
    Конвертирует строку с ценой в float.

    Поддерживаемые форматы (из инструкции V5):
        "1231412,12"   → 1231412.12
        "1231412.12"   → 1231412.12
        "1 234,56"     → 1234.56
        "150 ₽"        → 150.0
        "1 499,00 руб."→ 1499.0

    Алгоритм:
        1. Убрать всё кроме цифр, запятых и точек
        2. Заменить запятую на точку
        3. Если точек > 1 — убрать все кроме последней (разделитель тысяч)
        4. Привести к float
    """
    if not raw or not isinstance(raw, str):
        return None

    # Убираем всё кроме цифр, точек и запятых
    cleaned = re.sub(r"[^\d.,]", "", raw.strip())

    if not cleaned:
        return None

    # Заменяем запятую на точку
    cleaned = cleaned.replace(",", ".")

    # Если точек больше одной — все кроме последней убираем
    # Пример: "1.234.56" → "123456" + ".56" → "123456.56"  — НЕТ
    # Пример: "1.234,56" после замены "1.234.56"
    # → части: ["1", "234", "56"] → первые join без точки + "." + последняя
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]

    try:
        value = float(cleaned)
        # Защита: цена 0 или отрицательная — это точно ошибка парсинга
        return value if value > 0 else None
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════
#   ИЗВЛЕЧЕНИЕ ДАННЫХ ИЗ КАРТОЧКИ
# ══════════════════════════════════════════════════════════════

def extract_name(card) -> Optional[str]:
    """
    Извлекает название скина из HTML-карточки lis-skins.
    Основной селектор: .name-inner
    Fallback: .item-name, .name, h3, h4
    """
    # Основной селектор для lis-skins.com
    el = card.select_one(".name-inner")
    if el:
        text = el.get_text(strip=True)
        if text:
            return text

    # Fallback-селекторы
    for selector in [".item-name", ".name", "[class*='name']", "h3", "h4"]:
        el = card.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text

    return None


def extract_price(card) -> Optional[float]:
    """
    Извлекает цену из HTML-карточки lis-skins.
    Основной селектор: .price
    Fallback: .item-price, [class*='price']

    Важно: .price содержит SVG-иконку рубля после числа,
    get_text() её игнорирует — берём только текстовый узел.
    """
    el = card.select_one(".price")
    if el:
        raw = el.get_text(strip=True)
        price = parse_price_str(raw)
        if price is not None:
            return price

    # Fallback-селекторы
    for selector in [".item-price", "[class*='price']"]:
        el = card.select_one(selector)
        if el:
            raw = el.get_text(strip=True)
            price = parse_price_str(raw)
            if price is not None:
                return price

    return None


# ══════════════════════════════════════════════════════════════
#   ЗАГРУЗКА СТРАНИЦЫ
# ══════════════════════════════════════════════════════════════

def fetch_with_curl_cffi() -> Optional[str]:
    """
    Загружает страницу lis-skins через curl_cffi.
    Имитирует Chrome 131 для обхода Cloudflare.

    Передаёт:
        - Accept-Language: ru-RU (регион RUS из инструкции)
        - cookies currency=RUB + locale=ru (исправление бага V1-V3)
        - URL с ?currency=RUB (двойная гарантия рублей)
    """
    logger.info("Загрузка через curl_cffi (Chrome impersonate)...")
    try:
        resp = curl_requests.get(
            config.LIS_SKINS_URL,
            headers=config.HEADERS,
            cookies=config.LIS_SKINS_COOKIES,
            timeout=config.REQUEST_TIMEOUT,
            impersonate="chrome131",
            allow_redirects=True,
        )

        logger.debug(f"Статус: {resp.status_code} | Размер: {len(resp.text)} байт")

        if resp.status_code == 200:
            # Сохраняем для отладки (в .gitignore)
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(resp.text)
            logger.info(f"Страница получена ({len(resp.text):,} байт) → debug_page.html")
            return resp.text

        elif resp.status_code == 403:
            logger.warning("403 Forbidden — Cloudflare заблокировал. Пробуем Selenium...")
        elif resp.status_code == 429:
            logger.warning("429 Too Many Requests — сайт ограничивает запросы")
        elif resp.status_code == 503:
            logger.warning("503 Service Unavailable — сайт на обслуживании")
        else:
            logger.warning(f"Неожиданный статус: {resp.status_code}")

    except Exception as e:
        logger.error(f"curl_cffi ошибка: {type(e).__name__} — {e}")

    return None


def fetch_with_selenium() -> Optional[str]:
    """
    Загружает страницу lis-skins через Selenium + Edge WebDriver.
    Используется как fallback если curl_cffi не прошёл Cloudflare.

    msedgedriver.exe должен находиться рядом с этим файлом.
    """
    logger.info("Загрузка через Selenium (Edge headless)...")
    driver = None

    try:
        driver_path = os.path.join(os.path.dirname(__file__), "msedgedriver.exe")
        if not os.path.exists(driver_path):
            logger.error(f"msedgedriver.exe не найден: {driver_path}")
            return None

        opts = EdgeOptions()
        opts.add_argument("--headless")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        # Скрываем признаки автоматизации
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(f"--user-agent={config.HEADERS['User-Agent']}")
        # Язык браузера — ru (для корректного региона)
        opts.add_argument("--lang=ru-RU")

        service = EdgeService(executable_path=driver_path)
        driver = webdriver.Edge(service=service, options=opts)
        driver.set_window_size(1920, 1080)

        logger.info(f"Открываю: {config.LIS_SKINS_URL}")
        driver.get(config.LIS_SKINS_URL)

        # Ждём появления тела страницы (до 20 сек)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            logger.warning("Таймаут ожидания body (20 сек)")

        # Дополнительная пауза — ждём рендер JS-контента
        time.sleep(3)

        html = driver.page_source

        # Сохраняем для отладки
        with open("debug_page_selenium.html", "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Страница получена через Selenium ({len(html):,} байт) → debug_page_selenium.html")

        return html

    except Exception as e:
        logger.error(f"Selenium ошибка: {type(e).__name__} — {e}")
        return None

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
#   ПАРСИНГ HTML
# ══════════════════════════════════════════════════════════════

def parse_html(html: str, limit: int, min_price: float, max_price: float) -> list[dict]:
    """
    Извлекает скины из HTML-страницы lis-skins.
    Возвращает список: [{"skin_name": str, "parse_lisskins_buy": float}, ...]

    Фильтрует по ценовому диапазону [min_price, max_price].
    Останавливается после `limit` подходящих скинов.
    """
    soup = BeautifulSoup(html, "lxml")

    # Основной селектор карточек товаров на lis-skins
    cards = soup.select(".item.market_item.item_rust")

    if not cards:
        # Пробуем более общие селекторы
        for selector in [".item.market_item", ".market_item", "[class*='item_rust']"]:
            cards = soup.select(selector)
            if cards:
                logger.info(f"Карточки найдены по запасному селектору: '{selector}'")
                break

    if not cards:
        logger.error(
            "Карточки скинов не найдены. "
            "Проверьте debug_page.html — возможно сайт изменил HTML-структуру."
        )
        return []

    logger.info(f"Найдено карточек на странице: {len(cards)}")

    items: list[dict] = []
    skipped_no_name  = 0
    skipped_no_price = 0
    skipped_range    = 0

    for card in cards:
        if len(items) >= limit:
            break

        name = extract_name(card)
        if not name:
            skipped_no_name += 1
            continue

        price = extract_price(card)
        if price is None:
            skipped_no_price += 1
            logger.debug(f"Нет цены: {name}")
            continue

        if not (min_price <= price <= max_price):
            skipped_range += 1
            logger.debug(f"Вне диапазона [{min_price}–{max_price}]: {name} = {price}")
            continue

        items.append({"skin_name": name, "parse_lisskins_buy": price})
        logger.debug(f"  ✓  {name}  →  {price:.2f} ₽")

    logger.info(
        f"Итог парсинга HTML: принято={len(items)}, "
        f"без имени={skipped_no_name}, "
        f"без цены={skipped_no_price}, "
        f"вне диапазона={skipped_range}"
    )
    return items


# ══════════════════════════════════════════════════════════════
#   ГЛАВНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════

def parse_and_save(
    limit: int     = config.ITEMS_TO_PARSE,
    min_price: float = config.MIN_PRICE,
    max_price: float = config.MAX_PRICE,
    progress_callback=None,
) -> list[dict]:
    """
    Главная функция модуля. Полный цикл:
        1. Инициализировать / очистить БД
        2. Загрузить страницу (curl_cffi → Selenium)
        3. Распарсить карточки
        4. Сохранить в prices.db
        5. Вернуть список скинов

    Параметры:
        limit          — максимальное количество скинов
        min_price      — минимальная цена (рубли)
        max_price      — максимальная цена (рубли)
        progress_callback(pct: int, msg: str) — колбэк прогресса для web_app.py

    Возвращает:
        list[dict] — список {"skin_name", "parse_lisskins_buy"}
                     или [] при ошибке
    """

    def _progress(pct: int, msg: str) -> None:
        logger.info(f"[{pct}%] {msg}")
        if progress_callback:
            progress_callback(pct, msg)

    _progress(5, "Инициализация базы данных...")
    init_database()
    clear_table()

    # ── Загрузка страницы ──────────────────────────────────────
    html: Optional[str] = None

    for attempt in range(1, config.MAX_RETRIES + 1):
        _progress(
            10 + attempt * 3,
            f"Загрузка lis-skins.com (попытка {attempt}/{config.MAX_RETRIES})...",
        )

        html = fetch_with_curl_cffi()

        if not html:
            _progress(
                10 + attempt * 3,
                f"curl_cffi не помог — пробуем Selenium (попытка {attempt})...",
            )
            html = fetch_with_selenium()

        if html:
            break

        if attempt < config.MAX_RETRIES:
            logger.warning(f"Пауза {config.RETRY_DELAY} сек перед следующей попыткой...")
            time.sleep(config.RETRY_DELAY)

    if not html:
        logger.error(
            f"Не удалось загрузить lis-skins.com после {config.MAX_RETRIES} попыток.\n"
            "Возможные причины:\n"
            "  • Нет доступа в интернет\n"
            "  • Сайт недоступен или изменил структуру\n"
            "  • Требуется VPN\n"
            "Подробности — в parser.log и debug_page.html"
        )
        _progress(0, "Ошибка: не удалось загрузить lis-skins.com")
        return []

    # ── Парсинг HTML ───────────────────────────────────────────
    _progress(35, f"Парсинг карточек (лимит={limit}, цена={min_price}–{max_price} ₽)...")
    items = parse_html(html, limit=limit, min_price=min_price, max_price=max_price)

    if not items:
        logger.error(
            "Парсинг не дал результатов.\n"
            "Откройте debug_page.html и проверьте структуру страницы вручную."
        )
        _progress(0, "Ошибка: скины не найдены на странице")
        return []

    # ── Сохранение в БД ───────────────────────────────────────
    _progress(38, f"Сохранение {len(items)} скинов в prices.db...")
    save_skins(items)

    _progress(40, f"lis-skins готово: {len(items)} скинов записано в БД")
    return items


# ══════════════════════════════════════════════════════════════
#   ЗАПУСК НАПРЯМУЮ (для отладки)
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  parse_lisskins_buy.py — тестовый запуск")
    print("=" * 60)
    print(f"  URL      : {config.LIS_SKINS_URL}")
    print(f"  Лимит    : {config.ITEMS_TO_PARSE}")
    print(f"  Диапазон : {config.MIN_PRICE} – {config.MAX_PRICE} ₽")
    print("=" * 60)

    result = parse_and_save()

    if result:
        print(f"\n  Найдено скинов: {len(result)}\n")
        for i, item in enumerate(result, 1):
            print(f"  {i:>3}. {item['skin_name']:<45} {item['parse_lisskins_buy']:>10.2f} ₽")
    else:
        print("\n  Результатов нет. Проверьте debug_page.html и parser.log")
