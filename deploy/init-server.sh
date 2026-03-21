#!/bin/bash

# Конфигурация
SSH_CONFIG=nvpnt
REMOTE_DIR=tg-diary-form-filler

# Проверка на наличие файлов рядом со скриптом
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
ENV_FILE="$SCRIPT_DIR/.env"
FORM_CONFIG_FILE="$SCRIPT_DIR/form_config.json"

if [[ ! -f "$COMPOSE_FILE" || ! -f "$ENV_FILE" || ! -f "$FORM_CONFIG_FILE" ]]; then
  echo "Ошибка: не найден docker-compose.yml, .env или form_config.json рядом со скриптом."
  exit 1
fi

echo "Создание папки $REMOTE_DIR на сервере (если не существует)..."
ssh "$SSH_CONFIG" "mkdir -p $REMOTE_DIR"

echo "Копирование файлов на сервер..."
scp "$COMPOSE_FILE" "$ENV_FILE" "$FORM_CONFIG_FILE" "$SSH_CONFIG:$REMOTE_DIR/"

echo "Готово. Файлы успешно отправлены."