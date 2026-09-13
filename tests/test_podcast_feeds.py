"""Offline checks for the subscription refresher (new-episode detection)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

import json
import tempfile
import unittest
from unittest.mock import patch

import paths
import podcast_archive as archive
import podcast_feeds as feeds


class FeedRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'library').mkdir()
        self.state_file = self.root / 'feed-state.json'
        self.shows_file = self.root / 'shows.json'
        for target, name, value in (
            (paths, 'FEED_STATE_JSON', self.state_file),
            (paths, 'SHOWS_JSON', self.shows_file),
            (archive, 'LIBRARY', self.root / 'library'),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        archive.write_json(self.shows_file, [
            {'id': 'alpha', 'name': 'Alpha Show', 'category': '测试', 'feed_url': 'https://example.com/alpha.rss'},
            {'id': 'beta', 'name': 'Beta Show', 'category': '测试', 'feed_url': 'https://example.com/beta.rss'},
        ])
        self.feeds = {'alpha': ['a1', 'a2'], 'beta': ['b1']}
        self.fail = set()

    def fake_sync(self, slug, url):
        if slug in self.fail:
            raise OSError('feed unreachable')
        archive.write_json(archive.folder(slug) / 'catalog.json', {
            'schema_version': 1, 'show_id': slug, 'title': slug.title(), 'feed_url': url,
            'current_feed_ids': list(self.feeds[slug]),
            'episodes': [{'episode_id': eid, 'title': 'Episode ' + eid, 'published_at': 'Mon, 01 Sep 2025 10:00:00 +0000'}
                         for eid in self.feeds[slug]],
        })

    def test_first_sweep_seeds_without_reporting_backlog(self):
        with patch.object(feeds.archive, 'sync', side_effect=self.fake_sync):
            summary = feeds.refresh()
        self.assertEqual(summary['checked'], 2)
        self.assertEqual(summary['new_episodes'], 0)
        payload = feeds.updates()
        self.assertEqual(payload['unseen_total'], 0)
        self.assertEqual(len(payload['shows']), 2)
        state = json.loads(self.state_file.read_text(encoding='utf-8'))
        self.assertEqual(sorted(state['shows']['alpha']['known_ids']), ['a1', 'a2'])
        self.assertTrue(all(row['last_ok'] for row in payload['shows']))

    def test_new_episode_is_reported_once_and_cleared_by_mark_seen(self):
        with patch.object(feeds.archive, 'sync', side_effect=self.fake_sync):
            feeds.refresh()
            self.feeds['alpha'].insert(0, 'a3')
            self.fake_sync('alpha', 'https://example.com/alpha.rss')
            summary = feeds.refresh(['alpha'])
        self.assertEqual(summary['new_episodes'], 1)
        payload = feeds.updates()
        self.assertEqual(payload['unseen_total'], 1)
        row = next(s for s in payload['shows'] if s['id'] == 'alpha')
        self.assertEqual(len(row['new_episodes']), 1)
        self.assertEqual(row['new_episodes'][0]['key'], 'alpha:a3')
        self.assertEqual(row['new_episodes'][0]['date'], '2025-09-01')
        self.assertTrue(row['new_episodes'][0]['title'].startswith('Episode'))

        after = feeds.mark_seen('alpha')
        self.assertEqual(after['unseen_total'], 0)
        state = json.loads(self.state_file.read_text(encoding='utf-8'))
        self.assertEqual(len(state['shows']['alpha']['new']), 1)  # kept, but marked read

        # A second sweep must not re-announce the same id.
        with patch.object(feeds.archive, 'sync', side_effect=self.fake_sync):
            again = feeds.refresh(['alpha'])
        self.assertEqual(again['new_episodes'], 0)

    def test_one_broken_feed_does_not_stop_the_sweep(self):
        self.fail = {'alpha'}
        with patch.object(feeds.archive, 'sync', side_effect=self.fake_sync):
            summary = feeds.refresh()
        self.assertEqual(summary['checked'], 1)
        self.assertEqual(summary['failed'], 1)
        payload = feeds.updates()
        broken = next(s for s in payload['shows'] if s['id'] == 'alpha')
        self.assertFalse(broken['last_ok'])
        self.assertIn('unreachable', broken['error'])
        healthy = next(s for s in payload['shows'] if s['id'] == 'beta')
        self.assertTrue(healthy['last_ok'])

    def test_settings_are_validated(self):
        self.assertFalse(feeds.read_settings()['enabled'])
        self.assertEqual(feeds.save_settings({'enabled': True, 'interval_minutes': 60}),
                         {'enabled': True, 'interval_minutes': 60, 'last_run': 0})
        for bad in (1, 2000, 'soon'):
            with self.assertRaises(ValueError):
                feeds.save_settings({'interval_minutes': bad})
        self.assertTrue(feeds.read_settings()['enabled'])

    def test_async_refresh_refuses_to_double_start(self):
        feeds.STATUS['running'] = True
        try:
            self.assertEqual(feeds.refresh_async(), {'ok': True, 'already_running': True})
        finally:
            feeds.STATUS['running'] = False


if __name__ == '__main__':
    unittest.main()
