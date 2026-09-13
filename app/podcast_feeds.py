"""Background subscription refresher: periodically asks every followed feed
whether a new episode exists, and records what is new.

This module is deliberately independent from the manual job queue in
podcast_server: it owns its own thread and its own state file, so the existing
"更新目录" button, download queue and transcription pipeline keep behaving
exactly as before.
"""
import datetime as dt
import json
import threading
import time

import paths
import podcast_archive as archive

LOCK = threading.RLock()
STOP = threading.Event()
DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60

STATUS = {'running': False, 'started_at': 0, 'finished_at': 0, 'current': '',
          'done': 0, 'total': 0, 'new_episodes': 0, 'trigger': ''}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read(path, default):
    """Read the state file, tolerating a transient lock.

    Windows antivirus and indexers can briefly hold a freshly written file, and
    a state file is not worth failing a request over: fall back to the default.
    """
    for attempt in range(2):
        try:
            return archive.read_json(path, default) or default
        except OSError:
            if attempt:
                return default
            time.sleep(0.05)
    return default


def read_settings():
    state = _read(paths.FEED_STATE_JSON, {})
    settings = state.get('settings') or {}
    return {
        'enabled': bool(settings.get('enabled', False)),
        'interval_minutes': int(settings.get('interval_minutes', DEFAULT_INTERVAL_MINUTES)),
        'last_run': settings.get('last_run', 0),
    }


def save_settings(data):
    with LOCK:
        state = _read(paths.FEED_STATE_JSON, {})
        settings = state.setdefault('settings', {})
        if 'enabled' in data:
            settings['enabled'] = bool(data['enabled'])
        if 'interval_minutes' in data:
            try:
                minutes = int(data['interval_minutes'])
            except (TypeError, ValueError):
                raise ValueError('检查间隔必须是分钟数') from None
            if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
                raise ValueError(f'检查间隔请设在 {MIN_INTERVAL_MINUTES}–{MAX_INTERVAL_MINUTES} 分钟之间')
            settings['interval_minutes'] = minutes
        archive.write_json(paths.FEED_STATE_JSON, state)
    return read_settings()


def registry():
    return _read(paths.SHOWS_JSON, [])


def _catalog(slug):
    try:
        return archive.read_json(archive.folder(slug) / 'catalog.json', {}) or {}
    except ValueError:
        return {}


def state_for(slug, state=None):
    state = state if state is not None else _read(paths.FEED_STATE_JSON, {})
    return (state.get('shows') or {}).get(slug) or {}


def refresh(show_ids=None, trigger='manual', progress=None):
    """Sync every followed feed and record episode ids that were not seen before.

    Returns a summary dict. Never raises for a single failing feed: one dead
    publisher must not stop the rest of the subscriptions.
    """
    with LOCK:
        state = _read(paths.FEED_STATE_JSON, {})
        shows_state = state.setdefault('shows', {})
        registry_rows = registry()
        if show_ids is None:
            targets = [row['id'] for row in registry_rows if row.get('id')]
        else:
            targets = [sid for sid in show_ids if sid]
        names = {row['id']: row.get('name', row['id']) for row in registry_rows}
        with LOCK:
            STATUS.update({'running': True, 'started_at': time.time(), 'finished_at': 0,
                           'current': '', 'done': 0, 'total': len(targets),
                           'new_episodes': 0, 'trigger': trigger})
        summary = {'checked': 0, 'failed': 0, 'new_episodes': 0, 'shows': {}}

        for index, slug in enumerate(targets, 1):
            STATUS['current'] = names.get(slug, slug)
            STATUS['done'] = index - 1
            if progress:
                progress(slug, index, len(targets))
            entry = shows_state.setdefault(slug, {})
            known = set(entry.get('known_ids') or [])
            previous = _catalog(slug)
            feed_url = previous.get('feed_url')
            if not feed_url:
                record = next((row for row in registry_rows if row.get('id') == slug), None)
                feed_url = (record or {}).get('feed_url')
            try:
                if not feed_url:
                    raise ValueError('缺少订阅地址')
                archive.sync(slug, feed_url)
                catalog = _catalog(slug)
                current = list(catalog.get('current_feed_ids') or [])
                if not known:
                    # First observation: adopt the existing catalogue instead of
                    # reporting the whole back-list as brand new.
                    fresh = []
                else:
                    fresh = [eid for eid in current if eid not in known]
                by_id = {row['episode_id']: row for row in catalog.get('episodes', [])}
                new_rows = [{'episode_id': eid,
                             'title': (by_id.get(eid) or {}).get('title') or '',
                             'published_at': (by_id.get(eid) or {}).get('published_at') or '',
                             'seen': False,
                             'found_at': now()} for eid in fresh]
                if new_rows:
                    merged = {row['episode_id']: row for row in (entry.get('new') or [])}
                    for row in new_rows:
                        previous_row = merged.get(row['episode_id'])
                        if previous_row:
                            row['seen'] = previous_row.get('seen', False)
                        merged[row['episode_id']] = row
                    entry['new'] = list(merged.values())
                entry.update({'known_ids': sorted(set(known) | set(current)),
                              'last_checked': now(), 'last_ok': True, 'error': '',
                              'title': catalog.get('title') or names.get(slug, slug),
                              'episodes_in_feed': len(current),
                              'new_count': sum(1 for row in (entry.get('new') or []) if not row.get('seen'))})
                summary['checked'] += 1
                summary['new_episodes'] += len(new_rows)
                summary['shows'][slug] = {'ok': True, 'new': len(new_rows), 'feed_entries': len(current)}
            except Exception as exc:  # one bad feed must not abort the sweep
                entry.update({'last_checked': now(), 'last_ok': False, 'error': str(exc)[:300],
                              'title': names.get(slug, slug)})
                summary['failed'] += 1
                summary['shows'][slug] = {'ok': False, 'error': str(exc)[:300]}
            archive.write_json(paths.FEED_STATE_JSON, state)

        state.setdefault('settings', {})['last_run'] = time.time()
        archive.write_json(paths.FEED_STATE_JSON, state)
        STATUS.update({'running': False, 'finished_at': time.time(), 'current': '',
                       'done': len(targets), 'new_episodes': summary['new_episodes']})
        return summary


