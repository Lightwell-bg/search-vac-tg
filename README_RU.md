# search-vac-tg

Следит за публичными Telegram-каналами с фриланс-заказами, отбирает их под профиль одного разработчика и присылает только подходящие в ваш личный Telegram-бот.

Документация: [docs/QUICK_START.md](docs/QUICK_START.md) (быстрый старт) | [docs/architecture.md](docs/architecture.md) | [PROMPTS.md](PROMPTS.md) (вопросы JEV и промпты OpenRouter) | [README.md](README.md) (английская версия)

## Что делает

Пользовательская сессия Telethon читает публичные каналы (по умолчанию `@FreelanceBay`). Каждый пост нормализуется, проверяется на дубли и проходит дешёвый детерминированный фильтр правил, затем JEV: дешёвую модель с типизированными ответами ACCEPT / REJECT / REVIEW. Дорогая LLM через OpenRouter вызывается только для REVIEW (или если JEV не ответил). Для подходящих заказов определяется контакт (без оплаты и без вступления куда-либо), заказ сохраняется в SQLite и приходит вам карточкой через aiogram-бота с кнопками: открыть пост, контакт, обратная связь и «Сделать отклик» (черновик; отправляете вы сами, автоматически ничего не отправляется).

```
каналы (публичные, config/channels.yaml)
   |
   v
Telethon (события новых сообщений + периодический опрос)
   |
   v
нормализация -> дедуп (хэш + fuzzy) -> правила (config/filter.yaml)
                                            |
                              reject <------+------> pass
                                                       |
                                                       v
                                                     JEV (API типизированных решений)
                                     ACCEPT / REJECT / REVIEW  (или ошибка JEV)
                                       |        |        |
                                       |        |        +--> OpenRouter review (только для REVIEW / ошибки JEV)
                                       |        v                     |
                                       |     отброшено                |
                                       v                              v
                                    финальное решение (fit score >= NOTIFY_SCORE) <-+
                                                       |
                                                       v
                                            contact resolver
                                                       |
                                                       v
                                                    SQLite
                                                       |
                                                       v
                                        aiogram-бот -> карточка заказа владельцу
```

## Стек

- Python 3.12, asyncio
- Telethon (пользовательская сессия MTProto), aiogram 3 (бот уведомлений)
- SQLAlchemy 2 (async) + aiosqlite (SQLite)
- httpx (HTTP к JEV и OpenRouter), pydantic, PyYAML, python-dotenv, RapidFuzz (fuzzy-дедуп)
- pypdf, python-docx (сборка профиля)
- pytest, pytest-asyncio, respx (тесты)
- Docker / docker compose

## Структура проекта

```
search-vac-tg/
  src/
    main.py                     точка входа: монитор + бот в одном event loop; build_pipeline()
    config.py                   настройки из .env + config.ini, загрузка каналов, логирование
    telegram/
      client.py                 обёртка Telethon: сессия, чтение каналов, клик по callback, refetch
      listener.py               обработчик NewMessage + цикл опроса + цикл повторов
      parser.py                 нормализация постов, извлечение контактов/бюджета/кнопок
      contact_resolver.py       определение контакта и обнаружение платного контакта
    filtering/
      pipeline.py               весь поток обработки, статусы, повторы
      rules.py                  предфильтр по ключевым словам (config/filter.yaml)
      deduplicator.py           поиск дублей: хэш + RapidFuzz
      scorer.py                 итоговый fit score из правил + JEV + OpenRouter
    jev/
      client.py                 HTTP-клиент JEV System One API
      classifier.py             три типизированных вопроса и guard-правила
      schemas.py                JevDecision / JevError / JevUsage
    llm/
      openrouter.py             клиент OpenRouter (review, черновик отклика, доработка профиля)
      prompts.py                промпты OpenRouter
      schemas.py                ReviewResult / LlmError / LlmUsage
    profile/
      builder.py                сборка data/profile.json и data/profile.md из materials/
      loader.py                 загрузка профиля, компактный вид, временный профиль
      matcher.py                подбор проектов под заказ
    bot/
      bot.py                    NotifyBot: отправка карточек, запуск диспетчера
      handlers.py               /start, /stats, обратная связь и отклик (только владелец)
      cards.py                  текст карточки заказа (с HTML-экранированием)
      keyboards.py              inline-клавиатура карточки
      stats_format.py           текст статистики
    db/
      database.py               async-движок и сессия
      models.py                 ORM-модели и JobStatus
      repository.py             все запросы к БД
  config/
    channels.yaml               каналы для мониторинга
    filter.yaml                 списки ключевых слов фильтра правил
  config.ini                    некритичные константы
  materials/                    резюме, портфолио, README проектов для профиля
  scripts/
    telegram_login.py           интерактивный вход, создаёт файл сессии
    rebuild_profile.py          пересборка профиля из materials/
    dry_run.py                  прогон реального пайплайна на примерах без Telegram
    stats.py                    вывод статистики из БД
  tests/                        тесты pytest
  docs/                         QUICK_START.md, architecture.md, PROGRESS.md
  data/                         рантайм-файлы: app.db, telegram.session, profile.*, app.log (в .gitignore)
  Dockerfile, docker-compose.yml
  requirements.txt, requirements-dev.txt
  .env.example                  шаблон секретов (реальные значения только в .env)
  PROMPTS.md                    вопросы JEV и промпты OpenRouter
```

