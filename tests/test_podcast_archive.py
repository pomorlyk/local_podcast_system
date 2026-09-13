"""Offline checks for source parsing, idempotency, and revision-safe schema."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import podcast_archive as app

FEED = b'''<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
 xmlns:p="https://podcastindex.org/namespace/1.0"><channel><title>Demo</title>
 <item><title>Original title</title><itunes:title>Alias title</itunes:title>
 <guid>stable-guid</guid><description>Show notes only</description>
 <enclosure url="https://example.com/audio.mp3" type="audio/mpeg" length="0"/>
 <p:transcript url="https://example.com/captions.vtt" type="text/vtt"/>
 </item></channel></rss>'''


class Response(io.BytesIO):
    status = 200
    url = 'https://example.com/feed'
    headers = {'Content-Type': 'application/xml'}


class ArchiveTests(unittest.TestCase):
    def test_namespace_and_identity(self):
        a = app.parse_feed(FEED, 'https://example.com/feed')['entries'][0]
        b = app.parse_feed(FEED.replace(b'Original title', b'New title'), 'https://example.com/feed')['entries'][0]
        self.assertEqual(a['title'], 'Original title')
        self.assertEqual(a['episode_id'], b['episode_id'])
        self.assertEqual(a['transcript_links'][0]['type'], 'text/vtt')
        self.assertEqual(a['transcript_status'], 'publisher_link_found')
        self.assertEqual(a['audio_enclosure']['length'], '0')

    def test_missing_transcript_not_filled_from_description(self):
        raw = FEED.replace(b'<p:transcript url="https://example.com/captions.vtt" type="text/vtt"/>', b'')
        row = app.parse_feed(raw, 'https://example.com/feed')['entries'][0]
        self.assertEqual(row['transcript_status'], 'not_in_feed')
        self.assertEqual(row['transcript_links'], [])

    def test_reject_unsafe_input(self):
        with self.assertRaises(ValueError):
            app.folder('../escape')
        with self.assertRaises(ValueError):
            app.parse_feed(b'<!DOCTYPE rss><rss/>', 'https://example.com/feed')

    def test_resync_preserves_learning_and_disappeared_episode(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LIBRARY', Path(tmp)):
            with patch.object(app, 'open_url', return_value=Response(FEED)):
                app.sync('demo', 'https://example.com/feed')
            catalog_path = Path(tmp) / 'demo/catalog.json'
            catalog = app.read_json(catalog_path)
            catalog['episodes'][0]['learning_status'] = 'in_progress'
            app.write_json(catalog_path, catalog)
            with patch.object(app, 'open_url', return_value=Response(FEED.replace(b'Original title', b'Renamed'))):
                app.sync('demo', 'https://example.com/feed')
            catalog = app.read_json(catalog_path)
            self.assertEqual(len(catalog['episodes']), 1)
            self.assertEqual(catalog['episodes'][0]['learning_status'], 'in_progress')
            with patch.object(app, 'open_url', return_value=Response(b'<rss><channel><title>Demo</title></channel></rss>')):
                app.sync('demo', 'https://example.com/feed')
            self.assertEqual(len(app.read_json(catalog_path)['episodes']), 1)

    def test_schema_search_and_foreign_key(self):
        schema = Path(__file__).resolve().parents[1] / 'data/index/schema.sql'
        db = sqlite3.connect(':memory:')
        db.executescript(schema.read_text(encoding='utf-8'))
        db.execute("INSERT INTO shows(id,title,feed_url) VALUES('s','show','https://example.com/feed')")
        db.execute("INSERT INTO episodes(id,show_id,title,metadata_json) VALUES('e','s','ep','{}')")
        for asset in ('a1', 'a2'):
            db.execute("INSERT INTO assets(id,episode_id,sha256,relative_path,bytes,source_url,fetched_at) VALUES(?,'e',?,?,1,'https://example.com','today')", (asset, asset, asset+'.mp3'))
        db.execute("INSERT INTO transcripts(id,episode_id,asset_id,source_kind,source_file_sha256,revision,language,alignment_status,quality_status,relative_path,created_at) VALUES('t','e','a1','local_asr','hash',1,'en','unaligned','unreviewed','t.json','today')")
        db.execute("INSERT INTO segments(id,transcript_id,position,text_en) VALUES('seg','t',0,'camera lens')")
        self.assertEqual(db.execute("SELECT segment_id FROM segment_search WHERE segment_search MATCH 'camera'").fetchone()[0], 'seg')
        db.execute("UPDATE segments SET text_en='console game' WHERE id='seg'")
        self.assertEqual(db.execute("SELECT count(*) FROM segment_search WHERE segment_search MATCH 'camera'").fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO bookmarks(id,asset_id,transcript_id,start_ms,end_ms,created_at) VALUES('b','a2','t',0,1000,'today')")
        db.close()

    def test_import_and_corruption_detection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LIBRARY', Path(tmp) / 'library'):
            with patch.object(app, 'open_url', return_value=Response(FEED)):
                app.sync('demo', 'https://example.com/feed')
            source = Path(tmp) / 'signature-fixture.mp3'
            source.write_bytes(b'ID3' + bytes(40))  # Signature fixture, not a decodable audio claim.
            result = app.import_audio('demo', 'latest', source)
            self.assertTrue(app.verify('demo')['all_ok'])
            with self.assertRaises(ValueError):
                app.import_audio('demo', 'latest', source)
            Path(result['path']).write_bytes(b'corrupt')
            self.assertFalse(app.verify('demo')['all_ok'])

    def test_partial_receipt_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'LIBRARY', Path(tmp) / 'library'):
            with patch.object(app, 'open_url', return_value=Response(FEED)):
                app.sync('demo', 'https://example.com/feed')
            source = Path(tmp) / 'partial.mp3'
            source.write_bytes(b'ID3' + bytes(40))
            receipt = Path(tmp) / 'receipt.json'
            app.write_json(receipt, {'Status': 206, 'Length': 43, 'Headers': {'Content-Length': ['43'], 'Content-Range': ['bytes 0-42/1000']}})
            with self.assertRaises(ValueError):
                app.import_audio('demo', 'latest', source, receipt)


if __name__ == '__main__':
    unittest.main()
