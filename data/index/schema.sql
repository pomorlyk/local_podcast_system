-- Design schema v0.1. Local application not yet implemented.
-- Integer millisecond positions; media on disk; multiple revisions are preserved.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS shows (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, feed_url TEXT NOT NULL UNIQUE,
  website TEXT, tags_json TEXT NOT NULL DEFAULT '[]', last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY, show_id TEXT NOT NULL REFERENCES shows(id), guid TEXT,
  title TEXT NOT NULL, published_at TEXT, description_text TEXT,
  metadata_json TEXT NOT NULL, UNIQUE(show_id, guid)
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id),
  sha256 TEXT NOT NULL, relative_path TEXT NOT NULL, bytes INTEGER NOT NULL CHECK(bytes>0),
  source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
  duration_ms INTEGER CHECK(duration_ms>0), decode_status TEXT NOT NULL DEFAULT 'unchecked',
  UNIQUE(episode_id, sha256), UNIQUE(episode_id, id)
);
CREATE TABLE IF NOT EXISTS transcripts (
  id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, asset_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('publisher','platform_auto','local_asr','user_import')),
  source_url TEXT, source_file_sha256 TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0), parent_id TEXT REFERENCES transcripts(id),
  language TEXT NOT NULL, model_config_json TEXT,
  alignment_status TEXT NOT NULL CHECK(alignment_status IN ('unaligned','machine_aligned','sample_checked')),
  quality_status TEXT NOT NULL CHECK(quality_status IN ('unreviewed','partly_reviewed','reviewed')),
  relative_path TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(episode_id,asset_id) REFERENCES assets(episode_id,id),
  UNIQUE(asset_id,id)
);
CREATE TABLE IF NOT EXISTS segments (
  id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL REFERENCES transcripts(id),
  position INTEGER NOT NULL, start_ms INTEGER, end_ms INTEGER,
  speaker_label TEXT, text_en TEXT NOT NULL, review_note TEXT,
  CHECK((start_ms IS NULL AND end_ms IS NULL) OR
    (start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms>=0 AND end_ms>start_ms)),
  UNIQUE(transcript_id,position)
);
CREATE VIRTUAL TABLE IF NOT EXISTS segment_search USING fts5(
  segment_id UNINDEXED, transcript_id UNINDEXED, text_en,
  tokenize='unicode61'
);
-- Synchronize in the same transaction; English full-text search first.
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
  INSERT INTO segment_search(segment_id,transcript_id,text_en) VALUES(new.id,new.transcript_id,new.text_en);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
  DELETE FROM segment_search WHERE segment_id=old.id;
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
  DELETE FROM segment_search WHERE segment_id=old.id;
  INSERT INTO segment_search(segment_id,transcript_id,text_en) VALUES(new.id,new.transcript_id,new.text_en);
END;
CREATE TABLE IF NOT EXISTS progress (
  asset_id TEXT PRIMARY KEY REFERENCES assets(id), position_ms INTEGER NOT NULL DEFAULT 0 CHECK(position_ms>=0),
  speed REAL NOT NULL DEFAULT 1 CHECK(speed>0), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listening_sessions (
  id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), started_at TEXT NOT NULL,
  ended_at TEXT, played_ranges_json TEXT NOT NULL DEFAULT '[]',
  -- Actual play intervals are evidence of playback, never of comprehension.
  device_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookmarks (
  id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id),
  transcript_id TEXT, start_ms INTEGER NOT NULL CHECK(start_ms>=0),
  end_ms INTEGER NOT NULL, label TEXT, note TEXT, created_at TEXT NOT NULL,
  CHECK(end_ms>start_ms),
  FOREIGN KEY(asset_id,transcript_id) REFERENCES transcripts(asset_id,id)
);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
  expression TEXT NOT NULL, context_text TEXT NOT NULL, explanation_zh TEXT,
  my_example TEXT, generated_example TEXT, explanation_provenance_json TEXT,
  status TEXT NOT NULL CHECK(status IN ('new','unclear','understood','used','paused')),
  next_review_at TEXT, interval_days INTEGER NOT NULL DEFAULT 1 CHECK(interval_days>=0)
);
CREATE TABLE IF NOT EXISTS review_events (
  id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id), reviewed_at TEXT NOT NULL,
  response TEXT NOT NULL CHECK(response IN ('unclear','understood','used','skip')),
  evidence_note TEXT, recording_id TEXT REFERENCES recordings(id), next_review_at TEXT
);
CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY, bookmark_id TEXT REFERENCES bookmarks(id),
  relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
  reflection TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, job_type TEXT NOT NULL, target_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE, input_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, error_detail TEXT,
  started_at TEXT, finished_at TEXT
);