## Установка и запуск на VPS (Docker)

Всё работает на Ubuntu VPS в Docker; на сервере нужны только Docker и git. Пошаговая версия только с командами: [docs/QUICK_START.md](docs/QUICK_START.md). Путь на сервере: `/opt/search-vac-tg`, сервис в `docker-compose.yml`: `search-vac-tg`.

1. Docker и код (URL репозитория возьмите на GitHub: кнопка Code → SSH):
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   REPO_URL=
   ```
   Вставьте скопированный URL после `REPO_URL=` и выполните:
   ```bash
   cd /opt && sudo git clone "$REPO_URL" search-vac-tg
   cd /opt/search-vac-tg
   ```
2. Секреты: `cp .env.example .env && nano .env`. Где взять каждый ключ, см. раздел «Сторонние сервисы и API-ключи» ниже. Сохранить в nano: Ctrl+O, Enter, Ctrl+X.
3. Каталоги данных: `mkdir -p data materials && sudo chown -R 1000:1000 data && chmod 700 data`. Контейнер работает от uid 1000, поэтому `data/` должна быть ему доступна на запись и закрыта для остальных.
4. Сборка: `sudo docker compose build`.
5. Проверка `.env` (ожидается `OK`):
   ```bash
   sudo docker compose run --rm search-vac-tg python -c "from src.config import load_settings; m=load_settings().missing_for_run(); print('OK' if not m else 'Не заполнено: '+', '.join(m))"
   ```
6. Вход в Telegram (один раз; номер, код из Telegram, пароль 2FA): `sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py`. Файл `data/telegram.session` появится на хосте (это секрет).
7. Пробный прогон (необязательно; реальные JEV/OpenRouter, без Telegram; пишет `data/dry_run.db`): `sudo docker compose run --rm search-vac-tg python scripts/dry_run.py`.
8. Запуск: `sudo docker compose up -d`, затем `sudo docker compose logs -f`: в логе строка `monitoring @FreelanceBay`; в боте отправьте `/stats`.

Повседневные команды (из `/opt/search-vac-tg`):

```bash
# обновление
sudo git pull && sudo docker compose up -d --build
# статистика (то же, что /stats в боте)
sudo docker compose exec search-vac-tg python scripts/stats.py
# логи / перезапуск / остановка
sudo docker compose logs -f
sudo docker compose restart
sudo docker compose down
```

- Каналы и фильтр: отредактируйте `config/channels.yaml` / `config/filter.yaml` на сервере (`nano`), затем `sudo docker compose restart`.
- Профиль: готовый `data/profile.json` и `data/profile.md` лежат в git и приезжают с `git clone`. Пересобирать на сервере нужно только после того, как вы положили свои файлы в `/opt/search-vac-tg/materials` на хосте (каталог смонтирован в контейнер только для чтения, внутри контейнера его не заполнить): `sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py && sudo docker compose restart`.
- Тома (`docker-compose.yml`): `./data` (база, сессия, профиль, логи; запись), `./config`, `./materials` и `./config.ini` (только чтение). Секреты берутся из `.env` (`env_file`). Файл сессии автоматически получает права 0600. `.dockerignore` не пускает в образ `.claude/`, `materials/private/`, `*.session` и `data/`. Логи контейнера ротируются (10 МБ x 3). Отдельный контейнер для JEV не нужен.
- Бэкап (онлайн, без остановки; нужен `sudo apt install -y sqlite3`):
  ```bash
  sudo sqlite3 data/app.db ".backup data/backup-$(date +%F).db"
  ```
  либо остановить (`sudo docker compose down`), `sudo cp data/app.db data/backup-$(date +%F).db` и запустить снова. Делайте бэкап и файла `data/telegram.session`: это секрет с полным доступом к аккаунту, храните приватно и не коммитьте. Восстановление: остановите сервис, верните `app.db` и `telegram.session` в `data/`, запустите сервис.
- Сеть: нужен только исходящий HTTPS до `openrouter.ai` и Telegram. Входящие порты не нужны.

## Как используется JEV

JEV: типизированный API TypeSafe «System One». Вместо свободного текста он возвращает типизированные ответы с реальными вероятностями и уверенностью (confidence).

- Боевой вызов: `POST https://openrouter.ai/api/v1/systemone`, модель `~typesafe/jev-latest` (используется тот же `OPENROUTER_API_KEY`; URL = `base_url` из `config.ini` `[openrouter]` + `/systemone`). Модель задаётся в `config.ini` `[jev] model`.
- Один вызов на заказ, три типизированных вопроса: `decision` (выбор accept / reject / review), `fit` (оценка 0-3, переводится в 0-100) и `category` (выбор категории). Точные тексты в [PROMPTS.md](PROMPTS.md).
- Отправляются только компактный профиль и нормализованный текст заказа (не больше `[jev] max_text_chars` = 3000 символов).
- Guard-правила (`src/jev/classifier.py`): ACCEPT или REJECT с confidence ниже `[jev] min_confidence` (0.70) становится REVIEW; ACCEPT с fit ниже 1.5 из 3 становится REVIEW; REJECT с fit 2.0 и выше из 3 становится REVIEW (ответы противоречат друг другу).
- Стоимость: около $0.00003 за заказ (оценка; фактическая стоимость каждого вызова пишется в таблицу `jev_usage` и видна в `/stats`).
- JEV: удалённый HTTPS API. Локального рантайма и отдельного контейнера нет; серверу нужен только исходящий HTTPS до `openrouter.ai`.
- Инструмент разработки (приложением в рантайме не используется): `~/.claude/scripts/ojc/jev-route.sh` маршрутизирует подзадачи кодинга по моделям через тот же JEV.

