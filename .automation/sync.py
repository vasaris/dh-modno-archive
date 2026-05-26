"""Incremental sync: pull new Telegram messages → append to monthly files → commit."""
import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print('Install telethon: pip install telethon')
    sys.exit(1)

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / '.sync-state.json'

TOPIC_IDS = {
    7348: '01-вопросы-по-правилам',
    7351: '02-файлы-полезное-и-хоумрулы',
    14289: '03-поиск-игроков',
    19690: '04-хоумрулы-неофициальные-механики',
    70924: '05-новости',
}


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
        print(f'Session saved to {out}. Add as TG_SESSION secret.')


async def sync():
    api_id = int(os.environ['TG_API_ID'])
    api_hash = os.environ['TG_API_HASH']
    session = os.environ['TG_SESSION']
    group_id = os.environ['TG_GROUP_ID']

    state = load_state()
    min_id = state['last_message_id']
    print(f'Syncing from message_id > {min_id}')

    new_msgs = []
    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        entity = await client.get_entity(group_id)
        async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
            new_msgs.append(_convert(msg))

    print(f'Got {len(new_msgs)} new messages.')
    if not new_msgs:
        return

    by_id = {m['id']: m for m in new_msgs}
    grouped = defaultdict(list)
    for m in new_msgs:
        tid = _find_topic(m, by_id)
        if tid:
            grouped[f'topics/{TOPIC_IDS[tid]}.md'].append(m)
        else:
            month = m['date'][:7]
            grouped[f'general/{month}-incremental.md'].append(m)
            grouped[f'general-archive/{month}-incremental.md'].append(m)

    for fpath, lst in grouped.items():
        full = ROOT / fpath
        full.parent.mkdir(parents=True, exist_ok=True)
        if not full.exists():
            full.write_text(f'# Incremental: {fpath}\n\n_Auto-appended by sync._\n\n---\n\n', encoding='utf-8')
        with full.open('a', encoding='utf-8') as f:
            for m in lst:
                f.write(_render(m) + '\n')

    state['last_message_id'] = max(m['id'] for m in new_msgs)
    state['last_sync'] = datetime.utcnow().isoformat()
    save_state(state)
    print(f'New last_id: {state["last_message_id"]}')


def _convert(tl_msg):
    sender = tl_msg.sender
    name = 'Аноним'
    if sender:
        if hasattr(sender, 'first_name') and sender.first_name:
            name = sender.first_name
        elif hasattr(sender, 'title') and sender.title:
            name = 'Админ'  # channel post
    return {
        'id': tl_msg.id,
        'date': tl_msg.date.isoformat()[:19],
        'from': name,
        'text': tl_msg.text or '',
        'reply_to_message_id': tl_msg.reply_to.reply_to_msg_id if tl_msg.reply_to else None,
        'has_media': bool(tl_msg.media),
    }


def _find_topic(m, by_id):
    rid = m.get('reply_to_message_id')
    visited = set()
    while rid:
        if rid in TOPIC_IDS:
            return rid
        if rid in visited:
            return None
        visited.add(rid)
        parent = by_id.get(rid)
        if not parent:
            return None
        rid = parent.get('reply_to_message_id')
    return None


def _render(m):
    date = m['date'][8:10] + '.' + m['date'][5:7] + ' ' + m['date'][11:16]
    out = [f'### <a id="m{m["id"]}"></a>{m["from"]} · {date}', '']
    if m['text']:
        out.append(m['text'])
    if m['has_media']:
        out.append('')
        out.append('🖼️ *(медиа)*')
    out.append('')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--reset', action='store_true')
    args = ap.parse_args()

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        print('State reset.')
        return

    if args.setup:
        asyncio.run(setup())
        return

    asyncio.run(sync())


if __name__ == '__main__':
    main()
