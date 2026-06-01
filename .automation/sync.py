"""Incremental real-time sync: pull new Telegram messages into the archive.

Userbot (Telethon) reads messages with id > last synced, renders them through
archive_core into the same on-disk format as the original snapshot, appends them
to the right files, and records what changed in .sync-new.json for regen.py.

  python .automation/sync.py --setup   # one-time, local: create session string
  python .automation/sync.py           # CI: pull + append + record
  python .automation/sync.py --reset   # forget sync state

TIME ZONE: Telegram delivers timestamps in UTC. The original snapshot was
exported in local time, so we convert to TG_TZ (default Europe/Moscow) to keep
displayed times consistent with the historical files. Verify on the first run
and override via the TG_TZ env/secret if the group's times look shifted.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import MessageService
except ImportError:
    print('Install telethon: pip install telethon')
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import archive_core as ac

ROOT = ac.ROOT
STATE_FILE = ROOT / '.sync-state.json'
NEW_FILE = ROOT / '.sync-new.json'
TZ_NAME = os.environ.get('TG_TZ', 'Europe/Moscow')


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {'last_message_id': 0, 'last_sync': None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


async def setup():
    api_id = int(input('TG_API_ID: '))
    api_hash = input('TG_API_HASH: ').strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        out = ROOT / '.session-string.txt'
        out.write_text(client.session.save())
        print(f'Session saved to {out}. Add it as the TG_SESSION secret.')


def _localize(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if ZoneInfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(TZ_NAME))
        except Exception:
            pass
    return dt.isoformat()[:19]


def _name(msg):
    s = msg.sender
    if s is None:
        return 'Аноним'
    first = getattr(s, 'first_name', None)
    last = getattr(s, 'last_name', None)
    if first or last:
        return ' '.join(p for p in (first, last) if p)
    if getattr(s, 'title', None):  # posted as the channel
        return 'Админ'
    return getattr(s, 'username', None) or 'Аноним'


def _topic_and_reply(msg):
    """Return (topic_id, reply_to_id). topic_id is one of the 5 known forum
    topics or None (None == General). reply_to_id is a genuine quoted message,
    not the topic-root linkage."""
    rt = msg.reply_to
    if rt is None:
        return None, None
    top = getattr(rt, 'reply_to_top_id', None)
    msgid = getattr(rt, 'reply_to_msg_id', None)
    is_forum = getattr(rt, 'forum_topic', False)
    topic_id = None
    if is_forum:
        if top in ac.TOPIC_IDS:
            topic_id = top
        elif msgid in ac.TOPIC_IDS:
            topic_id = msgid
        reply_id = msgid if (msgid and msgid != topic_id and msgid != top) else None
    else:
        reply_id = msgid
    return topic_id, reply_id


def _media(msg):
    if msg.photo:
        return {'kind': 'photo', 'filename': None, 'size': None, 'ext': None}
    if msg.document or msg.video or msg.audio or msg.voice:
        f = msg.file
        name = getattr(f, 'name', None) if f else None
        size = getattr(f, 'size', None) if f else None
        ext = name.rsplit('.', 1)[-1].lower() if name and '.' in name else None
        if msg.voice or msg.audio:
            kind = 'audio'
        elif msg.video and not name:
            kind = 'video'
        else:
            kind = 'file'
        return {'kind': kind, 'filename': name, 'size': size, 'ext': ext}
    return None  # webpage previews / polls are not attachments


async def _convert(msg, client, cache):
    topic_id, reply_id = _topic_and_reply(msg)
    reply_name = reply_text = None
    if reply_id:
        ref = cache.get(reply_id)
        if ref is None:
            try:
                ref = await msg.get_reply_message()
            except Exception:
                ref = None
        if ref is not None:
            reply_name = _name(ref)
            reply_text = ref.text or ''
    text = msg.text or ''
    return {
        'id': msg.id,
        'date': _localize(msg.date),
        'from': _name(msg),
        'text': text,
        'pinned': bool(getattr(msg, 'pinned', False)),
        'edited': bool(getattr(msg, 'edit_date', None)),
        'topic_id': topic_id,
        'reply_to_id': reply_id,
        'reply_to_name': reply_name,
        'reply_to_text': reply_text,
        'media': _media(msg),
        'links': ac.extract_links(text),
    }


def _append_blocks(path, blocks, header=None):
    body = '\n\n'.join(blocks)
    if path.exists():
        existing = path.read_text(encoding='utf-8').rstrip('\n')
        text = existing + '\n\n' + body + '\n'
    else:
        text = (header or '') + '\n' + body + '\n'  # header ends with '---\n'
    path.write_text(text, encoding='utf-8')


def _patch_topic_count(path):
    txt = path.read_text(encoding='utf-8')
    n = len(re.findall(r'^### <a id="m\d+">', txt, re.M))
    txt = re.sub(r'(Всего сообщений: )\d+', rf'\g<1>{n}', txt, count=1)
    path.write_text(txt, encoding='utf-8')


async def sync():
    api_id = int(os.environ['TG_API_ID'])
    api_hash = os.environ['TG_API_HASH']
    session = os.environ['TG_SESSION']
    group_id = os.environ['TG_GROUP_ID']
    try:
        group_id = int(group_id)
    except ValueError:
        pass

    state = load_state()
    min_id = state['last_message_id']
    print(f'Syncing messages with id > {min_id} (tz={TZ_NAME})')

    raw = []
    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        entity = await client.get_entity(group_id)
        async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
            if isinstance(msg, MessageService):
                continue
            raw.append(msg)
        cache = {m.id: m for m in raw}
        msgs = [await _convert(m, client, cache) for m in raw
                if (m.text or m.media)]

    print(f'Got {len(msgs)} new content messages.')
    if not msgs:
        NEW_FILE.write_text(json.dumps({'archive_stems': [], 'messages': []}))
        return

    all_stems = set(ac.general_stems())
    by_file = defaultdict(list)   # path -> (ctx, [msg,...])
    touched_stems = set()
    for m in msgs:
        if m['topic_id']:
            rel = ac.topic_file_for(m['topic_id'])
            by_file[(rel, 'topic', None)].append(m)
        else:
            month, day = ac.month_of(m['date']), int(m['date'][8:10])
            rel = ac.archive_file_for(month, day)
            stem = Path(rel).stem
            touched_stems.add(stem)
            all_stems.add(stem)
            by_file[(rel, 'archive', stem)].append(m)

    for (rel, ctx, stem), lst in by_file.items():
        path = ROOT / rel
        blocks = [ac.render_message(m, ctx=ctx) for m in lst]
        if ctx == 'archive':
            header = ac.archive_header(stem, 0, all_stems)
            _append_blocks(path, blocks, header=header)
        else:  # topic file — always already exists
            _append_blocks(path, blocks)
            _patch_topic_count(path)
        print(f'  +{len(lst)} -> {rel}')

    state['last_message_id'] = max(m['id'] for m in msgs)
    state['last_sync'] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    NEW_FILE.write_text(json.dumps(
        {'archive_stems': sorted(touched_stems, key=ac.stem_sort_key), 'messages': msgs},
        ensure_ascii=False))
    print(f'New last_message_id: {state["last_message_id"]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--reset', action='store_true')
    args = ap.parse_args()
    if args.reset:
        STATE_FILE.exists() and STATE_FILE.unlink()
        print('State reset.')
    elif args.setup:
        asyncio.run(setup())
    else:
        asyncio.run(sync())


if __name__ == '__main__':
    main()
