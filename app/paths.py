"""Single source of truth for where things live inside this repository.

The original podcast system lived inside a larger study workspace and derived
every path from a relative folder tree. This module keeps that behaviour while
letting the podcast system stand alone as its own project.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP = ROOT / 'app'
WEB = ROOT / 'web'
DATA = ROOT / 'data'
INDEX = DATA / 'index'
LIBRARY = DATA / 'library'
ARCHIVE = DATA / 'archive'   # shows you stopped following; files are kept, not deleted
CACHE = DATA / 'cache'
DOCS = ROOT / 'docs'

DB = DATA / 'ui.sqlite3'
SHOWS_JSON = INDEX / 'shows.json'
SCHEMA_SQL = INDEX / 'schema.sql'
TRANSLATION_SETTINGS = INDEX / 'translation-settings.json'
DISCOVERY_SETTINGS = INDEX / 'discovery-settings.json'
INTERESTS_JSON = INDEX / 'interests.json'
FEED_STATE_JSON = INDEX / 'feed-state.json'
FETCH_PS1 = APP / 'podcast_fetch.ps1'

# Optional local speech-to-text. The whisper model is ~1.6 GB, so it is never
# committed and never bundled; point these two variables at your own install.
ASR_MODEL = Path(os.environ.get('PODCAST_ASR_MODEL', str(ROOT / 'models/whisper-large-v3-turbo')))
_default_asr = ROOT / '.venv-asr' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
ASR_PYTHON = Path(os.environ.get('PODCAST_ASR_PYTHON', str(_default_asr)))

for folder in (DATA, INDEX, LIBRARY, CACHE):
    folder.mkdir(parents=True, exist_ok=True)
