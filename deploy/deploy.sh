#!/bin/bash

SSH_CONFIG=nvpnt
REMOTE_DIR=tg-form-filler

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Создание директории $REMOTE_DIR на сервере (если не существует)..."
ssh "$SSH_CONFIG" "mkdir -p $REMOTE_DIR"

if ! ssh "$SSH_CONFIG" "[[ -f $REMOTE_DIR/.env ]]"; then
  ENV_FILE="$SCRIPT_DIR/.env"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Ошибка: .env не найден на сервере и отсутствует в deploy/.env локально."
    exit 1
  fi
  echo "Отправка .env на сервер (первоначальная настройка)..."
  scp "$ENV_FILE" "$SSH_CONFIG:$REMOTE_DIR/.env"
fi

echo "Синхронизация файлов проекта на сервер..."
rsync -av --delete \
  --exclude='.env' \
  --exclude='deploy/' \
  "$PROJECT_DIR/" "$SSH_CONFIG:$REMOTE_DIR/"

echo "Запуск приложения через docker-compose..."
ssh "$SSH_CONFIG" "cd $REMOTE_DIR && docker compose up -d --build"

echo "Готово."
