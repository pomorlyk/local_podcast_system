"""Small, standard-library-only podcast acquisition proof. No ASR or paid services.

sync: snapshot a public RSS feed and keep an append-only episode catalogue.
download: archive ONE enclosure, with a content hash and immutable version.
verify: verify all recorded local media hashes. No automatic bulk downloads.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from email.message import Message
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import paths

ROOT = paths.ROOT
LIBRARY = paths.LIBRARY
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def folder(slug):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", slug):
        raise ValueError("Show slug must be lowercase ASCII letters/digits/_/-.")
    return LIBRARY / slug


def request(url):
    if not url.startswith("https://"):
        raise ValueError("Use a public HTTPS URL.")
    headers = {"User-Agent": "PersonalPodcastLibrary/0.1", "Accept-Encoding": "identity"}
    if ".mp3" in url or ".m4a" in url:
        headers["Range"] = "bytes=0-"
    return urllib.request.Request(url, headers=headers)


class WindowsResponse:
    """Use Windows' network stack when Python TLS cannot reach a public feed.

    No credentials are copied and certificate checks remain enabled. The helper
    stages the response under the project's cache before Python validation.
    """
    def __init__(self, url):
        request(url)
        cache = paths.CACHE / "podcast_network"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        base = Path(self.temp.name)
        config = base / "request.json"
        write_json(config, {"url": url, "output": str(base / "data"), "info": str(base / "info.json"),
                            "audio": ".mp3" in url or ".m4a" in url})
        try:
            shell = shutil.which("pwsh")
            if not shell:
                raise ValueError("PowerShell 7 is needed for the Windows network fallback.")
            completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File",
                                        str(Path(__file__).with_name("podcast_fetch.ps1")),
                                        "-RequestPath", str(config)], capture_output=True, timeout=210,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if completed.returncode:
                raise OSError(completed.stderr.decode("utf-8", errors="replace")[-1800:])
            info = json.loads((base / "info.json").read_text(encoding="utf-8-sig"))
            self.status = info["status"]
            self.url = info["url"] or url
            self.headers = Message()
            for k, v in info["headers"].items():
                self.headers[k] = v
            self.stream = (base / "data").open("rb")
        except Exception:
            self.temp.cleanup()
            raise

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()
        self.temp.cleanup()


def open_url(url, timeout):
    try:
        return urllib.request.urlopen(request(url), timeout=timeout)
    except urllib.error.URLError:
        if sys.platform != "win32":
            raise
        return WindowsResponse(url)


def local_tag(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw, feed_url):
    # Standard-library XML; disallow DTD/entity declarations in untrusted feeds.
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not supported.")
    channel = ET.fromstring(raw).find("channel")
    if channel is None:
        raise ValueError("Expected RSS 2.0 channel.")
    entries = []
    for item in channel.findall("item"):
        enclosures = [e.attrib for e in item.findall("enclosure")]
        audio = next((e for e in enclosures if e.get("type", "").startswith("audio/")), None)
        guid = item.findtext("guid")
        # Do not identify by title: publishers can rename episodes.
        identity = guid or (audio or {}).get("url") or item.findtext("link")
        if not identity:
            continue
        eid = hashlib.sha256(identity.encode()).hexdigest()[:20]
        transcripts = [e.attrib for e in item if local_tag(e.tag) == "transcript"]
        chapters = [e.attrib for e in item if local_tag(e.tag) == "chapters"]
        description = item.findtext("description") or ""
        entries.append({
            "episode_id": eid, "guid": guid, "identity_basis": "guid" if guid else "url_fallback",
            "title": item.findtext("title"), "published_at": item.findtext("pubDate"),
            "episode_number": item.findtext("itunes:episode", namespaces=NS),
            "declared_duration": item.findtext("itunes:duration", namespaces=NS),
            "episode_url": item.findtext("link"), "audio_enclosure": audio,
            "show_notes_html": description,
            "show_notes_text": html.unescape(re.sub(r"<[^>]+>", " ", description)),
            "transcript_links": transcripts, "chapter_links": chapters,
            "transcript_status": "publisher_link_found" if transcripts else "not_in_feed",
            "learning_status": "not_started", "last_seen_at": now(),
        })
    return {"title": channel.findtext("title"), "website": channel.findtext("link"),
            "feed_url": feed_url, "language": channel.findtext("language"), "entries": entries}


def sync(slug, url):
    target = folder(slug)
    with open_url(url, timeout=45) as response:
        raw = response.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("Feed exceeds 20 MiB.")
        http = {"status": response.status, "final_url": response.url,
                "content_type": response.headers.get("Content-Type")}
    parsed = parse_feed(raw, url)
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = target / "feed_snapshots" / (digest + ".xml")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if not snapshot.exists():
        snapshot.write_bytes(raw)
    old = read_json(target / "catalog.json", {"episodes": []})
    merged = {row["episode_id"]: row for row in old["episodes"]}
    for row in parsed["entries"]:
        existing = merged.get(row["episode_id"], {})
        row["learning_status"] = existing.get("learning_status", "not_started")
        row["first_seen_at"] = existing.get("first_seen_at", now())
        merged[row["episode_id"]] = {**existing, **row}
    current_ids = [row["episode_id"] for row in parsed["entries"]]
    catalog = {"schema_version": 1, "show_id": slug, "title": parsed["title"],
               "website": parsed["website"], "feed_url": url, "fetched_at": now(),
               "current_feed_ids": current_ids, "episodes": list(merged.values())}
    write_json(target / "catalog.json", catalog)
    proof = {"checked_at": now(), "http": http, "feed_sha256": digest,
             "title": parsed["title"], "current_feed_entries": len(current_ids),
             "retained_catalog_entries": len(merged),
             "entries_with_audio": sum(bool(e["audio_enclosure"]) for e in parsed["entries"]),
             "entries_with_transcript_links": sum(bool(e["transcript_links"]) for e in parsed["entries"]),
             "entries_with_chapter_links": sum(bool(e["chapter_links"]) for e in parsed["entries"]),
             "latest": [{k: e[k] for k in ("episode_id", "title", "published_at", "declared_duration", "episode_number")} for e in parsed["entries"][:3]],
             "scope": "Only current RSS contents were inspected; absence is not proof that no transcript exists elsewhere."}
    write_json(target / "source_audit.json", proof)
    return proof


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(slug, episode_id, max_mb, direct_megaphone=False):
    target = folder(slug)
    catalog = read_json(target / "catalog.json")
    if not catalog:
        raise ValueError("Run sync first.")
    if episode_id == "latest":
        episode_id = catalog["current_feed_ids"][0]
    episode = next((e for e in catalog["episodes"] if e["episode_id"] == episode_id), None)
    if not episode or not episode["audio_enclosure"]:
        raise ValueError("Episode/audio not found.")
    dest = target / "episodes" / episode_id
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / "asset.json"
    existing = read_json(manifest_path)
    if existing:
        saved = dest / existing["filename"]
        if saved.exists() and hash_file(saved) == existing["sha256"]:
            return {"status": "already_archived", "path": str(saved), "sha256": existing["sha256"]}
        raise ValueError("Existing audio missing/changed. Keep manifest for investigation; do not replace it silently.")
    partial = dest / "audio.part"
    # Restart interrupted downloads: DAI hosts can change bytes between requests;
    # never append a different advertising variant through naive Range resume.
    url = episode["audio_enclosure"]["url"]
    source_url = url
    if direct_megaphone:
        # Some public RSS URLs wrap a literal Megaphone destination in analytics
        # redirects. Use only that explicit destination, never guess a media ID.
        match = re.search(r"(?:https://|/)traffic\.megaphone\.fm/([A-Za-z0-9_-]+\.mp3)(?:\?.*)?$", url)
        if not match:
            raise ValueError("The enclosure does not contain a supported explicit Megaphone destination.")
        url = "https://traffic.megaphone.fm/" + match.group(1)
    h = hashlib.sha256()
    total = 0
    with open_url(url, timeout=60) as response:
        mime = response.headers.get("Content-Type", "").split(";")[0].strip()
        if mime not in {"audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "application/octet-stream"}:
            raise ValueError(f"Unexpected audio content type: {mime}")
        expected = response.headers.get("Content-Length")
        if expected and int(expected) > max_mb * 1024 * 1024:
            raise ValueError("Audio exceeds download size cap.")
        first = b""
        with partial.open("wb") as stream:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                if not first:
                    first = chunk[:16]
                total += len(chunk)
                if total > max_mb * 1024 * 1024:
                    raise ValueError("Audio exceeds download size cap.")
                stream.write(chunk)
                h.update(chunk)
        if expected and total != int(expected):
            raise ValueError("Incomplete HTTP body.")
        content_range = response.headers.get("Content-Range")
        if content_range:
            match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
            if not match or int(match.group(1)) + 1 != total or int(match.group(2)) != total:
                raise ValueError("Partial response is not a complete audio file.")
        mp3 = first.startswith(b"ID3") or (len(first) > 1 and first[0] == 255 and first[1] & 224 == 224)
        mp4 = first[4:8] == b"ftyp"
        if not total or not (mp3 or mp4):
            raise ValueError("File signature is not recognized MP3/M4A.")
        digest = h.hexdigest()
        filename = "audio_" + digest[:16] + (".mp3" if mp3 else ".m4a")
        final = dest / filename
        partial.replace(final)
        manifest = {"episode_id": episode_id, "title": episode["title"], "filename": filename,
                    "sha256": digest, "bytes": total, "mime": mime, "source_url": source_url,
                    "requested_url": url, "direct_megaphone": direct_megaphone,
                    "resolved_url": response.url, "downloaded_at": now(),
                    "http_content_length": expected, "etag": response.headers.get("ETag"),
                    "validation": "HTTP length if provided + file signature + SHA256; decoding not yet checked",
                    "transcript_status": "not_acquired", "alignment_status": "not_started"}
    write_json(manifest_path, manifest)
    write_json(dest / "episode.json", episode)
    (dest / "show_notes.txt").write_text(episode["show_notes_text"], encoding="utf-8")
    return {"status": "archived", "path": str(final), **manifest}


def import_audio(slug, episode_id, source, receipt=None):
    """Archive a user file, or a previously staged download with an HTTP receipt."""
    target = folder(slug)
    catalog = read_json(target / "catalog.json")
    if not catalog:
        raise ValueError("Run sync first.")
    if episode_id == "latest":
        episode_id = catalog["current_feed_ids"][0]
    episode = next((e for e in catalog["episodes"] if e["episode_id"] == episode_id), None)
    if episode is None:
        raise ValueError("Episode not found.")
    source = Path(source).resolve()
    size = source.stat().st_size
    with source.open("rb") as stream:
        first = stream.read(16)
    mp3 = first.startswith(b"ID3") or (len(first)>1 and first[0]==255 and first[1]&224==224)
    mp4 = first[4:8] == b"ftyp"
    if not size or not (mp3 or mp4):
        raise ValueError("Unsupported media signature.")
    evidence = None
    if receipt:
        evidence = json.loads(Path(receipt).read_text(encoding="utf-8-sig"))
        if evidence.get("Status") not in (200, 206) or evidence.get("Length") != size:
            raise ValueError("Download receipt does not match the file size.")
        headers = {k.lower(): v[0] if isinstance(v, list) else v for k,v in evidence["Headers"].items()}
        if int(headers.get("content-length", "-1")) != size:
            raise ValueError("Receipt Content-Length mismatch.")
        if evidence["Status"] == 206 and headers.get("content-range") != f"bytes 0-{size-1}/{size}":
            raise ValueError("Receipt represents only a partial download.")
    digest = hash_file(source)
    dest = target / "episodes" / episode_id
    dest.mkdir(parents=True, exist_ok=True)
    if (dest / "asset.json").exists():
        raise ValueError("An audio version already exists; do not overwrite.")
    filename = "audio_" + digest[:16] + (".mp3" if mp3 else ".m4a")
    partial = dest / "audio.part"
    shutil.copyfile(source, partial)
    if hash_file(partial) != digest:
        raise ValueError("Copy hash mismatch.")
    partial.replace(dest / filename)
    manifest = {"episode_id": episode_id, "title": episode["title"], "filename": filename,
                "sha256": digest, "bytes": size, "mime": "audio/mpeg" if mp3 else "audio/mp4",
                "import_method": "staged_download_with_receipt" if evidence else "user_file",
                "source_url": (episode.get("audio_enclosure") or {}).get("url") if evidence else None,
                "download_receipt": evidence, "archived_at": now(),
                "validation": "Signature + SHA256 + HTTP receipt length/range when provided; decoding unchecked",
                "transcript_status": "not_acquired", "alignment_status": "not_started"}
    write_json(dest / "asset.json", manifest)
    write_json(dest / "episode.json", episode)
    (dest / "show_notes.txt").write_text(episode["show_notes_text"], encoding="utf-8")
    return {"status": "archived", "path": str(dest / filename), "bytes": size, "sha256": digest}


def verify(slug):
    results = []
    for manifest_path in (folder(slug) / "episodes").glob("*/asset.json"):
        m = read_json(manifest_path)
        path = manifest_path.parent / m["filename"]
        passed = path.is_file() and path.stat().st_size == m["bytes"] and hash_file(path) == m["sha256"]
        results.append({"episode_id": m["episode_id"], "ok": passed})
    return {"verified_assets": len(results), "all_ok": bool(results) and all(r["ok"] for r in results), "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sync")
    p.add_argument("show")
    p.add_argument("feed_url")
    p = sub.add_parser("download")
    p.add_argument("show")
    p.add_argument("episode", help="episode_id, or latest")
    p.add_argument("--max-mb", type=int, default=350)
    p.add_argument("--direct-megaphone", action="store_true", help="Use the literal Megaphone destination embedded in the enclosure if its analytics redirects fail.")
    p = sub.add_parser("verify")
    p.add_argument("show")
    p = sub.add_parser("import-audio")
    p.add_argument("show")
    p.add_argument("episode")
    p.add_argument("file")
    p.add_argument("--receipt")
    args = parser.parse_args()
    if args.command == "sync":
        result = sync(args.show, args.feed_url)
    elif args.command == "download":
        result = download(args.show, args.episode, args.max_mb, args.direct_megaphone)
    elif args.command == "import-audio":
        result = import_audio(args.show, args.episode, args.file, args.receipt)
    else:
        result = verify(args.show)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if args.command == "verify" and not result["all_ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ET.ParseError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(1)
