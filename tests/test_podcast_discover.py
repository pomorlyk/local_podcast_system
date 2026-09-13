"""Offline checks for AI discovery, following, and credential handling."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import paths
import podcast_archive as archive
import podcast_discover as discover
import podcast_llm as llm

SEARCH_PAYLOAD = {'results': [
    {'collectionId': 111, 'collectionName': 'Photography Weekly',
     'artistName': 'Someone', 'feedUrl': 'https://example.com/pw.rss',
     'artworkUrl600': 'https://example.com/pw.jpg', 'genres': ['Technology', 'Photography'],
     'trackCount': 120, 'releaseDate': '2025-06-01', 'country': 'USA',
     'collectionViewUrl': 'https://podcasts.apple.com/pw'},
    {'collectionId': 222, 'collectionName': 'Not Secure Feed',
     'artistName': 'Someone', 'feedUrl': 'http://example.com/insecure.rss',
     'genres': ['Technology'], 'trackCount': 5, 'releaseDate': '2025-01-01', 'country': 'USA'},
    {'collectionId': 333, 'collectionName': 'Generic Tech Daily',
     'artistName': 'Corp', 'feedUrl': 'https://example.com/tech.rss',
     'genres': ['Technology', 'News'], 'trackCount': 2500,
     'releaseDate': '2025-09-01', 'country': 'USA'},
]}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'library').mkdir()
        self.shows_file = self.root / 'shows.json'
        self.interests_file = self.root / 'interests.json'
        self.settings_file = self.root / 'discovery-settings.json'
        for target, name, value in (
            (paths, 'SHOWS_JSON', self.shows_file),
            (paths, 'INTERESTS_JSON', self.interests_file),
            (paths, 'DISCOVERY_SETTINGS', self.settings_file),
            (paths, 'ARCHIVE', self.root / 'archive'),
            (archive, 'LIBRARY', self.root / 'library'),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        archive.write_json(self.shows_file, [
            {'id': 'existing', 'name': 'Existing Show', 'category': '测试',
             'feed_url': 'https://example.com/pw.rss'}]),
        clean_env = patch.dict(os.environ, {'PODCAST_LLM_API_KEY': '',
                                            'PODCAST_LLM_BASE_URL': '',
                                            'PODCAST_LLM_MODEL': ''})
        clean_env.start()
        self.addCleanup(clean_env.stop)

    # ------------------------------------------------------------ interests
    def test_interests_default_then_cleaned(self):
        self.assertEqual(discover.interests()['keywords'], [])
        saved = discover.save_interests({'keywords': 'AI 编程，独立游戏、\n产品设计',
                                        'country': 'gb', 'auto_subscribe_limit': '4'})
        self.assertEqual(saved['keywords'], ['AI 编程', '独立游戏', '产品设计'])
        self.assertEqual(saved['country'], 'GB')
        self.assertEqual(saved['auto_subscribe_limit'], 4)
        for bad in ({'country': 'USA'}, {'country': '1'}, {'auto_subscribe_limit': 0},
                    {'auto_subscribe_limit': 99}, {'keywords': [str(i) for i in range(30)]}):
            with self.assertRaises(ValueError):
                discover.save_interests(bad)

    # --------------------------------------------------------------- search
    def test_search_keeps_only_https_feeds(self):
        with patch.object(discover, '_fetch_json', return_value=SEARCH_PAYLOAD):
            rows = discover.search('photography')
        self.assertEqual([r['name'] for r in rows], ['Photography Weekly', 'Generic Tech Daily'])
        self.assertEqual(rows[0]['apple_id'], 111)
        self.assertEqual(rows[0]['episodes'], 120)

    def test_search_rejects_blank_term(self):
        self.assertEqual(discover.search('   '), [])

    # ------------------------------------------------------------- ranking
    def test_name_match_outranks_genre_match(self):
        named = {'name': 'Photography Weekly', 'author': 'x', 'genres': ['Technology'],
                 'episodes': 100, 'released': '2025-01-01', 'country': 'USA', 'matched': ['photography']}
        generic = {'name': 'Generic Tech Daily', 'author': 'y', 'genres': ['Technology', 'News'],
                   'episodes': 2500, 'released': '2025-09-01', 'country': 'USA', 'matched': ['technology']}
        self.assertGreater(discover.heuristic_score(named, ['photography', 'technology'], []),
                           discover.heuristic_score(generic, ['photography', 'technology'], []))

    def test_excluded_keyword_pushes_a_show_down(self):
        row = {'name': 'True Crime Photography', 'author': 'x', 'genres': ['Photography'],
               'episodes': 20, 'released': '2025-01-01', 'country': 'USA', 'matched': ['photography']}
        keep = discover.heuristic_score(row, ['photography'], [])
        drop = discover.heuristic_score(row, ['photography'], ['crime'])
        self.assertLess(drop, keep)

    def test_recommend_marks_followed_shows_and_uses_heuristic_without_key(self):
        discover.save_interests({'keywords': 'photography'})
        with patch.object(discover, '_fetch_json', return_value=SEARCH_PAYLOAD):
            report = discover.recommend({})
        self.assertEqual(report['ranking'], 'heuristic')
        self.assertEqual(report['country'], 'US')
        names = {row['name']: row for row in report['candidates']}
        self.assertTrue(names['Photography Weekly']['already_followed'])
        self.assertFalse(names['Generic Tech Daily']['already_followed'])

    def test_recommend_needs_keywords(self):
        with self.assertRaises(ValueError):
            discover.recommend({})

    def test_ai_ranking_ignores_invented_ids(self):
        shortlist = [{'apple_id': 1, 'name': 'A', 'author': '', 'genres': [], 'episodes': 1,
                      'matched': []},
                     {'apple_id': 2, 'name': 'B', 'author': '', 'genres': [], 'episodes': 2,
                      'matched': []}]
        with patch.object(llm, 'chat_json', return_value=({'picks': [
                {'id': 2, 'reason': '很合适'}, {'id': 999, 'reason': '不存在的节目'}]}, {})):
            reasons = discover._ai_pick(shortlist, {'auto_subscribe_limit': 2}, ['x'])
        self.assertEqual(reasons, {'2': '很合适'})

    # ------------------------------------------------------------ following
    def test_subscribe_writes_registry_syncs_and_dedupes(self):
        calls = []

        def fake_sync(slug, url):
            calls.append((slug, url))
            archive.write_json(archive.folder(slug) / 'catalog.json', {
                'show_id': slug, 'title': slug, 'feed_url': url,
                'current_feed_ids': [], 'episodes': []})

        with patch.object(discover.archive, 'sync', side_effect=fake_sync), \
                patch.object(discover, '_save_cover', return_value=True) as cover:
            results = discover.subscribe([
                {'name': 'Photography Weekly', 'feed_url': 'https://example.com/new.rss',
                 'apple_id': 111, 'genres': ['Technology', 'Photography'],
                 'artwork_url': 'https://example.com/pw.jpg', 'reason': '命中主题',
                 'picked_by': 'ai'},
                {'name': 'Already Here', 'feed_url': 'https://example.com/pw.rss/', 'apple_id': 999},
                {'name': 'Broken Feed', 'feed_url': 'not-a-url', 'apple_id': 555},
            ])
        statuses = {r.get('name'): r['status'] for r in results}
        self.assertEqual(statuses['Photography Weekly'], 'followed')
        self.assertEqual(statuses['Already Here'], 'already_followed')
        self.assertEqual(statuses['Broken Feed'], 'failed')
        self.assertEqual(len(calls), 1)

        registry = json.loads(self.shows_file.read_text(encoding='utf-8'))
        entry = next(r for r in registry if r['id'] == 'photography-weekly')
        self.assertEqual(entry['feed_url'], 'https://example.com/new.rss')
        self.assertEqual(entry['category'], 'Technology')
        self.assertEqual(entry['added_by'], 'ai')
        self.assertEqual(entry['reason'], '命中主题')
        self.assertTrue(entry['added_at'])
        cover.assert_called_once()

    def test_slug_is_unique_and_url_safe(self):
        slug = discover._slug('《中文》名 — With Symbols!!', 7, set())
        self.assertRegex(slug, r'^[a-z0-9][a-z0-9_-]{0,79}$')
        self.assertEqual(discover._slug('Tech', 7, {'tech'}), 'tech-2')
        self.assertTrue(discover._slug('中文', 7, set()).startswith('show-'))

    def test_unfollow_moves_files_into_the_archive(self):
        folder = archive.folder('existing')
        archive.write_json(folder / 'catalog.json', {'show_id': 'existing', 'episodes': []})
        result = discover.unfollow('existing')
        self.assertTrue(result['archived'])
        self.assertTrue((self.root / 'archive' / 'existing' / 'catalog.json').is_file())
        self.assertFalse(folder.exists())
        self.assertEqual(json.loads(self.shows_file.read_text(encoding='utf-8')), [])
        with self.assertRaises(ValueError):
            discover.unfollow('../../etc')

    def test_auto_follows_then_reports_when_sync_fails(self):
        discover.save_interests({'keywords': 'photography', 'auto_subscribe_limit': 1})

        def fake_sync(slug, url):
            archive.write_json(archive.folder(slug) / 'catalog.json', {
                'show_id': slug, 'title': slug, 'feed_url': url,
                'current_feed_ids': [], 'episodes': []})

        with patch.object(discover, '_fetch_json', return_value=SEARCH_PAYLOAD), \
                patch.object(discover.archive, 'sync', side_effect=fake_sync), \
                patch.object(discover, '_save_cover', return_value=True):
            report = discover.auto({})
        followed = [r for r in report['subscribed'] if r['status'] == 'followed']
        # Photography Weekly is already followed in the fixture (same feed URL),
        # so the next best on-topic show must be the one that gets added.
        self.assertEqual([r['name'] for r in followed], ['Generic Tech Daily'])

        # Reset the registry so a fresh candidate exists, then make the feed sync fail.
        archive.write_json(self.shows_file, [
            {'id': 'existing', 'name': 'Existing Show', 'category': '测试',
             'feed_url': 'https://example.com/pw.rss'}])
        with patch.object(discover, '_fetch_json', return_value=SEARCH_PAYLOAD), \
                patch.object(discover.archive, 'sync', side_effect=OSError('offline')):
            broken = discover.auto({'limit': 1})
        self.assertFalse([r for r in broken['subscribed'] if r['status'] == 'followed'])
        self.assertIn('同步订阅源失败', broken['message'])

    # --------------------------------------------------------- credentials
    def test_settings_never_return_the_key(self):
        public = llm.public_settings()
        self.assertFalse(public['configured'])
        self.assertEqual(public['key_source'], 'none')
        self.assertTrue(public['env_hint'])
        self.assertFalse(any('key' == k or 'secret' in k for k in public))

    def test_save_and_remove_key_stores_ciphertext_only(self):
        saved = llm.save_settings({'provider': 'deepseek', 'model': 'deepseek-chat',
                                   'api_key': 'sk-secret-' + 'z' * 20})
        self.assertTrue(saved['configured'])
        self.assertEqual(saved['key_source'], 'local_file')
        self.assertNotIn('sk-secret', json.dumps(saved))
        self.assertNotIn('sk-secret', self.settings_file.read_text(encoding='utf-8'))
        self.assertFalse(llm.save_settings({'provider': 'deepseek', 'remove_key': True})['configured'])
        with self.assertRaises(ValueError):
            llm.credential()

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            llm.save_settings({'provider': 'nope'})
        with self.assertRaises(ValueError):
            llm.save_settings({'provider': 'compatible', 'base_url': 'http://insecure'})
        with self.assertRaises(ValueError):
            llm.save_settings({'provider': 'deepseek', 'api_key': 'short'})
        with self.assertRaises(ValueError):
            llm.save_settings({'provider': 'deepseek', 'model': 'm' * 200})
        with self.assertRaises(ValueError):
            llm.save_settings({'provider': 'deepseek', 'api_key': 'has space in it here'})

    def test_credential_comes_from_env_without_touching_the_repo(self):
        with self.assertRaises(ValueError):
            llm.credential()
        with patch.dict(os.environ, {'PODCAST_LLM_API_KEY': 'sk-env-' + 'e' * 20,
                                     'PODCAST_LLM_BASE_URL': 'https://llm.example.com/v1',
                                     'PODCAST_LLM_MODEL': 'my-model'}):
            self.assertEqual(llm.credential(), ('https://llm.example.com/v1', 'my-model',
                                                'sk-env-' + 'e' * 20))
        self.assertFalse(self.settings_file.exists())

    def test_chat_json_uses_bearer_auth_and_no_redirects(self):
        captured = {}

        class FakeResponse(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, request, timeout=None):
                captured['url'] = request.full_url
                captured['auth'] = request.headers.get('Authorization')
                captured['body'] = json.loads(request.data.decode('utf-8'))
                return FakeResponse(json.dumps(
                    {'choices': [{'finish_reason': 'stop',
                                  'message': {'content': '{"picks":[]}'}}],
                     'usage': {'total_tokens': 1}}).encode())

        with patch.dict(os.environ, {'PODCAST_LLM_API_KEY': 'sk-env-' + 'e' * 20}), \
                patch.object(llm.urllib.request, 'build_opener', return_value=FakeOpener()):
            parsed, usage = llm.chat_json('system', 'user')
        self.assertEqual(parsed, {'picks': []})
        self.assertEqual(usage, {'total_tokens': 1})
        self.assertEqual(captured['url'], 'https://api.deepseek.com/chat/completions')
        self.assertTrue(captured['auth'].startswith('Bearer sk-env-'))
        self.assertEqual(captured['body']['response_format'], {'type': 'json_object'})

    def test_truncated_model_answer_is_rejected(self):
        class FakeResponse(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, request, timeout=None):
                return FakeResponse(json.dumps(
                    {'choices': [{'finish_reason': 'length',
                                  'message': {'content': '{"picks":[]}'}}]}).encode())

        with patch.dict(os.environ, {'PODCAST_LLM_API_KEY': 'sk-env-' + 'e' * 20}), \
                patch.object(llm.urllib.request, 'build_opener', return_value=FakeOpener()):
            with self.assertRaises(ValueError):
                llm.chat_json('system', 'user')

    def test_model_http_errors_are_reported_in_chinese(self):
        import urllib.error

        class FakeOpener:
            def __init__(self, code):
                self.code = code

            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, self.code, 'err', {}, None)

        for code, fragment in ((401, '密钥'), (402, '余额'), (429, '频繁')):
            with patch.dict(os.environ, {'PODCAST_LLM_API_KEY': 'sk-env-' + 'e' * 20}), \
                    patch.object(llm.urllib.request, 'build_opener', return_value=FakeOpener(code)):
                with self.assertRaises(ValueError) as raised:
                    llm.chat_json('system', 'user')
            self.assertIn(fragment, str(raised.exception))


if __name__ == '__main__':
    unittest.main()
