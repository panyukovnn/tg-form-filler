#!/bin/bash

# Конфигурация
SSH_CONFIG=nvpnt
REMOTE_DIR=tg-diary-form-filler

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SOURCE_FILES=(
  "$PROJECT_DIR/Dockerfile"
  "$PROJECT_DIR/requirements.txt"
  "$PROJECT_DIR/main.py"
  "$PROJECT_DIR/bot.py"
  "$PROJECT_DIR/form_filler.py"
  "$PROJECT_DIR/llm_handler.py"
)

echo "Проверка наличия исходных файлов..."
for f in "${SOURCE_FILES[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "Ошибка: файл не найден: $f"
    exit 1
  fi
done

echo "Копирование исходных файлов на сервер..."
scp "${SOURCE_FILES[@]}" "$SSH_CONFIG:$REMOTE_DIR/"

echo "Запуск приложения через docker-compose..."
ssh "$SSH_CONFIG" "cd $REMOTE_DIR && docker compose up -d --build"

echo "Готово."
