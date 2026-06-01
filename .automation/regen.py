"""Regeneration pass — runs right after sync.

Keeps the derived layers consistent with general-archive/ (the source of truth):
  * signal version  (general/)        — rebuilt for touched months
  * file headers     (count + nav)     — patched for touched files + neighbours
  * README indexes   (month lists)     — counts updated, new months inserted
  * tags/            (per-type lists)   — new messages appended, counts refreshed

Incremental mode (default) reads .sync-new.json written by sync.py and touches
only what changed — history stays byte-for-byte intact. `--all` rebuilds every
derived layer from the archives (recovery / reformat; will rewrite history).
"""
import argparse
import json
import re
from pathlib import Path

import archive_core as ac

ROOT = ac.ROOT
NEW_FILE = ROOT / '.sync-new.json'


# ── counts & header patching ────────────────────────────────────────────────
def count_headers(path):
    return len(re.findall(r'^### <a id="m\d+">', path.read_text(encoding='utf-8'), re.M))


def patch_count_and_nav(path, stem, stems):
    lines = path.read_text(encoding='utf-8').split('\n')
    count = len(re.findall(r'^### <a id="m\d+">', '\n'.join(lines), re.M))
    for i, ln in enumerate(lines[:8]):
        if ln.startswith('_') and 'сообщений' in ln:
            lines[i] = re.sub(r'^_\d+', f'_{count}', ln)
        elif 'К навигации' in ln:
            lines[i] = ac.nav_line(stem, stems)
    path.write_text('\n'.join(lines), encoding='utf-8')
    return count


# ── signal rebuild ───────────────────────────────────────────────────────────
REPLY_ANCHOR_RE = re.compile(r'(> ↪ \[[^\]]*\]\()([^)#]*)(#m\d+\))')


def _reply_target_id(block):
    m = re.search(r'> ↪ \[[^\]]*\]\([^)]*#m(\d+)\)', block)
    return int(m.group(1)) if m else None


def build_ranges():
    """Min/max message id per archive file (ids are time-ordered & contiguous,
    so each id falls in exactly one file). Lets us resolve which file a reply
    target lives in without a full id->file map."""
    ranges = []
    for p in (ROOT / 'general-archive').glob('*.md'):
        ids = [int(x) for x in re.findall(r'id="m(\d+)"', p.read_text(encoding='utf-8'))]
        if ids:
            ranges.append((p.stem, min(ids), max(ids)))
    ranges.sort(key=lambda r: r[1])
    return ranges


def stem_of(rid, ranges):
    for stem, lo, hi in ranges:
        if lo <= rid <= hi:
            return stem
    return None


_SIGNAL_IDS = {}


def _signal_has(stem, rid):
    if stem not in _SIGNAL_IDS:
        p = ROOT / f'general/{stem}.md'
        ids = set(re.findall(r'id="m(\d+)"', p.read_text(encoding='utf-8'))) if p.exists() else set()
        _SIGNAL_IDS[stem] = ids
    return str(rid) in _SIGNAL_IDS[stem]


def resolve_signal_anchor(rid, this_stem, kept, ranges):
    """Reproduce the original cross-link rule for a reply target in a signal file:
      target kept here            -> #mID
      target in this month, cut   -> ../general-archive/{this}.md#mID
      target in another file kept -> ../general/{that}.md#mID
      target in another file, cut -> ../general-archive/{that}.md#mID
    """
    if rid in kept:
        return ''  # same-file: prefix empty, '#mID)' kept by the regex
    tgt = stem_of(rid, ranges)
    if tgt is None or tgt == this_stem:
        return f'../general-archive/{this_stem}.md'
    return f'../general/{tgt}.md' if _signal_has(tgt, rid) else f'../general-archive/{tgt}.md'


