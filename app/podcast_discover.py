"""Let the system look for podcasts by itself and follow the ones it likes.

Pipeline
--------
1. Search: the free iTunes/Apple Podcasts Search API (no key, no account) is
   queried with the interests the listener wrote down.
2. Rank: if a model key is configured, that model reads the candidate list and
   picks the shows that actually fit, with a one-line reason each. Without a
   key, a transparent local heuristic (keyword hit, genre, episode count,
   recency) does the ranking so the feature still works offline-ish.
3. Follow: chosen shows are appended to the subscription registry, their RSS
   catalogue is synced and their cover is archived - the same code path the
   manual "更新目录" button uses.

Nothing here ever writes a credential into the repository.
"""
import hashlib
import json
import re
import time
from urllib.parse import urlencode

import paths
import podcast_archive as archive
import podcast_llm as llm

SEARCH_ENDPOINT = 'https://itunes.apple.com/search'
LOOKUP_ENDPOINT = 'https://itunes.apple.com/lookup'
MAX_TERM = 120
MAX_LIMIT = 50

DEFAULT_INTERESTS = {
    'keywords': [],
    'exclude_keywords': [],
    'country': 'US',
    'language': 'en',
    'auto_subscribe_limit': 3,
    'note': '写上你真正关心的主题，系统会按这些主题去找节目并自己关注。',
}


def _read(path, default):
    return archive.read_json(path, default) or default


def interests():
    data = _read(paths.INTERESTS_JSON, None)
    if not data:
        return dict(DEFAULT_INTERESTS)
    merged = dict(DEFAULT_INTERESTS)
    merged.update({k: v for k, v in data.items() if v is not None})
    return merged


def save_interests(data):
    current = interests()
    if 'keywords' in data:
        rows = data['keywords']
        if isinstance(rows, str):
            rows = re.split(r'[,，、\n]', rows)
        cleaned = [str(x).strip()[:60] for x in (rows or []) if str(x).strip()]
        if len(cleaned) > 20:
            raise ValueError('兴趣关键词最多 20 个')
        current['keywords'] = cleaned
    if 'exclude_keywords' in data:
        rows = data['exclude_keywords']
        if isinstance(rows, str):
            rows = re.split(r'[,，、\n]', rows)
        current['exclude_keywords'] = [str(x).strip()[:40] for x in (rows or []) if str(x).strip()]
    if 'country' in data:
        country = str(data['country']).strip().upper()
        if not re.fullmatch(r'[A-Z]{2}', country):
            raise ValueError('地区代码应为两位字母，例如 US、GB、CN')
        current['country'] = country
    if 'auto_subscribe_limit' in data:
        try:
            limit = int(data['auto_subscribe_limit'])
        except (TypeError, ValueError):
            raise ValueError('每次自动关注数量必须是整数') from None
        if not 1 <= limit <= 10:
            raise ValueError('每次自动关注数量请设在 1–10 之间')
        current['auto_subscribe_limit'] = limit
    archive.write_json(paths.INTERESTS_JSON, current)
    return current


def _fetch_json(url, timeout=45):
    with archive.open_url(url, timeout=timeout) as response:
        raw = response.read(6_000_001)
    if len(raw) > 6_000_000:
        raise ValueError('检索结果过大')
    return json.loads(raw.decode('utf-8', errors='replace'))


def _normalize(result):
    feed_url = (result.get('feedUrl') or '').strip()
    return {
        'apple_id': result.get('collectionId'),
        'name': (result.get('collectionName') or '').strip(),
        'author': (result.get('artistName') or '').strip(),
        'feed_url': feed_url,
        'artwork_url': result.get('artworkUrl600') or result.get('artworkUrl100') or '',
        'apple_url': result.get('collectionViewUrl') or '',
        'genres': [g for g in (result.get('genres') or []) if g],
        'episodes': result.get('trackCount') or 0,
        'released': (result.get('releaseDate') or '')[:10],
        'country': result.get('country') or '',
    }


