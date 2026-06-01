"""Shared archive logic: rendering, routing, parsing, classification.

This module is the single source of truth for the on-disk format. Both the
incremental sync (sync.py) and the regeneration pass (regen.py) build on it, so
new real-time messages land in exactly the same format as the original snapshot.

No network here. Pure functions over message dicts and files on disk.

Canonical message dict:
    {
        'id': int,
        'date': 'YYYY-MM-DDTHH:MM:SS',   # 19 chars, local TG time
        'from': str,
        'text': str,
        'pinned': bool,
        'edited': bool,
        'topic_id': int | None,          # forum topic root id, else None
        'reply_to_id': int | None,
        'reply_to_name': str | None,     # quoted author (for the ↪ preview)
        'reply_to_text': str | None,     # quoted text   (for the ↪ preview)
        'media': None | {
            'kind': 'photo'|'video'|'audio'|'file',
            'filename': str | None,
            'size': int | None,          # bytes
            'ext': str | None,           # lowercase, no dot
        },
        'links': [str, ...],             # bare URLs found in text
    }
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
TG_BASE = 'https://t.me/daggerheart_ru'

TOPIC_IDS = {
    7348: '01-вопросы-по-правилам',
    7351: '02-файлы-полезное-и-хоумрулы',
    14289: '03-поиск-игроков',
    19690: '04-хоумрулы-неофициальные-механики',
    70924: '05-новости',
}

# ── tag rules ───────────────────────────────────────────────────────────────
# Structural tags come from media kind / flags; domain tags from links in text.
# Derived empirically from the existing tags/ files. label is the display name.
TAG_LABELS = {
    'photo': '🖼️ С фото', 'video': '🎬 Видео', 'telegram': '✈️ Telegram-ссылки',
    'image': '🎨 Изображения (файлом)', 'youtube': '▶️ YouTube', 'pdf': '📄 PDF-вложения',
    'google-docs': '📝 Google Docs / Drive', 'reddit': '🤖 Reddit',
    'daggerheart-official': '⚔️ Daggerheart Official', 'pinned': '📌 Закреплённые',
    'archive': '🗜️ Архивы (zip/rar/7z)', 'patreon': '💰 Patreon / Boosty',
    'vk': '🔵 ВКонтакте', 'freshcutgrass': '🌿 FreshCutGrass', 'demiplane': '🌐 Demiplane',
    'audio': '🎵 Аудио', 'yandex-disk': '☁️ Яндекс.Диск', 'dagger-heart-ru': '🇷🇺 dagger-heart.ru',
    'habr': '👨‍💻 Хабр', 'foundry': '🎲 Foundry VTT', 'github': '🐙 GitHub',
    'drivethru': '🎲 DriveThruRPG', 'kickstarter': '🚀 Kickstarter', 'steam': '🎮 Steam',
    'ozon': '📦 Ozon', 'dnd-su': '🐉 dnd.su',
}
# domain substring -> tag (first match wins per domain; a message can hit several)
DOMAIN_TAGS = [
    ('daggerheartdispatch.com', 'daggerheart-official'),
    ('daggerheart.com', 'daggerheart-official'),
    ('dagger-heart.ru', 'dagger-heart-ru'),
    ('youtube.com', 'youtube'), ('youtu.be', 'youtube'),
    ('docs.google.com', 'google-docs'), ('drive.google.com', 'google-docs'),
    ('reddit.com', 'reddit'),
    ('patreon.com', 'patreon'), ('boosty.to', 'patreon'),
    ('vk.com', 'vk'),
    ('freshcutgrass.app', 'freshcutgrass'),
    ('demiplane.com', 'demiplane'),
    ('disk.yandex', 'yandex-disk'),
    ('habr.com', 'habr'),
    ('foundryvtt.com', 'foundry'), ('foundry.ruleplaying.com', 'foundry'),
    ('github.com', 'github'),
    ('drivethrurpg.com', 'drivethru'),
    ('kickstarter.com', 'kickstarter'),
    ('steampowered.com', 'steam'),
    ('ozon.ru', 'ozon'),
    ('dnd.su', 'dnd-su'),
    ('t.me/', 'telegram'),
]
IMAGE_EXT = {'webp', 'png', 'jpg', 'jpeg', 'gif', 'bmp', 'svg', 'heic'}
ARCHIVE_EXT = {'zip', 'rar', '7z'}
AUDIO_EXT = {'mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac'}
VIDEO_EXT = {'mp4', 'mov', 'mkv', 'webm', 'avi'}

URL_RE = re.compile(r'https?://[^\s)>\]]+')


# ── small helpers ─────────────────────────────────────────────────────────────
def week_of(day):
    """Day-of-month -> week bucket 1..5 (1-7, 8-14, 15-21, 22-28, 29-end)."""
    return (day - 1) // 7 + 1


def month_of(date):
    return date[:7]


def human_size(nbytes):
    if nbytes is None:
        return ''
    if nbytes >= 1024 * 1024:
        return f'{round(nbytes / 1024 / 1024)} МБ'
    if nbytes >= 1024:
        return f'{round(nbytes / 1024)} КБ'
    return f'{nbytes} Б'


def fmt_date(date):
    """ISO 'YYYY-MM-DDTHH:MM:SS' -> 'DD.MM HH:MM'."""
    return f'{date[8:10]}.{date[5:7]} {date[11:16]}'


def tg_url(msg_id, topic_id=None):
    return f'{TG_BASE}/{topic_id}/{msg_id}' if topic_id else f'{TG_BASE}/{msg_id}'


def _collapse(text, limit):
    """Single-line preview, trimmed to <= limit chars on a word boundary + …"""
    t = ' '.join((text or '').split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(' ', 1)[0].rstrip()
    return (cut or t[:limit]) + '…'


def extract_links(text):
    return URL_RE.findall(text or '')


# ── classification ────────────────────────────────────────────────────────────
def tags_for(m):
    """Return the set of tag-keys a message belongs to (may be several)."""
    out = set()
    media = m.get('media')
    if media:
        kind, ext = media['kind'], (media.get('ext') or '')
        if kind == 'photo':
            out.add('photo')
        elif kind == 'video':
            out.add('video')
        elif kind == 'audio':
            out.add('audio')
        elif kind == 'file':
            if ext in IMAGE_EXT:
                out.add('image')
            elif ext == 'pdf':
                out.add('pdf')
            elif ext in ARCHIVE_EXT:
                out.add('archive')
            elif ext in AUDIO_EXT:
                out.add('audio')
            elif ext in VIDEO_EXT:
                out.add('video')
    if m.get('pinned'):
        out.add('pinned')
    for url in m.get('links', []):
        dom = url.split('//', 1)[-1]
        for needle, tag in DOMAIN_TAGS:
            if needle in dom:
                out.add(tag)
    return out


# ── rendering ─────────────────────────────────────────────────────────────────
def _reply_anchor(rid, ctx, kept_ids):
    """Anchor target for the ↪ quote line, matching original conventions.

    ctx='archive' -> ../g#mID         (grep-oriented dump; non-navigational)
    ctx='topic'   -> #mID             (same-file)
    ctx='signal'  -> #mID if target kept in this signal file, else cross-link
                     to the full archive file for the same month.
    """
    if ctx == 'archive':
        return f'../g#m{rid}'
    if ctx == 'topic':
        return f'#m{rid}'
    # signal
    if kept_ids is not None and rid in kept_ids:
        return f'#m{rid}'
    fname = kept_ids.get(rid, None) if isinstance(kept_ids, dict) else None
    # kept_ids for signal is a dict id->archive_filename when cross-linking needed
    if fname:
        return f'../general-archive/{fname}#m{rid}'
    return f'#m{rid}'


def render_message(m, ctx='archive', kept=None):
    """Render one message block exactly as the archive stores it.

    Layout (verified against the snapshot):
        ### header line               (reply quote, if any, on the very next
        > ↪ quote                      line — no blank between header and quote)
                                       (blank)
        body text                      (blank before media)
                                       (blank)
        🖼️/📎 media line
    Paragraphs are separated by one blank line. The reply quote belongs to the
    header paragraph; text and media are their own paragraphs. Returns the block
    without a trailing newline (callers join blocks with a blank line between).

    ctx: 'archive' | 'signal' | 'topic'
    kept: for 'signal', the set/dict of message ids present in this signal file
          (used to choose a same-file anchor vs a cross-link to the archive).
    """
    topic_id = m.get('topic_id')
    head = [f'### <a id="m{m["id"]}"></a>{m["from"]} · {fmt_date(m["date"])}']
    if m.get('pinned'):
        head.append('📌')
    if m.get('edited'):
        head.append('*(ред.)*')
    head.append(f'· [↗]({tg_url(m["id"], topic_id)} "Открыть в Telegram")')

    header_para = ' '.join(head)
    if m.get('reply_to_id'):
        rid = m['reply_to_id']
        anchor = _reply_anchor(rid, ctx, kept)
        name = m.get('reply_to_name') or 'сообщение'
        preview = _collapse(m.get('reply_to_text') or '', 80)
        if preview:
            header_para += f'\n> ↪ [{name}]({anchor}): «{preview}»'
        else:
            header_para += f'\n> ↪ [{name}]({anchor})'

    paras = [header_para]
    if m.get('text'):
        paras.append(m['text'])

    media = m.get('media')
    if media:
        url = tg_url(m['id'], topic_id)
        if media['kind'] == 'photo':
            paras.append(f'🖼️ *(фото)* · [открыть ↗]({url})')
        elif media['kind'] == 'video' and not media.get('filename'):
            paras.append(f'🖼️ *(видео)* · [открыть ↗]({url})')
        else:
            name = media.get('filename') or 'файл'
            size = human_size(media.get('size'))
            size = f' ({size})' if size else ''
            paras.append(f'📎 `{name}`{size} · [скачать ↗]({url})')
    return '\n\n'.join(paras)


# ── file routing ───────────────────────────────────────────────────────────────
def archive_file_for(month, day, root=ROOT):
    """Which general-archive file owns (month, day). Returns a relative path str.

    Honors the existing structure: append into whatever file already exists for
    the month (single monthly file, or the matching weekly bucket). New months
    default to a single monthly file, matching the recent convention.
    """
    single = f'general-archive/{month}.md'
    if (root / single).exists():
        return single
    wk = f'general-archive/{month}-w{week_of(day)}.md'
    if (root / wk).exists():
        return wk
    return single


def topic_file_for(topic_id):
    return f'topics/{TOPIC_IDS[topic_id]}.md'


def _prev_next_month(month):
    y, mo = int(month[:4]), int(month[5:7])
    pm = (y - 1, 12) if mo == 1 else (y, mo - 1)
    nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return f'{pm[0]:04d}-{pm[1]:02d}', f'{nm[0]:04d}-{nm[1]:02d}'


def stem_sort_key(stem):
    """Chronological sort key for file stems ('2026-05', '2025-08-w3')."""
    m = re.match(r'(\d{4})-(\d{2})(?:-w(\d))?$', stem)
    y, mo, wk = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return (y, mo, wk)


def nav_line(stem, stems):
    """Prev/next nav based on chronological neighbours among `stems`.

    Reproduces existing files byte-for-byte (the original used the same
    prev/next-neighbour rule) and extends the chain correctly for new files.
    """
    ordered = sorted(set(stems) | {stem}, key=stem_sort_key)
    i = ordered.index(stem)
    prev = f'[← {ordered[i - 1]}]({ordered[i - 1]}.md) · ' if i > 0 else ''
    nxt = f' · [{ordered[i + 1]} →]({ordered[i + 1]}.md)' if i < len(ordered) - 1 else ''
    return f'{prev}[К навигации](../README.md){nxt}'


def archive_header(stem, count, stems):
    """Header for a general-archive file. stem like '2026-06' or '2026-06-w2'."""
    return (
        f'# General archive · {stem}\n\n'
        f'_{count} сообщений (без сигнального фильтра)._ '
        f'Сигнальная версия: [general/{stem}.md](../general/{stem}.md)\n\n'
        f'{nav_line(stem, stems)}\n\n---\n'
    )


def signal_header(stem, count, stems):
    return (
        f'# General · {stem}\n\n'
        f'_{count} сообщений после фильтра._ '
        f'Полная версия: [general-archive/{stem}.md](../general-archive/{stem}.md)\n\n'
        f'{nav_line(stem, stems)}\n\n---\n'
    )


def general_stems(root=ROOT):
    """All general-archive file stems present on disk."""
    return [p.stem for p in (root / 'general-archive').glob('*.md')]


def tag_preview(text, photo_only=False):
    """Tag-list preview: hard 120-char cut + … (matches original)."""
    if photo_only and not (text or '').strip():
        return '[фото]'
    t = ' '.join((text or '').split())
    return t if len(t) <= 120 else t[:119] + '…'


# ── parsing (archive markdown -> message dicts) ─────────────────────────────────
HEADER_RE = re.compile(
    r'^### <a id="m(?P<id>\d+)"></a>(?P<from>.*?) · '
    r'(?P<date>\d{2}\.\d{2} \d{2}:\d{2})(?P<flags>(?: 📌| \*\(ред\.\)\*)*) · '
    r'\[↗\]\((?P<url>[^ )]+)'
)
REPLY_RE = re.compile(r'^> ↪ \[(?P<name>.*?)\]\((?P<anchor>[^)]*)\): «(?P<text>.*)»\s*$')
MEDIA_PHOTO_RE = re.compile(r'^🖼️ \*\((?:фото|видео)\)\*')
MEDIA_FILE_RE = re.compile(r'^📎 `(?P<name>.*?)`(?: \((?P<size>[^)]*)\))? ·')


def parse_archive_file(path):
    """Parse a rendered general-archive/topic file back into message dicts.

    Recovers enough to (a) re-filter into the signal version and (b) re-derive
    tags: id, from, date(display), flags, reply target+preview, body text,
    media kind, and links. Robust to both the original snapshot format and this
    module's output (they are identical by construction).
    """
    text = Path(path).read_text(encoding='utf-8')
    # message blocks start at a header line
    parts = re.split(r'(?=^### <a id="m\d+">)', text, flags=re.M)
    msgs = []
    for block in parts:
        h = HEADER_RE.search(block)
        if not h:
            continue
        lines = block.splitlines()
        body_lines, reply = [], None
        media_kind, media_ext = None, None
        for ln in lines[1:]:
            r = REPLY_RE.match(ln)
            if r:
                reply = (r.group('name'), r.group('text'))
                continue
            if MEDIA_PHOTO_RE.match(ln):
                media_kind = 'photo'
                continue
            mf = MEDIA_FILE_RE.match(ln)
            if mf:
                media_kind = 'file'
                nm = mf.group('name') or ''
                media_ext = nm.rsplit('.', 1)[-1].lower() if '.' in nm else None
                continue
            body_lines.append(ln)
        body = '\n'.join(body_lines).strip()
        flags = h.group('flags')
        msgs.append({
            'id': int(h.group('id')),
            'from': h.group('from'),
            'date_disp': h.group('date'),
            'pinned': '📌' in flags,
            'edited': '*(ред.)*' in flags,
            'url': h.group('url'),
            'reply_name': reply[0] if reply else None,
            'reply_text': reply[1] if reply else None,
            'text': body,
            'media_kind': media_kind,
            'media_ext': media_ext,
            'links': extract_links(body),
            'raw': block,
        })
    return msgs