### Когда вызывается OpenRouter, а когда нет

Вызывается:
- JEV вернул REVIEW (включая ACCEPT/REJECT, понижённые guard-правилами);
- JEV не ответил (таймаут, ошибка HTTP, невалидный ответ) и `JEV_FALLBACK_TO_OPENROUTER=true` (маршрут `JEV error → OpenRouter`);
- вы нажали «Сделать отклик» в боте (черновик отклика) и `scripts/rebuild_profile.py --llm` (доработка профиля).

Не вызывается:
- фильтр правил отклонил пост (слишком короткий, только негативные слова, нет признаков заказа);
- пост дубликат;
- JEV вернул ACCEPT или REJECT с достаточной уверенностью;
- JEV не ответил и `JEV_FALLBACK_TO_OPENROUTER=false`: заказ откладывается со статусом `jev_unavailable` и повторяется позже.

## Fit score и пороги

- `NOTIFY_SCORE` (по умолчанию 65): минимальный итоговый балл для отправки заказа вам.
- `HIGH_FIT_SCORE` (по умолчанию 80): начиная с него заголовок карточки «🔥 Подходящий заказ», ниже «✅ Возможно подходит».

Формула (`src/filtering/scorer.py`):
- бонус правил = `min(10, rules_score // 10)`, где rules score = `попадания strong_positive * 25 + positive * 10 - negative * 5 - strong_negative * 15`, в пределах 0..100;
- JEV ACCEPT без OpenRouter: `score = min(100, jev_fit + бонус)`; принят, если `score >= NOTIFY_SCORE`;
- OpenRouter использован: `base = 0.7 * llm_fit + 0.3 * jev_fit` (просто `llm_fit`, если JEV не ответил), `score = min(100, base + бонус)`; принят, только если LLM тоже вернула `should_notify=true` и `score >= NOTIFY_SCORE`.

