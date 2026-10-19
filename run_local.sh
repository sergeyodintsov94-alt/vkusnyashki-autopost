#!/bin/sh
# Запуск на своём компьютере (Mac/Linux): добавьте в cron строку
#   */15 * * * * /путь/к/папке/run_local.sh >> /путь/к/папке/autopost.log 2>&1
cd "$(dirname "$0")" && python3 publish.py run
