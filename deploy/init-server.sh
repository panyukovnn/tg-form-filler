#!/bin/bash

SSH_CONFIG=nvpnt
REMOTE_DIR=tg-form-filler

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Ошибка: не найден .env рядом со скриптом."
  exit 1
fi

echo "Создание директории $REMOTE_DIR на сервере..."
ssh "$SSH_CONFIG" "mkdir -p $REMOTE_DIR"

echo "Отправка .env на сервер..."
scp "$ENV_FILE" "$SSH_CONFIG:$REMOTE_DIR/.env"

echo "Готово."
