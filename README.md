# RUST Skin Tracker V6

Приложение для поиска выгодных скинов RUST:
купить на lis-skins.com → продать на Steam Market.

## Установка и запуск
### Требования

- Python 3.10+
- Microsoft Edge браузер (для Selenium fallback)
- `msedgedriver.exe` в папке проекта (версия должна совпадать с Edge)

## Установка необходимых библиотек

pip install -r requirements.txt

## Запуск

Запускаем python **web_app.py**
Затем открыть браузер: http://127.0.0.1:5000

### Типичный рабочий цикл

1. Открыть `http://127.0.0.1:5000`
2. Задать количество скинов, ценовой диапазон
3. Нажать **🚀 Запустить**
4. Дождаться 100% на прогресс-баре (время зависит от количества скинов и `STEAM_DELAY`)
5. Изучить таблицу — синие и зелёные строки = лучшие сделки

## Зависимости

- Python 3.10+
- Microsoft Edge + msedgedriver.exe (скачать с https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/)