def signal_keep(msgs):
    """Apply the signal filter; return the set of kept message ids.

    keep if: text >30 chars, OR media, OR a link, OR pinned, OR replied-to >=2x
    (reply count measured within this file, matching how files are chunked).
    """
    rc = {}
    for m in msgs:
        t = _reply_target_id(m['raw'])
        if t is not None:
            rc[t] = rc.get(t, 0) + 1
    kept = set()
    for m in msgs:
        if (len(m['text']) > 30 or m['media_kind'] or m['links']
                or m['pinned'] or rc.get(m['id'], 0) >= 2):
            kept.add(m['id'])
    return kept


def rebuild_signal(stem, stems, ranges=None):
    """Rebuild general/{stem}.md from general-archive/{stem}.md.

    The signal block is identical to the archive block except the ↪ reply
    anchor, so we reuse the raw archive blocks and only rewrite that anchor via
    resolve_signal_anchor (same-file / same-month-archive / other-file).
    """
    if ranges is None:
        ranges = build_ranges()
    arc_path = ROOT / f'general-archive/{stem}.md'
    msgs = ac.parse_archive_file(arc_path)
    kept = signal_keep(msgs)

    body = []
    for m in msgs:
        if m['id'] not in kept:
            continue
        block = m['raw'].strip('\n')
        rid = _reply_target_id(block)
        if rid is not None:
            prefix = resolve_signal_anchor(rid, stem, kept, ranges)
            block = REPLY_ANCHOR_RE.sub(rf'\g<1>{prefix}\g<3>', block, count=1)
        body.append(block)

    out = ac.signal_header(stem, len(kept), stems) + '\n' + '\n\n'.join(body) + '\n'
    (ROOT / f'general/{stem}.md').write_text(out, encoding='utf-8')
    return len(kept)


# ── README month lists ─────────────────────────────────────────────────────────
def update_readme_month(stem, sig_count, arc_count):
    path = ROOT / 'README.md'
    text = path.read_text(encoding='utf-8')

    def upsert(text, section_anchor, link_dir, count):
        line = f'- [{stem}]({link_dir}/{stem}.md) — {count} сообщений'
        pat = re.compile(rf'^- \[{re.escape(stem)}\]\({re.escape(link_dir)}/[^)]+\) — \d+ сообщений$', re.M)
        if pat.search(text):
            return pat.sub(line, text)
        # insert chronologically within the section
        lines = text.split('\n')
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == section_anchor)
        except StopIteration:
            return text
        block_re = re.compile(rf'^- \[(\S+?)\]\({re.escape(link_dir)}/')
        insert_at, last = None, start
        for i in range(start + 1, len(lines)):
            mm = block_re.match(lines[i])
            if mm:
                last = i
                if ac.stem_sort_key(mm.group(1)) > ac.stem_sort_key(stem):
                    insert_at = i
                    break
            elif lines[i].startswith('## '):
                break
        if insert_at is None:
            insert_at = last + 1
        lines.insert(insert_at, line)
        return '\n'.join(lines)

    text = upsert(text, '## General — сигнальная версия', 'general', sig_count)
    text = upsert(text, '## General — полный архив', 'general-archive', arc_count)
    path.write_text(text, encoding='utf-8')


# ── tags ─────────────────────────────────────────────────────────────────────
def _tag_header(tag):
    return (f'# {ac.TAG_LABELS.get(tag, tag)}\n\n_0 сообщений._\n\n'
            f'[← Все теги](README.md) · [К навигации](../README.md)\n\n---\n')


def append_tags(messages):
    """Append tag-list entries for new messages; refresh counts + tags/README."""
    touched = set()
    for m in messages:
        tags = ac.tags_for(m)
        if not tags:
            continue
        if m.get('topic_id'):
            rel = f'../topics/{ac.TOPIC_IDS[m["topic_id"]]}.md'
        else:
            af = ac.archive_file_for(ac.month_of(m['date']), int(m['date'][8:10]))
            rel = '../' + af.replace('general-archive/', 'general/')
        photo_only = bool(m.get('media') and m['media']['kind'] == 'photo' and not (m.get('text') or '').strip())
        preview = ac.tag_preview(m.get('text', ''), photo_only=photo_only)
        entry = (f'- **{m["from"]}** · {ac.fmt_date(m["date"])} · [архив]({rel}#m{m["id"]})'
                 f' · [TG ↗]({ac.tg_url(m["id"], m.get("topic_id"))}) — {preview}')
        for tag in tags:
            p = ROOT / f'tags/{tag}.md'
            if not p.exists():
                p.write_text(_tag_header(tag), encoding='utf-8')
            with p.open('a', encoding='utf-8') as f:
                f.write(entry + '\n')
            touched.add(tag)

    for tag in touched:
        p = ROOT / f'tags/{tag}.md'
        txt = p.read_text(encoding='utf-8')
        n = len(re.findall(r'^- \*\*', txt, re.M))
        p.write_text(re.sub(r'^_\d+ сообщений\.', f'_{n} сообщений.', txt, count=1, flags=re.M), encoding='utf-8')

    rebuild_tags_index()