## Contact resolver

Работает только для заказов, уже признанных подходящими. Порядок:

1. Сначала проверка оплаты по всей клавиатуре: кнопка Buy, контактная кнопка с признаком оплаты («Контакт за 50 ⭐») или платёжная ссылка (`buy.stripe.com`, `boosty.to`, `/checkout`, `t.me/$...` ...) -> сразу `paid_contact`, даже если в тексте есть `@username`. Фразы «бесплатно», «оплата не требуется», «free» не считаются признаком оплаты.
2. Прямой контакт человека в тексте (username, t.me-ссылка на пользователя, email, телефон) -> `direct`.
3. URL-кнопки: ссылка на пользователя Telegram -> `direct`; бот, сайт, форма отклика -> `external_contact_flow` (никогда не `direct`).
4. Один клик по публичной callback-кнопке (вроде «Получить контакт»), только если канал разрешает клики (`click_callbacks`, см. «Как добавить канал»). Ответ / отредактированный пост снова проверяется на оплату; контакт человека в нём -> `free`; только веб-ссылка или бот -> `external_contact_flow`.

Статусы (`jobs.contact_status`):

| Статус | Значение |
|---|---|
| `direct` | контакт человека в тексте (username, t.me-ссылка на пользователя, email, телефон) или URL-кнопка на пользователя Telegram |
| `free` | контакт человека получен после бесплатного клика по callback-кнопке (из ответа или из отредактированного поста) |
| `external_contact_flow` | контакт выдаёт бот, сайт или форма (ссылка на бота / deep-link / url_auth-кнопка / веб-ссылка) |
| `paid_contact` | любой признак оплаты на пути к контакту (проверка оплаты идёт первой) |
| `contact_unknown` | callback-кнопки не дали контакта, клик не удался или не разрешён, кнопка с паролем не нажата, либо нет доступа к Telegram |
| `missing` | нет ни контакта, ни кнопок |
| `resolving` | служебная отметка, пишется перед нажатием кнопки (см. «Надёжность»); не итоговый результат |

Поддерживаемые типы кнопок: `callback` (нажимается один раз, если похожа на контактную, не просит пароль и канал разрешает клики), `url`, `buy` (считается платной), `url_auth` и deep-link ботов (`t.me/bot?start=...`).

Маркеры платного контакта: Telegram Stars, «звёзд», invoice, «оплат», «платн», «купить», «покупка», subscription, «подписка», premium, «тариф», «пополнить», «баланс», «недостаточно», top up, «контакт за N», ценники (`за 100 ₽/$/stars`), платёжные ссылки (`t.me/$...`, `t.me/invoice/...`) и кнопки Buy (полное регулярное выражение: `_PAYMENT_RE` в `src/telegram/contact_resolver.py`).

Сервис никогда не платит, не вступает в каналы и чаты и не отправляет пароль 2FA ботам (кнопка, требующая пароль, никогда не нажимается; контакт получает статус `contact_unknown`). На заказ делается максимум один клик по callback (надёжно: повтор после сбоя его не нажимает второй раз), клики идут по одному с паузой `[telegram] contact_click_delay_sec`. Обработчик callback это серверный код бота канала, и что он делает, мы не знаем, поэтому для канала, чей бот списывает кредиты или лимиты, поставьте `click_callbacks: false` (см. «Как добавить канал»).

## Надёжность

