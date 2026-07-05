#!/usr/bin/env bash
# Запуск бота постоянным процессом (ответы за секунды).
# 1) Один раз создай рядом файл .env с токеном (он не попадёт в git):
#      TELEGRAM_BOT_TOKEN=1234:AA...
#      TELEGRAM_CHAT_ID=123456789
# 2) Запуск:   ./run-bot.sh
# Остановить — Ctrl+C. Бот отвечает на команды и кнопки мгновенно.
set -euo pipefail
cd "$(dirname "$0")"

# Подхватываем .env, если есть
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "❌ Нет TELEGRAM_BOT_TOKEN. Создай файл .env (см. комментарий в run-bot.sh)."
  exit 1
fi

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" radar.py --serve
