# Выкатка, обновление, откат, мониторинг и бэкапы

Целевая среда: Ubuntu VPS, Docker, путь `/opt/search-vac-tg`, сервис `search-vac-tg` (имя из `docker-compose.yml`, `container_name` не задан). Серверные команды (bash) идут первыми, команды для локального Windows (PowerShell) подписаны отдельно. Быстрый старт: [QUICK_START.md](QUICK_START.md), подробности: [../README_RU.md](../README_RU.md).

## 1. Первая установка

Сервер (bash):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo git clone https://github.com/Lightwell-bg/search-vac-tg /opt/search-vac-tg
sudo chown -R "$USER":"$USER" /opt/search-vac-tg
cd /opt/search-vac-tg
cp .env.example .env
nano .env
mkdir -p data materials
sudo chown -R 1000:1000 data
sudo chmod 700 data
sudo docker compose build
sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py
sudo docker compose up -d --build
```

В `.env` заполните `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `NOTIFY_BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (где взять: [QUICK_START.md](QUICK_START.md), шаг 2). Вход в Telegram интерактивный: номер телефона, код из Telegram, пароль 2FA. После него появляется `data/telegram.session` (секрет).

Проверка:

```bash
sudo docker compose ps
sudo docker compose logs --tail=50
```

В `docker compose ps` у сервиса должно быть состояние `healthy` (первые ~3 минуты `starting`), в логе строка `monitoring @FreelanceBay`, а в боте после запуска приходит сообщение «✅ Бот запущен». Отправьте боту `/menu`.

## 2. Обновление

Локально (PowerShell):

```powershell
git add -A
git commit -m "Описание изменений"
git push
```

Сервер (bash):

```bash
cd /opt/search-vac-tg
sudo git pull
sudo docker compose up -d --build
```

Проверка:

```bash
sudo docker compose ps
sudo docker compose logs --tail=50
```

Ожидается `healthy` и сообщение «✅ Бот запущен (версия …)» в боте. Новые переменные `.env` (если появились) добавьте до `up -d`: `.env` не лежит в git и сам не обновляется; сверяйте с `.env.example`.

## 3. Откат

Сервер (bash):

```bash
cd /opt/search-vac-tg
git log --oneline -5
sudo git checkout ХЭШ_КОММИТА
sudo docker compose up -d --build
sudo docker compose ps
```

Вместо `ХЭШ_КОММИТА` подставьте хэш из вывода `git log` (первые 7 символов). Чтобы вернуться на актуальную версию: `sudo git checkout main && sudo git pull && sudo docker compose up -d --build`.

Если откатываемая версия меняла структуру базы, восстановите и базу из бэкапа, сделанного до обновления (раздел 5).

## 4. Мониторинг

- **Healthcheck Docker** (`python -m src.health check`, каждые 60 с, 3 неудачи подряд = `unhealthy`). Проверяет файл пульса `data/heartbeat.json` (путь в `config.ini [paths] heartbeat_file`): приложение пишет его раз в минуту. Нездоров, если файл не читается или старше 180 с (процесс завис), либо опрос каналов не завершался дольше `2 × период проверки + 10 мин`.
- Состояние:

  ```bash
  sudo docker compose ps
  sudo docker inspect --format '{{.State.Health.Status}}' $(sudo docker compose ps -q search-vac-tg)
  sudo docker compose exec search-vac-tg python -m src.health check
  ```

  Ожидается `healthy` / код выхода 0. Содержимое пульса: `sudo cat data/heartbeat.json`.
- **Оповещения в боте** (вкл/выкл: «⚙️ Настройки → 🔔 Оповещения», применяется сразу): запуск и остановка бота, сбои опроса каналов 3 раза подряд, частые ошибки JEV и OpenRouter (5 за 30 минут), неудачный бэкап, повреждение базы при старте. Повторные оповещения одного вида не чаще раза в час.
- **Главное меню** `/menu` — это панель состояния: уведомления, каналы, последняя проверка, сбои опроса, последний бэкап.

## 5. Бэкапы

- Где: `data/backups` на сервере (`config.ini [backup] dir`). Файлы `app-ГГГГММДД-ЧЧММСС.db` (копия базы) и `session-….session` (сессия Telegram). В чат сессия никогда не отправляется.
- Когда: раз в сутки в `[backup] hour` по локальному времени (`[ui] timezone`, по умолчанию 04:00 Europe/Sofia). Вручную: «⚙️ Настройки → 💾 Бэкапы → 💾 Сделать бэкап сейчас».
- Сколько хранить: «Хранить последних» 3 / 7 / 14 / 30 на том же экране (по умолчанию `[backup] keep = 7`), применяется сразу. Старые копии удаляются автоматически.

Восстановление, сервер (bash):

```bash
cd /opt/search-vac-tg
sudo docker compose stop
sudo docker compose run --rm search-vac-tg python scripts/restore_backup.py --list
sudo docker compose run --rm search-vac-tg python scripts/restore_backup.py data/backups/ИМЯ_ФАЙЛА.db --force
sudo docker compose up -d
sudo docker compose ps
```

`ИМЯ_ФАЙЛА.db` берётся из вывода `--list` (например `app-20261006-040000.db`). Чтобы вместе с базой вернуть сессию Telegram, добавьте `--session data/backups/ИМЯ_СЕССИИ.session`. Перед заменой текущая база (и сессия, если вы её возвращаете) копируется «сырым» копированием файлов, а не через sqlite (поэтому работает и с повреждённой базой), вместе с файлами `-wal`/`-shm`/`-journal`: `data/app.db.before-restore-<время>` (+ суффиксы `-wal`, `-shm`). Сбой этого копирования не блокирует восстановление. Скрипт отказывается работать, пока пульс свежий (младше 180 с): сразу после `docker compose stop` он ещё свежий, поэтому в команде стоит `--force` (сервис вы уже остановили сами). Если не остановить сервис, запись в базу во время восстановления испортит её.

Копирование бэкапов с сервера на свой компьютер, локально (PowerShell):

```powershell
scp -r ПОЛЬЗОВАТЕЛЬ@АДРЕС_СЕРВЕРА:/opt/search-vac-tg/data/backups ./server-backups
```

Подставьте своего пользователя и адрес сервера (те же, что вы используете для `ssh`). Файл `session-….session` даёт полный доступ к аккаунту Telegram: храните его приватно, не коммитьте и не пересылайте.

## 6. Логи

- Файл приложения `data/app.log`: ротация по 5 МБ, 3 архивные копии (`[logging] max_bytes`, `backup_count`).
- Логи Docker: `json-file`, 10 МБ × 3 (`docker-compose.yml`).

```bash
sudo docker compose logs --tail=100
sudo docker compose logs -f
tail -n 100 data/app.log
```