- Статусы заказа: `new` (теперь повторяется: заказ, прерванный исключением или сбоем, продолжается), `notifying` (отправка заявлена), `notify_uncertain` (сбой произошёл во время отправки: карточка могла дойти, автоматически она не отправляется повторно; проверьте чат с ботом), а также остальные из [docs/architecture.md](docs/architecture.md).
- Надёжные отметки перед побочными действиями: `contact_status='resolving'` пишется перед нажатием callback-кнопки, `status='notifying'` перед отправкой карточки. Повтор, нашедший отметку, действие не повторяет.
- Восстановление сирот: сообщение, сохранённое без заказа (сбой между сохранением и созданием заказа), подхватывается циклом повторов после 2 минут ожидания.
- Курсор опроса `last_message_id` сдвигается только за надёжно сохранённые посты; пост, который сохранить не удалось, останавливает пачку канала и запрашивается снова при следующем опросе (после 3 неудач подряд пишется `ERROR skipping poison message` и пост пропускается). Каналы, которые не удалось разрешить при старте, разрешаются заново каждый интервал повтора.
- Ответы JEV и OpenRouter любой формы проверяются: испорченный ответ (не те типы, NaN, confidence вне 0..1, fit вне 0..3) считается ошибкой JEV/OpenRouter, значения не «подрезаются» и приложение не падает.

`SHOW_PAID_CONTACT` (по умолчанию `false`): при `false` подходящий заказ с платным контактом получает статус `paid_skipped`, карточка не отправляется; при `true` карточка отправляется с пометкой «⚠️ платный контакт».

## Сторонние сервисы и API-ключи

Все секреты хранятся только в `.env` (он в `.gitignore`). На сервере: `cp .env.example .env && nano .env` (сохранить: Ctrl+O, Enter, Ctrl+X). Ключи получаются в браузере/Telegram на любом устройстве, затем вставляются в `nano` на сервере. Ниже подробная инструкция, где взять каждый ключ.

### 1. `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`

Нужны, чтобы сервис читал каналы от имени вашего Telegram-аккаунта.

1. Откройте https://my.telegram.org в браузере.
2. Введите номер телефона своего аккаунта в международном формате (например `+79161234567`), нажмите Next.
3. Код придёт в сам Telegram (чат «Telegram» с синей галочкой), не по SMS. Введите его на сайте.
4. Нажмите **API development tools**.
5. Если приложения ещё нет, заполните форму:
   - App title: `search-vac-tg`
   - Short name: `searchvactg` (5–32 символа, латиница/цифры)
   - URL: оставьте пустым
   - Platform: Desktop
   - Description: можно оставить пустым

   Нажмите **Create application**.
6. На странице появятся **App api_id** (число, например `12345678`) и **App api_hash** (строка из 32 символов).
7. Впишите их в `.env`:
   ```env
   TELEGRAM_API_ID=12345678
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   ```

Если форма выдаёт «ERROR» без объяснений, это известная проблема сайта Telegram: выключите VPN/прокси, попробуйте другой браузер или режим инкогнито, поменяйте App title и Short name.

**Никому не передавайте `api_hash`: вместе с файлом сессии он даёт доступ к аккаунту.**

### 2. Вход в аккаунт (один раз)

```bash
cd /opt/search-vac-tg
sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py
```

Скрипт спросит номер телефона, код из Telegram и пароль двухэтапной проверки (если включён). После входа появится `data/telegram.session`: это пропуск в аккаунт, в git он не попадает.

### 3. `NOTIFY_BOT_TOKEN`

Отдельный бот, который присылает вам вакансии.

1. Откройте https://t.me/BotFather и нажмите Start.
2. Отправьте `/newbot`.
3. Имя бота (отображается в чате): любое, например «Мои вакансии».
4. Username: латиница/цифры/`_`, обязательно заканчивается на `bot`, например `vlad_vacancies_bot`. Если занято, выберите другое.
5. Придёт токен вида `1234567890:AAH...` (~46 символов). Скопируйте его целиком.
6. Впишите в `.env`:
   ```env
   NOTIFY_BOT_TOKEN=1234567890:AAHxxxxxxxx...
   ```
