# Проекты с диска D:\1PythonProjects (автосбор)

Источник: README и docs подпроектов (без .env и секретов). Портфолио-PDF лежат почти в каждой папке, см. конец файла.

## 20251105-ai-asist-deepseek — AI-ассистент компании «Информация о Болгарии» для Telegram, сайта WordPress и ВКонтакте
- Тип: ai_assistant
- Стек: Python, DeepSeek/OpenRouter, SQLite, Telegram, VK API, WordPress-плагин (PHP, vanilla JS), systemd
- Что сделано:
  - Общее ядро (core/) для трёх каналов: Telegram, виджет на WP-сайте, ВКонтакте.
  - Ответы по system prompt и FAQ-базе; переключение диалога на живого оператора.
  - Админ-панель в боте: FAQ (добавление вручную, по ссылке, из Excel), правка, поиск.
  - Собственный WP-плагин чат-виджета (WP Chatbot AI), общающийся с Python-сервером на VPS.
- Вероятно чужой код: нет

## 20251125BotForGroup — Telegram-бот модератор группы с DeepSeek
- Тип: telegram_bot
- Стек: Python, pyTelegramBotAPI, DeepSeek, SQLite, config.ini/.env
- Что сделано:
  - Анализ входящих сообщений через LLM: тип (вопрос, уточнение, ответ, small talk).
  - История в SQLite, антиспам, негативные фразы, уведомления и предложение виртуального помощника.
  - Админ-меню /admin для суперадминов и админов чата.
- Вероятно чужой код: нет

## 20260404MedVopros — MedVopros и VetVopros: справочные Telegram AI-боты (медицина и ветеринария) с RAG
- Тип: telegram_bot / ai_assistant
- Стек: Python, aiogram 3, FastAPI, Jinja2, PostgreSQL + pgvector, Alembic, OpenAI API, Docker Compose
- Что сделано:
  - RAG: индексация базы знаний, векторный поиск (cosine) по pgvector.
  - Веб-админка на FastAPI, биллинг (бесплатные лимиты, подписки, пакеты).
  - Полный комплект проектной документации (архитектура, БД, RAG, MVP, риски, чек-лист приёмки) и деплой на VPS.
  - Две версии: для людей и для питомцев.
- Вероятно чужой код: нет

## 20260609KworkCons — TG Sales AI Bot: внутренний AI-консультант отдела продаж
- Тип: telegram_bot / ai_assistant
- Стек: Python, aiogram 3, asyncio, Anthropic Claude, Google Sheets API, SQLite (aiosqlite), APScheduler, Docker
- Что сделано:
  - Ответы сотрудников по продукту, ценам и скриптам продаж из базы знаний в Google Sheets.
  - Планировщик синхронизации, деплой в Docker на VPS.
  - Заказ с Kwork (есть ТЗ и портфолио).
- Вероятно чужой код: нет

## 20260616-Rent — Rental Reviews BG: площадка проверенных отзывов об аренде жилья в Болгарии
- Тип: web_app + telegram_bot
- Стек: Next.js, TypeScript, Tailwind CSS, Supabase (PostgreSQL, Storage, RLS), Python 3.12, aiogram 3, Docker, Vercel/Cloudflare Pages
- Что сделано:
  - Отзывы подаются через Telegram-бот, публикуются на сайте после модерации.
  - Типы отзывов: объект, арендодатель, арендатор, агентство, управляющая компания.
  - SQL-миграции, RLS, справочники в Supabase.
- Вероятно чужой код: нет

## 20260706SendUser — админка массовых рассылок для подписчиков двух Telegram-ботов
- Тип: web_app / automation
- Стек: Python 3.12, FastAPI, Jinja2, HTMX, SQLite
- Что сделано:
  - Кампании, dry-run (проверка членства в чатах), тестовая отправка, рассылка с прогрессом в реальном времени.
  - Отложенная отправка (очередь), экспорт результата в CSV.
  - Логин, cookie-сессия, CSRF; базы ботов открываются только на чтение.
- Вероятно чужой код: нет

