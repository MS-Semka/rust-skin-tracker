"""
╔══════════════════════════════════════════════════════════════╗
║          RUST Skin Tracker V5 — calculator.py               ║
║   Читает prices.db и вычисляет для каждого скина:           ║
║     • lisskins_buy_commission                                ║
║     • steam_sell_commission                                  ║
║     • price_difference                                       ║
║     • percentage_indicator                                   ║
║     • color_indicator                                        ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import sqlite3
from typing import Optional

import config

# ══════════════════════════════════════════════════════════════
#   ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════════

logger = logging.getLogger("Calculator")


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
#   ФОРМУЛЫ
#   Все формулы строго по инструкции PARSER lis-skins - steam.md
# ══════════════════════════════════════════════════════════════

def calc_lisskins_commission(parse_lisskins_buy: float) -> float:
    """
    lisskins_buy_commission = parse_lisskins_buy × coefficient_lisskins_buy
    coefficient_lisskins_buy = 1.01  (наценка 1%)
    """
    return round(parse_lisskins_buy * config.coefficient_lisskins_buy, 2)


def calc_steam_commission(parse_steam_sell: float) -> float:
    """
    steam_sell_commission = parse_steam_sell × coefficient_steam_sell
    coefficient_steam_sell = 0.85  (комиссия Steam 15%)
    """
    return round(parse_steam_sell * config.coefficient_steam_sell, 2)


def calc_price_difference(
    steam_sell_commission: float,
    lisskins_buy_commission: float,
) -> float:
    """
    price_difference = steam_sell_commission − lisskins_buy_commission
    Без модуля — отрицательное значение означает убыток.
    """
    return round(steam_sell_commission - lisskins_buy_commission, 2)


def calc_percentage_indicator(
    steam_sell_commission: float,
    lisskins_buy_commission: float,
) -> Optional[float]:
    """
    percentage_indicator = (steam_sell_commission − lisskins_buy_commission)
                           / lisskins_buy_commission
    Без модуля. Возвращает долю (не проценты): 0.15 = 15%.
    Возвращает None если lisskins_buy_commission = 0 (деление на ноль).
    """
    if lisskins_buy_commission == 0:
        return None
    result = (steam_sell_commission - lisskins_buy_commission) / lisskins_buy_commission
    return round(result, 6)


def get_color(percentage_indicator: Optional[float]) -> str:
    """
    Определяет color_indicator по значению percentage_indicator.
    percentage_indicator — доля (0.15 = 15%), не проценты.

    Таблица из инструкции:
        < 0          → red    (Убыточно)
        [0;  0.10)   → grey   (Небольшая прибыль)
        [0.10; 0.15) → orange (Хорошая прибыль)
        [0.15; 0.20) → yellow (Очень хорошая прибыль)
        [0.20; 0.25) → green  (Отличная прибыль)
        ≥ 0.25       → blue   (Превосходная прибыль)
    """
    if percentage_indicator is None:
        return "grey"

    for color, bounds in config.COLOR_THRESHOLDS.items():
        if bounds["min"] <= percentage_indicator < bounds["max"]:
            return color

    # Защита: если ни один порог не сработал — серый
    return "grey"


# ══════════════════════════════════════════════════════════════
#   РАБОТА С БД
# ══════════════════════════════════════════════════════════════

def get_rows_to_calculate() -> list[tuple]:
    """
    Читает из prices.db строки у которых заполнены оба сырых поля:
        parse_lisskins_buy IS NOT NULL
        parse_steam_sell   IS NOT NULL

    Возвращает список кортежей: (id, skin_name, parse_lisskins_buy, parse_steam_sell)
    Строки где нет цены Steam — пропускаем (calculator не может их посчитать).
    """
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, skin_name, parse_lisskins_buy, parse_steam_sell
        FROM skin_prices
        WHERE parse_lisskins_buy IS NOT NULL
          AND parse_steam_sell   IS NOT NULL
        ORDER BY id
    """)
    rows = cur.fetchall()
    conn.close()
    logger.info(f"Строк для расчёта: {len(rows)}")
    return rows