def search(term, limit=25, country='US'):
    """One free-text query against the public Apple Podcasts index."""
    term = str(term or '').strip()
    if not term:
        return []
    params = {'media': 'podcast', 'entity': 'podcast', 'term': term[:MAX_TERM],
              'limit': max(1, min(int(limit), MAX_LIMIT)), 'country': country}
    payload = _fetch_json(SEARCH_ENDPOINT + '?' + urlencode(params))
    rows = []
    for result in payload.get('results', []):
        candidate = _normalize(result)
        if candidate['name'] and candidate['feed_url'].startswith('https://'):
            rows.append(candidate)
    return rows


def lookup(apple_id):
    payload = _fetch_json(LOOKUP_ENDPOINT + '?' + urlencode({'id': apple_id, 'entity': 'podcast'}))
    for result in payload.get('results', []):
        candidate = _normalize(result)
        if candidate['feed_url'].startswith('https://'):
            return candidate
    raise ValueError('没有找到这个节目的订阅地址')


def candidates(keywords, per_keyword=12, country='US', extra_terms=None):
    """Union of several searches, de-duplicated by feed address."""
    terms = [k for k in (keywords or []) if str(k).strip()][:10]
    merged, seen = [], set()
    errors = []
    for term in terms:
        try:
            for row in search(term, per_keyword, country):
                key = row['feed_url'].rstrip('/').lower()
                if key in seen:
                    continue
                seen.add(key)
                row['matched'] = row['matched'] + [term] if row.get('matched') else [term]
                merged.append(row)
        except Exception as exc:  # a region can be unavailable; keep the rest
            errors.append(f'{term}: {exc}')
    for term in (extra_terms or []):
        try:
            for row in search(term, per_keyword, country):
                key = row['feed_url'].rstrip('/').lower()
                if key in seen:
                    continue
                seen.add(key)
                row['matched'] = [term]
                merged.append(row)
        except Exception as exc:
            errors.append(f'{term}: {exc}')
    return merged, errors


def heuristic_score(candidate, keywords, exclude):
    """Transparent no-key ranking.

    A keyword in the show's own name matters far more than one that only shows
    up in its genre, otherwise broad 2000-episode magazines outrank the show
    the listener actually described.
    """
    name_hay = (candidate['name'] + ' ' + candidate['author']).lower()
    genre_hay = ' '.join(candidate['genres']).lower()
    score = 0.0
    for word in keywords:
        word = str(word).lower().strip()
        if not word:
            continue
        if word in name_hay:
            score += 4.0
        elif word in genre_hay:
            score += 1.5
    for word in candidate.get('matched') or []:
        word = word.lower()
        if word in name_hay:
            score += 0.8
        elif word in genre_hay:
            score += 0.4
    for word in exclude:
        word = str(word).lower().strip()
        if word and (word in name_hay or word in genre_hay):
            score -= 6.0
    score += min(candidate['episodes'], 300) / 600.0   # mild: active over dormant
    if candidate['released'][:4] >= '2024':
        score += 0.8
    if candidate['country'] in ('USA', 'GBR', 'CAN', 'AUS'):
        score += 0.2
    return round(score, 3)


def recommend(data=None):
    """Return ranked candidates plus how the ranking was produced."""
    data = data or {}
    profile = interests()
    keywords = data.get('keywords')
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r'[,，、\n]', keywords) if k.strip()]
    keywords = keywords or profile['keywords']
    if not keywords:
        raise ValueError('先在「AI 发现」里写下几个你感兴趣的主题，再让系统去找。')
    country = (data.get('country') or profile['country'] or 'US').upper()[:2]
    exclude = profile.get('exclude_keywords') or []
    limit = int(data.get('limit') or 20)
    rows, errors = candidates(keywords, per_keyword=int(data.get('per_keyword') or 12), country=country)
    if not rows:
        raise ValueError('没有检索到可用的节目，请换个关键词或地区再试。' + ('；'.join(errors) if errors else ''))
    for row in rows:
        row['score'] = heuristic_score(row, keywords, exclude)
    rows.sort(key=lambda r: (-r['score'], -r['episodes']))
    shortlist = [r for r in rows if r['score'] > -3][:max(6, min(limit, 24))] or rows[:12]

    registered = {(_feed_key(r['feed_url'])) for r in _read(paths.SHOWS_JSON, []) if r.get('feed_url')}
    for row in shortlist:
        row['already_followed'] = _feed_key(row['feed_url']) in registered

    result = {'candidates': shortlist, 'interests': profile, 'keywords': keywords,
              'country': country, 'errors': errors, 'ranking': 'heuristic', 'reasons': {}}
    if data.get('use_ai', True) and llm.public_settings()['configured']:
        try:
            picks = _ai_pick(shortlist, profile, keywords)
            if picks:
                result['ranking'] = 'ai'
                result['reasons'] = picks
                result['candidates'] = sorted(
                    shortlist, key=lambda r: (0 if str(r['apple_id']) in picks else 1, -r['score']))
        except Exception as exc:
            result['ai_error'] = str(exc)
    return result