def rebuild_tags_index():
    rows = []
    for p in (ROOT / 'tags').glob('*.md'):
        if p.stem == 'README':
            continue
        txt = p.read_text(encoding='utf-8')
        n = len(re.findall(r'^- \*\*', txt, re.M))
        label = ac.TAG_LABELS.get(p.stem, p.stem)
        rows.append((n, label, p.name))
    rows.sort(key=lambda r: -r[0])
    table = ['# Теги', '', '_Сообщения сгруппированы по типу контента._', '',
             '[← К навигации](../README.md)', '', '---', '',
             '| Тег | Сообщений |', '|---|---:|']
    table += [f'| [{label}]({fn}) | {n} |' for n, label, fn in rows]
    (ROOT / 'tags' / 'README.md').write_text('\n'.join(table) + '\n', encoding='utf-8')


# ── drivers ────────────────────────────────────────────────────────────────────
def neighbours(stems_touched, all_stems):
    out = set(stems_touched)
    ordered = sorted(set(all_stems), key=ac.stem_sort_key)
    for s in stems_touched:
        if s in ordered:
            i = ordered.index(s)
            if i > 0:
                out.add(ordered[i - 1])
            if i < len(ordered) - 1:
                out.add(ordered[i + 1])
    return out


def run_incremental():
    if not NEW_FILE.exists():
        print('No .sync-new.json — nothing to regenerate.')
        return
    payload = json.loads(NEW_FILE.read_text(encoding='utf-8'))
    stems = payload.get('archive_stems', [])
    messages = payload.get('messages', [])
    all_stems = ac.general_stems()
    _SIGNAL_IDS.clear()
    ranges = build_ranges()

    for stem in stems:
        sig = rebuild_signal(stem, all_stems, ranges)
        arc = patch_count_and_nav(ROOT / f'general-archive/{stem}.md', stem, all_stems)
        patch_count_and_nav(ROOT / f'general/{stem}.md', stem, all_stems)
        update_readme_month(stem, sig, arc)
        print(f'  {stem}: archive={arc} signal={sig}')

    for stem in neighbours(stems, all_stems):
        for d in ('general-archive', 'general'):
            p = ROOT / f'{d}/{stem}.md'
            if p.exists():
                patch_count_and_nav(p, stem, all_stems)

    if messages:
        append_tags(messages)
        print(f'  tagged {len(messages)} new messages')

    NEW_FILE.unlink()
    print('Regen done.')


def run_all():
    all_stems = ac.general_stems()
    _SIGNAL_IDS.clear()
    ranges = build_ranges()
    print(f'Full rebuild over {len(all_stems)} months (this rewrites history)…')
    for stem in sorted(all_stems, key=ac.stem_sort_key):
        sig = rebuild_signal(stem, all_stems, ranges)
        arc = patch_count_and_nav(ROOT / f'general-archive/{stem}.md', stem, all_stems)
        patch_count_and_nav(ROOT / f'general/{stem}.md', stem, all_stems)
        update_readme_month(stem, sig, arc)
    rebuild_tags_index()
    print('Full rebuild done.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true', help='rebuild every derived layer from archives')
    args = ap.parse_args()
    run_all() if args.all else run_incremental()


if __name__ == '__main__':
    main()