7. **Обязательно** откройте своего бота (`t.me/<username>`) и нажмите Start: бот не может писать первым, без этого уведомления не придут.

Если токен утёк: BotFather → `/revoke` → выберите бота → получите новый токен.

### 4. `OWNER_TELEGRAM_ID`

Ваш числовой ID; бот отвечает только ему и шлёт уведомления только ему.

1. Откройте https://t.me/userinfobot и нажмите Start.
2. Он пришлёт `Id: 123456789`.
3. Впишите в `.env`:
   ```env
   OWNER_TELEGRAM_ID=123456789
   ```

### 5. `OPENROUTER_API_KEY` (через него работают и JEV, и OpenRouter-модель)

1. Зарегистрируйтесь на https://openrouter.ai.
2. Пополните баланс: https://openrouter.ai/settings/credits (хватит $5: JEV ≈ $0.00003 за вакансию, OpenRouter-модель вызывается редко).
3. Откройте https://openrouter.ai/keys → Create Key → имя `search-vac-tg` (лимит можно не задавать).
4. Скопируйте ключ `sk-or-v1-...`: он показывается один раз.
5. Впишите в `.env`:
   ```env
   OPENROUTER_API_KEY=sk-or-v1-...
   OPENROUTER_MODEL=google/gemini-2.5-flash-lite
   ```
   В `OPENROUTER_MODEL` можно указать любую модель из https://openrouter.ai/models; после смены перезапустите сервис.

JEV вызывается только через OpenRouter с тем же ключом; его модель задаётся в `config.ini` `[jev] model` (по умолчанию `~typesafe/jev-latest`).

### 6. Проверка

1. Все обязательные переменные заполнены:
   ```bash
   sudo docker compose run --rm search-vac-tg python -c "from src.config import load_settings; m=load_settings().missing_for_run(); print('OK' if not m else 'Не заполнено: '+', '.join(m))"
   ```
   Ожидается `OK`.
2. Пробный прогон на примерах:
   ```bash
   sudo docker compose run --rm search-vac-tg python scripts/dry_run.py
   ```
   Ожидается таблица из 8 вакансий и `JEV errors: 0`.
3. Запуск сервиса:
   ```bash
   sudo docker compose up -d && sudo docker compose logs -f
   ```
   В логе должна быть строка `monitoring @FreelanceBay`, а `/stats` в боте отвечает.

### Все переменные окружения

| Переменная | Обязательна | Значение |
|---|---|---|
| `TELEGRAM_API_ID` | да | id приложения Telegram с my.telegram.org |
| `TELEGRAM_API_HASH` | да | hash приложения Telegram |
| `NOTIFY_BOT_TOKEN` | да | токен бота уведомлений (BotFather) |
| `OWNER_TELEGRAM_ID` | да | числовой id единственного пользователя, с которым говорит бот |
| `OPENROUTER_API_KEY` | да (всегда) | ключ OpenRouter: нужен для решений REVIEW, fallback от JEV и черновиков откликов; используется и для JEV |
| `OPENROUTER_MODEL` | да (всегда) | id модели OpenRouter для review / черновиков откликов |
| `JEV_FALLBACK_TO_OPENROUTER` | нет | `true` (по умолчанию): при ошибке JEV идти в OpenRouter; `false`: отложить заказ как `jev_unavailable` |
| `NOTIFY_SCORE` | нет | минимальный балл для уведомления, по умолчанию 65 |
| `HIGH_FIT_SCORE` | нет | балл для заголовка «🔥», по умолчанию 80 |
| `SHOW_PAID_CONTACT` | нет | `false` (по умолчанию): заказы с платным контактом пропускаются |
| `DATABASE_URL` | нет | по умолчанию `sqlite:///data/app.db` |

`python -m src.main` не запустится и выведет список незаполненных обязательных переменных.

## Конфигурация

### config.ini (некритичные константы)