## 20260727TestHH1 — тестовые задания на вакансию: очистка товарного каталога CSV и интеграция с Ozon Seller
- Тип: automation
- Стек: Python 3.12, requests, python-dotenv, pytest, CSV (utf-8-sig)
- Что сделано:
  - catalog-cleanup: разбор свободного текста названий на поля (бренд, OEM, количество), нормализация цен, дедупликация, подробный лог решений.
  - maviko1: выгрузка товаров из Ozon Seller API (пагинация, повторы, батчи) в CSV и утренняя сводка в Telegram с пометкой заканчивающихся остатков.
  - Unit-тесты, логирование.
- Вероятно чужой код: нет

## 20260729EmailVal — валидатор email-адресов из базы AcyMailing
- Тип: automation
- Стек: Python, dnspython, chardet, tqdm, python-dotenv, pytest
- Что сделано:
  - Трёхуровневая проверка: синтаксис (RFC 5321, IDN), MX-записи с кэшем, SMTP RCPT TO без отправки письма.
  - Флаги опечаток домена и одноразовой почты, построчный лог для переписки с хостингом.
  - Тесты без обращения к сети.
- Вероятно чужой код: нет

## 20260729PlaginWP — InsurWP: калькулятор медицинской страховки для WordPress
- Тип: wordpress_plugin
- Стек: PHP, WordPress (shortcode, admin), JSON-тарифы, JS, composer; есть Telegram Mini App (npm)
- Что сделано:
  - Шорткод [insurwp_calculator], сравнение предложений UNIQA и Bulstrad Life по Болгарии и Шенгену.
  - Редактор тарифов в админке с валидацией, импорт цен из внешнего API с показом «было → стало».
  - Евро и левы по курсу, адаптивная вёрстка, светлая и тёмная темы.
- Вероятно чужой код: нет

## 20260807TranslatePress — WP Multilang Press (плагин мультиязычности по модели TranslatePress) и AcyMailing Cron Sender
- Тип: wordpress_plugin
- Стек: PHP, WordPress, composer, DOM-парсинг HTML, OpenAI API (ИИ-перевод), MySQL, PHPMailer, системный cron
- Что сделано:
  - Мультиязычность: 1 запись + N языковых URL + переводы строк в своих таблицах; перехват отрендеренного HTML и подстановка переводов.
  - Ручной и ИИ-перевод (OpenAI), пользовательская инструкция, техзадание и поэтапная разработка.
  - acym-cron-sender: независимая от лицензии отправка очереди AcyMailing через системный cron, атомарный захват строк, SMTP keep-alive, статистика в таблицы AcyMailing.
- Вероятно чужой код: нет

## 20260824KworkCasino — GOMEDA BET 350: Telegram-бот-витрина бонусов на турецком с трекингом источников
- Тип: telegram_bot
- Стек: Python 3.11, aiogram 3, SQLite (aiosqlite, WAL), pydantic-settings, Docker Compose
- Что сделано:
  - First-touch атрибуция по ?start=, учёт кликов через callback_data, UTM-метки к внешним ссылкам.
  - Админ-панель на кнопках: статистика по периодам, источники, экспорт CSV, редактирование текстов, ссылок и FAQ без перезапуска.
  - Заказ с Kwork.
- Вероятно чужой код: нет

## 20260827CommonBuy — Group Buys: бот совместных закупок в Telegram-группе
- Тип: telegram_bot
- Стек: Python, aiogram, SQLite, миграции, Docker (по разделам README)
- Что сделано:
  - Создание закупки диалогом в личке, публикация в группе, вступление кнопкой, автопересчёт итогов.
  - Правила и согласие перед созданием, бэкап и восстановление SQLite, инструкции по правам и Privacy Mode.
  - README на английском и русском.
- Вероятно чужой код: нет