def refresh_async(show_ids=None, trigger='manual'):
    if STATUS.get('running'):
        return {'ok': True, 'already_running': True}
    thread = threading.Thread(target=lambda: refresh(show_ids, trigger), daemon=True)
    thread.start()
    return {'ok': True, 'started': True}


MAX_KEPT_PER_SHOW = 200


def mark_seen(show=None, episode_ids=None, everything=False):
    """Mark found episodes as read.

    Rows are marked, not deleted, so a re-sync cannot resurface an episode the
    listener already dismissed. With no explicit ids, the whole show (or every
    show when everything=True) is marked.
    """
    with LOCK:
        state = _read(paths.FEED_STATE_JSON, {})
        shows_state = state.setdefault('shows', {})
        targets = [show] if show else list(shows_state)
        wanted = set(episode_ids or [])
        for slug in targets:
            entry = shows_state.get(slug)
            if not entry:
                continue
            kept = []
            for row in (entry.get('new') or []):
                if everything or not wanted or row.get('episode_id') in wanted:
                    row['seen'] = True
                kept.append(row)
            entry['new'] = kept[-MAX_KEPT_PER_SHOW:]
            entry['new_count'] = sum(1 for row in entry['new'] if not row.get('seen'))
        archive.write_json(paths.FEED_STATE_JSON, state)
    return updates()


def updates():
    """Everything the UI needs to show what is new since the last visit."""
    state = _read(paths.FEED_STATE_JSON, {})
    shows_state = state.get('shows') or {}
    registry_rows = registry()
    covers = {row['id']: (archive.folder(row['id']) / 'cover.jpg').is_file()
              for row in registry_rows if row.get('id')}
    rows, total = [], 0
    for slug, name, category in [(r['id'], r.get('name', r['id']), r.get('category', '播客'))
                                 for r in registry_rows if r.get('id')]:
        entry = shows_state.get(slug) or {}
        unseen = [row for row in (entry.get('new') or []) if not row.get('seen')]
        total += len(unseen)
        rows.append({
            'id': slug, 'name': name, 'category': category,
            'cover': f'/covers/{slug}.jpg' if covers.get(slug) else None,
            'last_checked': entry.get('last_checked'), 'last_ok': entry.get('last_ok'),
            'error': entry.get('error', ''), 'feed_entries': entry.get('episodes_in_feed', 0),
            'new': sorted(unseen, key=lambda r: r.get('published_at') or '', reverse=True)[:50],
            'new_episodes': unseen and [
                {'key': f"{slug}:{row['episode_id']}", 'title': row.get('title', ''),
                 'date': _date(row.get('published_at')), 'found_at': row.get('found_at')}
                for row in sorted(unseen, key=lambda r: r.get('published_at') or '', reverse=True)[:50]
            ] or [],
            'seen_count': len(entry.get('new') or []) - len(unseen),
        })
    return {'unseen_total': total, 'shows': rows, 'settings': read_settings(), 'status': dict(STATUS)}


def _date(value):
    try:
        return dt.datetime.strptime(value, '%a, %d %b %Y %H:%M:%S %z').date().isoformat()
    except (TypeError, ValueError):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(value).date().isoformat()
        except (TypeError, ValueError):
            return ''


def scheduler():
    """Wake up on a fixed tick and run a sweep when the interval has elapsed."""
    STOP.clear()
    while not STOP.is_set():
        try:
            settings = read_settings()
            if settings['enabled'] and not STATUS.get('running'):
                elapsed = time.time() - (settings.get('last_run') or 0)
                if elapsed >= settings['interval_minutes'] * 60:
                    refresh(trigger='scheduled')
        except Exception:
            pass  # a broken state file must not kill the loop
        STOP.wait(30)


def start_scheduler():
    thread = threading.Thread(target=scheduler, daemon=True, name='podcast-feed-scheduler')
    thread.start()
    return thread
