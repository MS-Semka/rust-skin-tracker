"""
╔══════════════════════════════════════════════════════════════╗
║           RUST Skin Tracker V5 — web_app.py                  ║
║   Flask веб-интерфейс для управления парсером.               ║
║                                                              ║
║   Маршруты:                                                  ║
║     GET  /              → index.html                         ║
║     GET  /api/config    → текущие значения из config.py      ║
║     POST /api/start     → запуск парсера                     ║
║     GET  /api/status    → статус и прогресс                  ║
║     GET  /api/results   → данные из prices.db                ║
║     POST /api/clear     → очистить prices.db                 ║
╚══════════════════════════════════════════════════════════════╝
"""

import sqlite3
import threading
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template, request

import calculator
import config
import parse_lisskins_buy
import parse_steam_sell

# ══════════════════════════════════════════════════════════════
#   FLASK
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════
#   ГЛОБАЛЬНОЕ СОСТОЯНИЕ ПАРСЕРА
# ══════════════════════════════════════════════════════════════

parser_state: dict = {
    "running":    False,
    "progress":   0,
    "message":    "Ожидание",
    "last_run":   None,
    "last_error": None,
}

_state_lock = threading.Lock()


def _set_state(**kwargs) -> None:
    with _state_lock:
        parser_state.update(kwargs)


# ══════════════════════════════════════════════════════════════
#   ПОТОК ПАРСЕРА
# ══════════════════════════════════════════════════════════════

def _progress_callback(pct: int, msg: str) -> None:
    _set_state(progress=pct, message=msg)


def run_parser_thread(limit: int, min_price: float, max_price: float) -> None:
    try:
        _set_state(running=True, progress=0, message="Запуск парсера...", last_error=None)

        items = parse_lisskins_buy.parse_and_save(
            limit=limit,
            min_price=min_price,
            max_price=max_price,
            progress_callback=_progress_callback,
        )

        if not items:
            _set_state(
                running=False,
                progress=0,
                message="Ошибка: не удалось получить данные с lis-skins.com",
                last_error="Парсер lis-skins не вернул данных. Проверьте debug_page.html и parser.log",
            )
            return

        parse_steam_sell.update_steam_prices(progress_callback=_progress_callback)
        calculator.calculate_and_save(progress_callback=_progress_callback)

        _set_state(
            running=False,
            progress=100,
            message=f"Готово! Обработано скинов: {len(items)}",
            last_run=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            last_error=None,
        )

    except Exception as e:
        _set_state(
            running=False,
            progress=0,
            message=f"Критическая ошибка: {type(e).__name__}",
            last_error=str(e),
        )


# ══════════════════════════════════════════════════════════════
#   МАРШРУТЫ
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    """
    Главная страница.
    Передаёт текущие значения из config.py в шаблон через Jinja2,
    чтобы поля ввода отражали реальные настройки.
    """
    return render_template(
        "index.html",
        cfg_limit     = config.ITEMS_TO_PARSE,
        cfg_min_price = config.MIN_PRICE,
        cfg_max_price = config.MAX_PRICE,
    )


@app.route("/api/config")
def api_config():
    """
    Возвращает текущие значения параметров парсинга из config.py.
    Используется фронтендом для отображения актуальных дефолтов.
    """
    return jsonify({
        "limit":     config.ITEMS_TO_PARSE,
        "min_price": config.MIN_PRICE,
        "max_price": config.MAX_PRICE,
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    """
    Запускает парсер в отдельном потоке.
    Принимает JSON: {"limit": int, "min_price": float, "max_price": float}
    Если поля не переданы — берёт значения из config.py.
    """
    if parser_state["running"]:
        return jsonify({"ok": False, "message": "Парсер уже запущен. Дождитесь завершения."}), 409

    data = request.get_json(silent=True) or {}

    try:
        limit     = int(float(data.get("limit",     config.ITEMS_TO_PARSE)))
        min_price = float(data.get("min_price", config.MIN_PRICE))
        max_price = float(data.get("max_price", config.MAX_PRICE))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Некорректные параметры"}), 400

    if limit < 1 or limit > 500:
        return jsonify({"ok": False, "message": "limit должен быть от 1 до 500"}), 400
    if min_price < 0 or max_price < 0:
        return jsonify({"ok": False, "message": "Цены не могут быть отрицательными"}), 400
    if min_price > max_price:
        return jsonify({"ok": False, "message": "min_price не может быть больше max_price"}), 400

    # Обновляем config — чтобы модули парсера тоже видели актуальные значения
    config.ITEMS_TO_PARSE = limit
    config.MIN_PRICE      = min_price
    config.MAX_PRICE      = max_price

    thread = threading.Thread(
        target=run_parser_thread,
        args=(limit, min_price, max_price),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "ok":     True,
        "message": "Парсер запущен",
        "params": {"limit": limit, "min_price": min_price, "max_price": max_price},
    })


@app.route("/api/status")
def api_status():
    with _state_lock:
        state_copy = dict(parser_state)
    return jsonify(state_copy)


@app.route("/api/results")
def api_results():
    try:
        conn = sqlite3.connect(config.DB_FILE)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT
                skin_name,
                parse_lisskins_buy,
                parse_steam_sell,
                lisskins_buy_commission,
                steam_sell_commission,
                price_difference,
                percentage_indicator,
                color_indicator
            FROM skin_prices
            ORDER BY percentage_indicator DESC NULLS LAST, skin_name ASC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"ok": True, "count": len(rows), "data": rows})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e), "data": []}), 500


@app.route("/api/clear", methods=["POST"])
def api_clear():
    if parser_state["running"]:
        return jsonify({"ok": False, "message": "Нельзя удалить данные пока парсер работает"}), 409

    try:
        conn = sqlite3.connect(config.DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM skin_prices")
        cur.execute("DELETE FROM sqlite_sequence WHERE name='skin_prices'")
        conn.commit()
        conn.close()
        _set_state(progress=0, message="Данные удалены", last_run=None, last_error=None)
        return jsonify({"ok": True, "message": "Данные успешно удалены"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════
#   ЗАПУСК
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parse_lisskins_buy.init_database()

    print("=" * 55)
    print("  RUST Skin Tracker V5")
    print("  http://127.0.0.1:5000")
    print("=" * 55)

    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