| Секция / ключ | По умолчанию | Значение |
|---|---|---|
| `[paths]` channels_file, filter_file, profile_file, profile_md_file, materials_dir, session_file | `config/channels.yaml`, `config/filter.yaml`, `data/profile.json`, `data/profile.md`, `materials`, `data/telegram` | расположение файлов (от корня проекта) |
| `[jev]` timeout_sec | 20 | таймаут запроса к JEV |
| `[jev]` min_confidence | 0.70 | ниже этого ACCEPT/REJECT становится REVIEW |
| `[jev]` max_text_chars | 3000 | лимит текста заказа, отправляемого в JEV |
| `[openrouter]` base_url | `https://openrouter.ai/api/v1` | база API OpenRouter |
| `[openrouter]` timeout_sec | 60 | таймаут запроса |
| `[openrouter]` max_retries | 2 | повторы при таймауте / 429 / 5xx |
| `[dedup]` fuzzy_threshold | 90 | схожесть (0-100), при которой пост считается дублем |
| `[dedup]` window_days | 14 | на какую глубину сравнивать |
| `[dedup]` max_candidates | 1000 | максимум прежних заказов для сравнения |
| `[telegram]` catchup_limit | 50 | сколько постов брать при первом запуске канала |
| `[telegram]` poll_interval_sec | 120 | как часто опрашиваются каналы |
| `[telegram]` contact_click_delay_sec | 2 | пауза перед кликом по callback |
| `[telegram]` flood_sleep_threshold | 60 | Telethon сам ждёт при FloodWait до этого числа секунд |
| `[pipeline]` min_text_length | 40 | более короткие посты отклоняются как `too_short` |
| `[pipeline]` retry_limit | 5 | максимум попыток для заказа с ошибкой |
| `[pipeline]` retry_interval_sec | 600 | как часто повторяются заказы с ошибкой |
| `[logging]` level, file | `INFO`, `data/app.log` | уровень и ротируемый файл лога (5 МБ x 3) |

### Как добавить канал

Отредактируйте `config/channels.yaml`; поддерживаются только публичные каналы (сервис никуда не вступает):

```yaml
channels:
  - username: FreelanceBay
    enabled: true
  - username: another_public_channel
    enabled: true
    click_callbacks: false
```

Формы `@name` и `https://t.me/name` тоже принимаются. Перезапустите сервис: `sudo docker compose restart`.

`click_callbacks` (по умолчанию `true`) определяет, может ли contact resolver нажимать callback-кнопку «получить контакт» этого канала. Обработчик кнопки это серверный код бота канала; поставьте `false` для канала, чей бот списывает кредиты/лимиты за нажатие. При `false` контакт берётся только из текста и URL-кнопок.

### config/filter.yaml

Списки ключевых слов предфильтра (в нижнем регистре):
- `strong_positive` (+25 к rules score за попадание), `positive` (+10): технические признаки;
- `negative` (-5), `strong_negative` (-15): нетехнические работы;
- `job_markers`: слова, показывающие, что это пост о заказе («нужен», «бюджет», «hiring»...);
- `ignore_contacts`: юзернеймы (админы каналов, рекламные подписи), которые нельзя принимать за контакт заказчика.

Семантика: пост отклоняется, если он короче `min_text_length`; если в нём только негативные слова и нет позитивных; или если нет ни позитивных слов, ни маркеров заказа. Всё остальное идёт в JEV; негативное слово рядом с позитивным не отклоняет пост. Два и более `strong_positive` без негативных пишутся в лог как `RULE_ACCEPT` (но всё равно идут в JEV). Слова ищутся по границе слова: кириллические принимают окончания, латинские допускают множественное `s`, `*` в конце означает префикс.

### Как поменять пороги и модель OpenRouter

- Пороги: измените `NOTIFY_SCORE` / `HIGH_FIT_SCORE` в `.env`, перезапустите (`sudo docker compose restart`).
- Модель OpenRouter: измените одну строку `OPENROUTER_MODEL=...` в `.env` и перезапустите сервис.

## Профиль

Профиль строится из ваших материалов и используется в решениях JEV/OpenRouter и для черновиков откликов.

