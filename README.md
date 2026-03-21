# tg-diary-form-filler

Telegram-бот, который принимает сообщение от администратора, извлекает данные с помощью LLM и заполняет Google форму.

## Настройка

Скопируй `.env.example` → `.env` и заполни переменные:

```
DEEPSEEK_API_KEY=
TG_BOT_TOKEN=
TG_ADMIN_CHAT_ID=
```

Скопируй `form_config.example.json` → `form_config.json` и заполни своими данными.

## Локальный запуск

```bash
pip install -r requirements.txt
python main.py
```

## Деплой на сервер

На сервере нужен только Docker. SSH-алиас — `nvpn`.

**Первый раз** — отправить конфиг:
```bash
./deploy/init-server.sh
```

**Обновление кода и перезапуск:**
```bash
./deploy/deploy.sh
```
