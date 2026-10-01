# Быстрый старт (Ubuntu VPS, Docker)

Подробности, таблицы настроек и диагностика: [README_RU.md](../README_RU.md). Все команды выполняются на сервере по SSH. Сервис работает только в Docker; путь на сервере `/opt/search-vac-tg`, сервис `search-vac-tg`.

1. Установите Docker (если его нет) и скачайте код:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   cd /opt && sudo git clone https://github.com/Lightwell-bg/search-vac-tg.git
   cd /opt/search-vac-tg
   ```

2. Создайте `.env` и заполните его:
   ```bash
   cp .env.example .env && nano .env
   ```
   Сохранить в nano: Ctrl+O, Enter, Ctrl+X. Ключи получаются в браузере / Telegram на любом устройстве и вставляются в `nano` на сервере (подробнее: [README_RU.md](../README_RU.md#сторонние-сервисы-и-api-ключи)):

   **a) `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`**
   1. Откройте https://my.telegram.org, введите номер телефона (например `+79161234567`), Next.
   2. Код придёт в сам Telegram (чат «Telegram»), не по SMS. Введите его на сайте.
   3. Нажмите **API development tools**. Если приложения нет, заполните: App title `search-vac-tg`, Short name `searchvactg` (5–32 символа, латиница/цифры), URL пусто, Platform Desktop, Create application.
   4. Скопируйте App api_id (число) и App api_hash (32 символа):
      ```env
      TELEGRAM_API_ID=12345678
      TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
      ```
   5. Если форма выдаёт «ERROR»: выключите VPN/прокси, попробуйте другой браузер/инкогнито, поменяйте App title и Short name. `api_hash` никому не передавайте.

   **b) `NOTIFY_BOT_TOKEN`**
   1. Откройте https://t.me/BotFather, Start, отправьте `/newbot`.
   2. Имя бота: любое. Username: латиница/цифры/`_`, заканчивается на `bot` (например `vlad_vacancies_bot`).
   3. Скопируйте токен целиком:
      ```env
      NOTIFY_BOT_TOKEN=1234567890:AAHxxxxxxxx...
      ```
   4. Обязательно откройте своего бота (`t.me/<username>`) и нажмите Start: бот не может писать первым, без этого уведомления не придут.

   **c) `OWNER_TELEGRAM_ID`**
   1. Откройте https://t.me/userinfobot, Start, он пришлёт `Id: 123456789`.
      ```env
      OWNER_TELEGRAM_ID=123456789
      ```

   **d) `OPENROUTER_API_KEY` и `OPENROUTER_MODEL`** (ключ используется и для JEV)
   1. Зарегистрируйтесь на https://openrouter.ai и пополните баланс: https://openrouter.ai/settings/credits ($5 хватит).
   2. https://openrouter.ai/keys → Create Key → имя `search-vac-tg`. Ключ показывается один раз, скопируйте сразу:
      ```env
      OPENROUTER_API_KEY=sk-or-v1-...
      OPENROUTER_MODEL=google/gemini-2.5-flash-lite
      ```
      Другую модель выберите на https://openrouter.ai/models.

   Остальное по умолчанию. Модель JEV задаётся в `config.ini` `[jev] model`.

3. Подготовьте каталоги данных (контейнер работает от uid 1000):
   ```bash
   mkdir -p data materials && sudo chown -R 1000:1000 data && chmod 700 data
   ```

4. Соберите образ:
   ```bash
   sudo docker compose build
   ```

5. Проверьте, что все обязательные переменные заполнены (ожидается `OK`):
   ```bash
   sudo docker compose run --rm search-vac-tg python -c "from src.config import load_settings; m=load_settings().missing_for_run(); print('OK' if not m else 'Не заполнено: '+', '.join(m))"
   ```

6. Войдите в Telegram-аккаунт (один раз; спросит номер телефона, код из Telegram и пароль 2FA, если включён; на сервере появится `data/telegram.session`, это секрет):
   ```bash
   sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py
   ```
   +359877447225

7. (Необязательно) Пробный прогон на примерах (без Telegram, реальные JEV/OpenRouter; ожидается таблица из 8 вакансий и `JEV errors: 0`):
   ```bash
   sudo docker compose run --rm search-vac-tg python scripts/dry_run.py
   ```

8. Запустите сервис и проверьте лог:
   ```bash
   sudo docker compose up -d
   sudo docker compose logs -f
   ```
   В логе должна быть строка `monitoring @FreelanceBay` (выход из логов: Ctrl+C, сервис продолжит работать). В своём боте отправьте `/stats`, придёт статистика; подходящие заказы приходят карточками в бот.

9. Повседневные команды (из `/opt/search-vac-tg`):
   - Обновление:
     ```bash
     cd /opt/search-vac-tg && sudo git pull && sudo docker compose up -d --build
     ```
   - Статистика:
     ```bash
     sudo docker compose exec search-vac-tg python scripts/stats.py
     ```
   - Каналы и фильтры: отредактируйте `config/channels.yaml` / `config/filter.yaml` (`nano`), затем:
     ```bash
     sudo docker compose restart
     ```
   - Пересборка профиля. Готовый `data/profile.json` уже приезжает из репозитория. Пересобирайте, только если положили свои файлы в `/opt/search-vac-tg/materials` на сервере (в контейнере каталог смонтирован только для чтения):
     ```bash
     sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py && sudo docker compose restart
     ```
   - Бэкап базы и сессии (секрет, храните приватно):
     ```bash
     sudo apt install -y sqlite3
     sudo sqlite3 data/app.db ".backup data/backup-$(date +%F).db"
     sudo cp data/telegram.session data/telegram.session.bak
     ```

10. Дальше всё настраивается в боте: отправьте `/menu` (каналы, пороги, платные контакты, пауза, модель). Команда `/journal` показывает журнал проверок: что отправлено и почему отсеяно. Настройки хранятся в базе и перекрывают `.env` / `config/channels.yaml`, которые задают лишь начальные значения.