def _ai_pick(shortlist, profile, keywords):
    payload = {
        'interests': keywords,
        'exclude': profile.get('exclude_keywords') or [],
        'wanted': profile.get('auto_subscribe_limit', 3),
        'note': profile.get('note', ''),
        'candidates': [{
            'id': row['apple_id'],
            'name': row['name'],
            'author': row['author'],
            'genres': row['genres'][:4],
            'episodes': row['episodes'],
            'matched_keywords': row.get('matched') or [],
        } for row in shortlist],
    }
    parsed, _usage = llm.chat_json(llm.SYSTEM, json.dumps(payload, ensure_ascii=False))
    picks, reasons = set(), {}
    for row in (parsed.get('picks') or [])[:20]:
        if not isinstance(row, dict):
            continue
        try:
            identifier = int(row.get('id'))
        except (TypeError, ValueError):
            continue
        reason = str(row.get('reason') or '').strip()[:200]
        if any(c['apple_id'] == identifier for c in shortlist):
            picks.add(identifier)
            reasons[str(identifier)] = reason
    return picks and reasons or {}


def auto(data=None):
    """Search, rank and follow in one call - the "let the AI subscribe for me" path."""
    data = data or {}
    profile = interests()
    keywords = data.get('keywords') or profile.get('keywords')
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r'[,，、\n]', keywords) if k.strip()]
    if not keywords:
        raise ValueError('先写下你想听的主题，系统才能自己去找节目。')
    country = (data.get('country') or profile.get('country') or 'US').upper()[:2]
    report = recommend({'keywords': keywords, 'country': country, 'use_ai': True, 'limit': 24})
    limit = int(data.get('limit') or profile.get('auto_subscribe_limit') or 3)
    chosen, skipped = [], []
    if report['ranking'] == 'ai':
        for row in report['candidates']:
            if str(row['apple_id']) in report['reasons'] and not row['already_followed']:
                chosen.append({**row, 'reason': report['reasons'][str(row['apple_id'])], 'picked_by': 'ai'})
            if len(chosen) >= limit:
                break
    if not chosen:  # no key, or the model abstained: fall back to the local ranking
        best = max([row['score'] for row in report['candidates']] or [0])
        floor = max(1.0, best * 0.4)   # never fill the quota with weak matches
        for row in report['candidates']:
            if not row['already_followed'] and row['score'] >= floor:
                names = '、'.join(row.get('matched') or []) or '兴趣关键词'
                chosen.append({**row, 'reason': f'命中「{names}」，按关键词与活跃度自动选出',
                               'picked_by': 'heuristic'})
            if len(chosen) >= limit:
                break
        if not chosen:
            weak = [row for row in report['candidates'] if not row['already_followed']]
            for row in weak:
                chosen.append({**row, 'reason': '候选中相关性最高的节目，供你确认',
                               'picked_by': 'heuristic'})
                if len(chosen) >= limit:
                    break
    skipped = [row['name'] for row in report['candidates'] if row['already_followed']]
    if not chosen:
        report['subscribed'] = []
        report['message'] = '候选里没有可新增的节目，可能都已经关注过了。'
        return report
    report['subscribed'] = subscribe(chosen)
    report['skipped_followed'] = skipped
    if not any(row.get('status') == 'followed' for row in report['subscribed']):
        report['message'] = ('找到了节目但同步订阅源失败，请检查网络后重试；'
                             '检索与排序结果仍保留在下面。')
    return report