1. Положите резюме, портфолио, README проектов, `links.txt` в `materials/` на хосте сервера (форматы: md, txt, pdf, docx; OCR нет). Файлы в `materials/private/` игнорируются git.
2. Дополнительные файлы или папки вне проекта: абсолютные пути, по одному на строку, в `materials/extra_paths.txt` (`#` = комментарий; несуществующие пути пропускаются).
3. Пересборка на сервере: `sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py` (пишет `data/profile.json` и `data/profile.md`).
   - `--dry-run` выводит сводку и ничего не пишет;
   - `--llm` дополнительно дорабатывает summary/services через OpenRouter (только компактные данные).
4. Если материалов нет, используется временный профиль (`provisional: true`), при старте пишется предупреждение.
5. После пересборки перезапустите сервис (`sudo docker compose restart`): профиль загружается при старте.

## Статистика и экономия

`/stats` в боте или `sudo docker compose exec search-vac-tg python scripts/stats.py` показывает: получено сообщений, дубли, отсев правилами, JEV: обработано / accept / reject / review / ошибки (и fallback), вызовы OpenRouter (review / прочие), уведомления, пропущенные платные контакты, обратную связь, оценку стоимости JEV и OpenRouter, заказы по статусам. Строка «OpenRouter вызван для N из M прошедших правила (X% сэкономлено)» показывает, сколько заказов JEV решил без дорогой модели.

## Безопасность

- Никаких платежей: сервис не вызывает платёжные методы; любой признак оплаты останавливает contact resolver.
- Никаких автоответов: черновик отклика это только текст для вас, заказчику ничего не отправляется.
- Никакого вступления в каналы и чаты; читаются только публичные каналы.
- Пароль 2FA никогда не отправляется ботам.
- Секреты только в `.env`; `.env` и `data/*.session` в `.gitignore` и не коммитятся.
- Бот игнорирует всех, кроме `OWNER_TELEGRAM_ID`.
- Недоверенный текст (текст заказа, контакты, ответы LLM) экранируется в карточках как HTML.

## Диагностика проблем

| Симптом | Что делать |
|---|---|
| `Telegram session is not authorized` | выполните `sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py` |
| `FloodWait Ns` в логе | лимит Telegram; сервис ждёт и повторяет тот же канал (до 3 попыток; неразрешённые каналы повторяются каждый интервал повтора). Короткие ожидания (до `flood_sleep_threshold`) Telethon обрабатывает сам; если часто, увеличьте `poll_interval_sec` |
| Бот ничего не присылает | один раз нажмите `/start` в своём боте; проверьте `NOTIFY_BOT_TOKEN`, `OWNER_TELEGRAM_ID` и `sudo docker compose logs` на `ERROR notification` (статус `notify_error`, повторяется автоматически) |
| `fill these variables in .env: ...` | заполните перечисленные переменные |
| `JEV_ERROR` в логе | заказ уходит в OpenRouter (если fallback включён) либо откладывается как `jev_unavailable` и повторяется каждые `retry_interval_sec` до `retry_limit` попыток; проверьте ключ, баланс и исходящий HTTPS до openrouter.ai |
| `ERROR OpenRouter review` | заказ получает `llm_error` и повторяется автоматически; проверьте `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, баланс |
| `no enabled channels` / `channel @x not found` | проверьте `config/channels.yaml`; канал должен быть публичным |
| `using PROVISIONAL profile` | добавьте материалы и выполните `sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py`, затем `sudo docker compose restart` |

## Локальная разработка (необязательно)

Нужна только для правки кода и тестов; боевой запуск идёт на VPS в Docker (см. выше). Виртуальное окружение (Windows):

```bash
python -m venv .venv
# Git Bash
source .venv/Scripts/activate
# PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
# для тестов
pip install -r requirements-dev.txt
```

Запуск локально (после заполнения `.env`): `python scripts/telegram_login.py`, `python scripts/dry_run.py`, `python -m src.main`.

Тесты:

```bash
pytest -q
# живой тест JEV (реальная сеть, копеечная стоимость, нужен OPENROUTER_API_KEY):
RUN_LIVE_JEV=1 pytest -q -m live
```