## 20260905TestHHgames — gamesinsp: тестовое задание «AI Automation Engineer»
- Тип: parser / automation
- Стек: Python, APScheduler, PostgreSQL, парсинг Metacritic, LLM-резюме отзывов, разбор YouTube-обзора, веб-интерфейс
- Что сделано:
  - Раз в час забирает 20 новых игр с Metacritic, upsert по slug.
  - AI-резюме отзывов критиков и пользователей, разбор YouTube-обзора, оценки по платформам в отдельной таблице.
  - Веб-интерфейс: плитка, карточка, поиск, фильтры, сортировка, похожие игры, мониторинг обработки в реальном времени.
- Вероятно чужой код: нет

## 20260905kwork_assist-main — Kwork AI Assistant и Upwork AI Assistant: мониторинг бирж фриланса и генерация откликов
- Тип: automation / ai_assistant
- Стек: Python, FastAPI, aiogram 3, Playwright, BeautifulSoup/lxml, GigaChat API, APScheduler, SQLAlchemy, aiosqlite, Docker, pytest
- Что сделано:
  - Мониторинг заказов Kwork: 9 настраиваемых фильтров (AND/OR), пресеты, REST API, AI-скоринг релевантности 0-10.
  - Генерация откликов и подтверждение отправки в Telegram inline-кнопками.
  - work_assistant для Upwork: сбор из embedded-состояния страницы, фильтры, дешёвая модель для скоринга и сильная для черновика.
- Вероятно чужой код: нет (название папки «-main» похоже на скачанный zip, но README и код в стиле владельца; возможен форк, проверить при необходимости)

## 20260907telegram-mcp-master — MCP-сервер для подключения личного Telegram-аккаунта к Claude Code
- Тип: automation (MCP-сервер)
- Стек: Python 3.10+, Telethon, mcp, qrcode, python-dotenv
- Что сделано:
  - Чтение чатов, отправка сообщений, подсчёт активности через личный аккаунт; вход по QR-коду.
  - Подключение как stdio-MCP в Claude Code.
- Вероятно чужой код: да (папка «-master» + zip, README как у публичной утилиты; авторство владельца не подтверждено)

## 20260910news-post — BG News Monitor: отбор важных новостей Болгарии для русскоязычной аудитории
- Тип: parser / ai_assistant
- Стек: Python, планировщик, адаптеры парсинга сайтов, OpenRouter (LLM), Pydantic, Telegram
- Что сделано:
  - Опрос источников, нормализация URL, дедупликация по URL и хешу содержимого, детерминированные фильтры до вызова AI.
  - LLM оценивает и пишет русский черновик; код-гарды отклоняют дословное копирование и числа, которых нет в источнике.
  - Публикация в канал только после подтверждения админом кнопкой.
- Вероятно чужой код: нет

## 20260915CalcEU — BGINFO Schengen Calculator: калькулятор правила 90/180
- Тип: wordpress_plugin
- Стек: PHP, WordPress (shortcode), JavaScript (расчёт в браузере)
- Что сделано:
  - Проверка пребывания, планирование поездки, поиск ближайшей даты въезда, нарушения по истории.
  - Расчёт в датах UTC, без зависимости от часового пояса и перехода на летнее время; слияние пересекающихся поездок.
- Вероятно чужой код: нет

## 20260917bginfodocs — DocPilot: локальный Document Engine и бенчмарк Vision-моделей для административных документов
- Тип: ai_assistant / automation (CLI)
- Стек: Python, PyMuPDF, Pillow, Pydantic (strict), Vision LLM
- Что сделано:
  - Классификация документа в 7 типов и извлечение только разрешённых полей; одна попытка repair; статусы processed/manual_review/unsupported/failed.
  - Детерминированные проверки без AI; Procedure Engine (чек-лист по анкете и документам).
  - Пакетный бенчмарк с защитой персональных данных (документы не сохраняются и не логируются).
- Вероятно чужой код: нет

## 20260919LangHelper — Vira: ситуативный тренажёр иностранных языков в Telegram (голос)
- Тип: telegram_bot / ai_assistant
- Стек: Python, Telegram, Supabase (БД и хранилище аудио), OpenRouter, ElevenLabs, Redis, платежи, Docker, Caddy, VPS Ubuntu
- Что сделано:
  - Диалоговые сценарии с голосом, отдельные боты по языкам (BG, EN, PL, TR), миграции и seed.
  - Платежи, админ-панель, инструкция добавления нового языка.
  - Документация: QUICK_START, схема draw.io, модели и стоимость, деплой.