def _feed_key(url):
    return str(url or '').rstrip('/').lower()


def _slug(name, apple_id, taken):
    base = re.sub(r'[^a-z0-9]+', '-', str(name).lower()).strip('-')[:60]
    base = re.sub(r'-{2,}', '-', base)
    if not base or not re.match(r'[a-z0-9]', base):
        base = 'show-' + str(apple_id or hashlib.sha256(str(name).encode()).hexdigest()[:8])
    slug, index = base, 2
    while slug in taken:
        slug = f'{base}-{index}'
        index += 1
    return slug


def subscribe(entries):
    """Follow shows: registry entry + RSS catalogue + cover, reusing archive.py."""
    registry = list(_read(paths.SHOWS_JSON, []))
    taken = {row['id'] for row in registry if row.get('id')}
    existing = {_feed_key(row.get('feed_url')) for row in registry}
    results = []
    for entry in entries or []:
        record = {'name': entry.get('name'), 'apple_id': entry.get('apple_id')}
        try:
            name = str(entry.get('name') or '').strip()
            feed_url = str(entry.get('feed_url') or '').strip()
            if not name or not feed_url.startswith('https://'):
                raise ValueError('缺少节目名称或订阅地址')
            if _feed_key(feed_url) in existing:
                results.append({**record, 'status': 'already_followed'})
                continue
            slug = _slug(name, entry.get('apple_id'), taken)
            taken.add(slug)
            archive.sync(slug, feed_url)
            cover_saved = _save_cover(slug, entry.get('artwork_url'))
            registry.append({
                'id': slug, 'name': name,
                'category': str(entry.get('category') or (entry.get('genres') or ['播客'])[0])[:20],
                'feed_url': feed_url,
                'apple_url': entry.get('apple_url') or '',
                'artwork_url': entry.get('artwork_url') or '',
                'apple_id': entry.get('apple_id'),
                'added_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'added_by': entry.get('picked_by') or 'manual',
                'reason': entry.get('reason') or '',
            })
            existing.add(_feed_key(feed_url))
            results.append({**record, 'id': slug, 'status': 'followed', 'cover': cover_saved})
            archive.write_json(paths.SHOWS_JSON, registry)
        except Exception as exc:
            results.append({**record, 'status': 'failed', 'error': str(exc)[:300]})
    return results


def _save_cover(slug, artwork_url):
    if not artwork_url or not str(artwork_url).startswith('https://'):
        return False
    target = archive.folder(slug) / 'cover.jpg'
    if target.is_file():
        return True
    try:
        with archive.open_url(artwork_url, timeout=45) as response:
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000 or not raw.startswith(b'\xff\xd8'):
            raise ValueError('封面不是 JPEG')
        target.write_bytes(raw)
        return True
    except Exception:
        return False


def unfollow(show):
    """Stop following a show.

    The library list is built from the catalogues found on disk, so dropping the
    registry entry alone would not hide the show. The catalogue folder is
    therefore MOVED to data/archive/ - never deleted - so downloaded audio,
    subtitles and notes stay recoverable.
    """
    slug = str(show or '').strip()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', slug):
        raise ValueError('节目标识无效')
    registry = [row for row in _read(paths.SHOWS_JSON, []) if row.get('id') != slug]
    archive.write_json(paths.SHOWS_JSON, registry)
    state = _read(paths.FEED_STATE_JSON, {})
    (state.get('shows') or {}).pop(slug, None)
    archive.write_json(paths.FEED_STATE_JSON, state)
    moved = False
    source = archive.folder(slug)
    if source.is_dir():
        target = paths.ARCHIVE / slug
        if target.exists():
            target = paths.ARCHIVE / (slug + '-' + time.strftime('%Y%m%d%H%M%S'))
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        moved = True
    return {'ok': True, 'id': slug, 'archived': moved,
            'where': f'data/archive/{slug}' if moved else '',
            'note': '节目文件已移入 data/archive，仍可手动移回 data/library 恢复。'}
