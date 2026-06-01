# Автоматизация и деплой

Здесь два независимых компонента:

1. **Деплой web-сайта** через MkDocs Material + GitHub Pages — `docs.yml` workflow
2. **Инкрементальный sync новых сообщений** через Telethon + cron — `sync.py` и `sync.yml` workflow

Поднимай их **по одному**, в этом порядке. Сначала задеплой сайт (полезно само по себе), потом — автосинк.

---

## Phase 1: deploy сайта

Делаем по шагам. После каждого шага — сообщи мне, что готово, и я скажу следующий.

### Шаг 1. Создай приватный репо на GitHub
- Имя любое (например `dh-modno-archive`)
- Тип: **Private**
- Не добавляй README/license/gitignore — заллью своё

### Шаг 2. Залей содержимое архива
Распакуй zip, в корне получишь папку `tg_export/`. Содержимое **этой папки** (не саму папку) залей в репо. Через git:
```bash
cd tg_export
git init
git add -A
git commit -m "Initial archive"
git branch -M main
git remote add origin git@github.com:<твой-логин>/dh-modno-archive.git
git push -u origin main
```
Или через GitHub UI «Add file → Upload files» — но там придётся таскать ~120 файлов вручную, удобнее через git.

### Шаг 3. Включи GitHub Pages
В репо: **Settings → Pages → Build and deployment → Source: GitHub Actions**. Просто переключи в этот режим, никакие команды не нужны.

### Шаг 4. Дождись первой сборки
Зайди в **Actions** в репо. Увидишь workflow «Deploy docs» (запустится автоматически после push). Подожди 1–2 минуты пока он зелёный.

### Шаг 5. Открой сайт
URL появится в `Settings → Pages` сверху страницы: `https://<твой-логин>.github.io/dh-modno-archive/`. Откроется MkDocs Material с sidebar'ом, поиском, тегами.

---

## Phase 2: автосинк новых сообщений

После того как сайт работает.

### Шаг 1. API credentials
Зайди https://my.telegram.org → API development tools → создай приложение. Сохрани `api_id` и `api_hash`.

### Шаг 2. Session string
Локально (на компе):
```bash
pip install telethon
python .automation/sync.py --setup
```
Введёшь номер, код из Telegram. Скрипт положит `.session-string.txt` рядом. **Не коммить!** (`.gitignore` уже про него знает.)

### Шаг 3. Chat ID группы
Через бот [@username_to_id_bot](https://t.me/username_to_id_bot). Для супергрупп ID начинается с `-100`, например `-1002290953869`.

### Шаг 4. GitHub Secrets
В репо: **Settings → Secrets and variables → Actions → New secret**. Добавь 4 штуки:
- `TG_API_ID` — число из шага 1
- `TG_API_HASH` — строка из шага 1
- `TG_SESSION` — содержимое `.session-string.txt`
- `TG_GROUP_ID` — ID из шага 3 (с `-100`)

Опционально, во вкладке **Variables** (не Secrets) можно задать `TG_TZ` — таймзону, в которой показывать время сообщений (по умолчанию `Europe/Moscow`). См. пункт про таймзону ниже.

### Шаг 5. Начальная точка синка
В корне уже лежит `.sync-state.json` с последним ID исходного архива (`{"last_message_id": 79664}`) — синк продолжит ровно отсюда, ничего создавать не нужно. Если когда-нибудь захочешь пересобрать точку отсчёта:
```bash
grep -hoE '<a id="m[0-9]+"' general-archive/*.md topics/*.md | grep -oE '[0-9]+' | sort -n | tail -1
```

### Шаг 6. Включи Actions write permission
**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**

### Шаг 7. Проверь sync
**Actions → Sync Telegram → Run workflow → Run**. Workflow делает два шага: `sync.py` (тянет новые сообщения в `general-archive/` и топики) и `regen.py` (пересобирает сигнальную версию затронутых месяцев, навигацию, счётчики в README и теги). Если были новые сообщения — появится коммит «Sync: ...», и сайт пересоберётся сам.

Дальше синк работает сам, **каждый час**. Деплой сайта на каждый коммит — тоже автоматически. Холостые прогоны (новых сообщений нет) коммит не делают.

> **Про таймзону — проверь на первом прогоне.** Telegram отдаёт время в UTC, а исходный архив сделан в локальном времени. Синк переводит время в `TG_TZ` (по умолчанию `Europe/Moscow`). После первого реального синка открой свежее сообщение в архиве и сравни время с тем, что видишь в самом Telegram. Если разъехалось — поставь правильную зону в Variables → `TG_TZ` (например `Europe/Belgrade`).

---

## Troubleshooting

**Деплой упал.** Открой Actions → красный workflow → раскрой `Build site`. Скорее всего MkDocs нашёл сломанную ссылку. Скажи мне ошибку — поправлю.

**Sync упал.** Чаще всего — неправильные secrets. Проверь, что все 4 на месте и без пробелов.

**Sync не видит группу.** Userbot должен быть участником. Проверь, что зашёл в Telegram под нужным номером при шаге 2.

**Аккуратно с session string.** Это полный доступ к твоему Telegram. Если случайно закоммитил — немедленно отзови в `my.telegram.org → API → Reset` и сгенерируй новую.