- Вероятно чужой код: нет

## 20260924upwork — upwork-scout: локальный триаж вакансий Upwork
- Тип: parser / automation
- Стек: Python 3.11+, Playwright (Chrome), BeautifulSoup, SQLite, Jinja2, PyYAML, Pydantic, httpx, OpenRouter, pytest
- Что сделано:
  - Автообнаружение сохранённых поисков, сбор карточек, слияние дублей по ID, жёсткие правила и оценка 0-100.
  - Перевод названий и описаний на русский, HTML-отчёт; режим inbox без автоматизации браузера.
  - Только чтение, отклики не отправляются.
- Вероятно чужой код: нет

## bginfoMiniApp — Telegram Mini App, навигатор по разделам сайта bginfo.eu
- Тип: web_app
- Стек: React, Vite, TypeScript, Telegram WebApp API, react-router-dom, CSS
- Что сделано:
  - SPA-справочник по темам (ВНЖ, бизнес, страховки), переход на страницы сайта.
  - Статическая сборка для любого хостинга.
- Вероятно чужой код: нет

## ai-dev-team — локальная инфраструктура команды ИИ-агентов (Lead, Developer, QA, Reviewer, Release)
- Тип: automation (мета-инструменты для разработки)
- Стек: PowerShell, Claude Code (.claude/agents), Codex (.codex), Markdown-процессы
- Что сделано:
  - Правила ролей, конфигурации агентов, скрипты запуска Developer/QA, диагностика check-team.ps1.
  - Журнал решений, текущая задача, архитектура.
- Вероятно чужой код: нет

## agents_cc_codex — рецепт мультиагентной оркестрации в Claude Code (Opus + Sonnet-сабагенты + Codex-ревьюер + Jev-маршрутизация)
- Тип: automation (конфигурация и документация)
- Стек: Claude Code, Codex, Jev (API), shell/PowerShell-скрипты
- Что сделано:
  - Маршрутизация моделей и skills, гейт рискованных действий, правила ревью.
- Вероятно чужой код: нет

## KML-TEST — пустая папка
- Тип: неизвестно
- Стек: нет данных
- Что сделано:
  - Файлов нет.
- Вероятно чужой код: нет

## Найденные файлы резюме/портфолио
Явных resume/CV/резюме не найдено. Есть Kwork-портфолио в PDF (по одному-три на проект):
- D:\1PythonProjects\20251105-ai-asist-deepseek\Portfolio_AI-Assistant-DeepSeek.pdf
- D:\1PythonProjects\20251105-ai-asist-deepseek\Portfolio_AI_Assistant_DeepSeek.pdf
- D:\1PythonProjects\20251125BotForGroup\Portfolio_Telegram_Group_Admin_Bot.pdf
- D:\1PythonProjects\20260404MedVopros\Portfolio_MedVopros.pdf
- D:\1PythonProjects\20260404MedVopros\Portfolio_VetVopros.pdf
- D:\1PythonProjects\20260404MedVopros\Portfolio_VetVopros1.pdf
- D:\1PythonProjects\20260609KworkCons\TG_Sales_AI_Bot_Kwork_Portfolio.pdf
- D:\1PythonProjects\20260616-Rent\SMM\Rental_Reviews_BG_Kwork_Portfolio.pdf
- D:\1PythonProjects\20260727TestHH1\Portfolio_Ochistka_tovarnogo_kataloga_CSV.pdf
- D:\1PythonProjects\20260727TestHH1\Portfolio_Ozon_Seller_Automation.pdf
- D:\1PythonProjects\20260729EmailVal\Portfolio_Email_Validator_AcyMailing.pdf
- D:\1PythonProjects\20260807TranslatePress\Portfolio_WP_Multilang_Press.pdf (+ _.pdf, _EN.pdf)
- D:\1PythonProjects\20260824KworkCasino\Portfolio_GOMEDA_BET_350.pdf