def save_calculated(
    skin_id: int,
    lisskins_buy_commission: float,
    steam_sell_commission: float,
    price_difference: float,
    percentage_indicator: Optional[float],
    color_indicator: str,
) -> None:
    """
    Записывает все вычисленные поля для одного скина по его id.
    """
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        UPDATE skin_prices SET
            lisskins_buy_commission = ?,
            steam_sell_commission   = ?,
            price_difference        = ?,
            percentage_indicator    = ?,
            color_indicator         = ?
        WHERE id = ?
    """, (
        lisskins_buy_commission,
        steam_sell_commission,
        price_difference,
        percentage_indicator,
        color_indicator,
        skin_id,
    ))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════
#   ГЛАВНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════

def calculate_and_save(progress_callback=None) -> None:
    """
    Главная функция модуля. Полный цикл:
        1. Читает строки с обоими сырыми ценами из prices.db
        2. Вычисляет все 5 производных полей
        3. Записывает результаты обратно в prices.db

    Параметры:
        progress_callback(pct: int, msg: str) — колбэк для web_app.py
        Прогресс идёт от 90% до 95%
    """

    def _progress(pct: int, msg: str) -> None:
        logger.info(f"[{pct}%] {msg}")
        if progress_callback:
            progress_callback(pct, msg)

    _progress(90, "Расчёт комиссий и прибыли...")

    rows = get_rows_to_calculate()

    if not rows:
        _progress(95, "Нет данных для расчёта (нужны обе цены)")
        logger.warning(
            "Нет строк с заполненными parse_lisskins_buy и parse_steam_sell. "
            "Расчёт пропущен."
        )
        return

    calculated   = 0
    profitable   = 0
    unprofitable = 0

    colors = config.CONSOLE_COLORS

    for row in rows:
        skin_id, skin_name, lis_raw, steam_raw = row

        # ── Шаг 1: комиссии ───────────────────────────────────
        lis_commission   = calc_lisskins_commission(lis_raw)
        steam_commission = calc_steam_commission(steam_raw)

        # ── Шаг 2: разница цен ────────────────────────────────
        difference = calc_price_difference(steam_commission, lis_commission)

        # ── Шаг 3: процентный индикатор ───────────────────────
        pct = calc_percentage_indicator(steam_commission, lis_commission)

        # ── Шаг 4: цветовой индикатор ─────────────────────────
        color = get_color(pct)

        # ── Шаг 5: запись в БД ────────────────────────────────
        save_calculated(
            skin_id,
            lis_commission,
            steam_commission,
            difference,
            pct,
            color,
        )

        # Статистика
        calculated += 1
        if difference > 0:
            profitable += 1
        else:
            unprofitable += 1

        # Лог с цветом в консоли
        pct_display = f"{pct * 100:+.2f}%" if pct is not None else "N/A"
        col   = colors.get(color, "")
        reset = colors["reset"]
        label = config.COLOR_LABELS.get(color, color)
        logger.info(
            f"  {col}{skin_name[:42]:<42}  "
            f"lis={lis_commission:.2f} ₽  "
            f"steam={steam_commission:.2f} ₽  "
            f"diff={difference:+.2f} ₽  "
            f"{pct_display}  [{label}]{reset}"
        )

    _progress(
        95,
        f"Калькулятор готово: посчитано={calculated}, "
        f"прибыльных={profitable}, убыточных={unprofitable}",
    )
    logger.info(
        f"Расчёт завершён | "
        f"посчитано={calculated} | "
        f"прибыльных={profitable} | "
        f"убыточных={unprofitable}"
    )


# ══════════════════════════════════════════════════════════════
#   ЗАПУСК НАПРЯМУЮ (для отладки)
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("  calculator.py — тестовый запуск")
    print("=" * 70)
    print(f"  coefficient_lisskins_buy : {config.coefficient_lisskins_buy}")
    print(f"  coefficient_steam_sell   : {config.coefficient_steam_sell}")
    print("=" * 70)

    calculate_and_save()

    # Показываем итоговую таблицу из БД
    conn = sqlite3.connect(config.DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT skin_name,
               lisskins_buy_commission,
               steam_sell_commission,
               price_difference,
               percentage_indicator,
               color_indicator
        FROM skin_prices
        WHERE lisskins_buy_commission IS NOT NULL
        ORDER BY percentage_indicator DESC
    """)
    rows = cur.fetchall()
    conn.close()

    colors = config.CONSOLE_COLORS

    if rows:
        print(f"\n  {'№':<4} {'Скин':<38} {'lis':>9} {'steam':>9} {'разница':>9} {'%':>8}  цвет")
        print("  " + "─" * 85)
        for i, (name, lis, steam, diff, pct, color) in enumerate(rows, 1):
            lis_s   = f"{lis:.2f}"           if lis   is not None else "—"
            steam_s = f"{steam:.2f}"         if steam is not None else "—"
            diff_s  = f"{diff:+.2f}"         if diff  is not None else "—"
            pct_s   = f"{pct * 100:+.2f}%"  if pct   is not None else "—"
            col     = colors.get(color, "")
            reset   = colors["reset"]
            print(
                f"  {col}{i:<4} {name[:38]:<38} "
                f"{lis_s:>9} {steam_s:>9} {diff_s:>9} {pct_s:>8}  {color}{reset}"
            )
    else:
        print(
            "\n  Нет данных. Сначала запустите:\n"
            "    python parse_lisskins_buy.py\n"
            "    python parse_steam_sell.py"
        )
