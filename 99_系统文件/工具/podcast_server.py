"""Loopback-only local UI for the existing podcast archive. Python standard library."""
import argparse
import contextlib
import datetime
import email.utils
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import podcast_archive as archive
import podcast_translate as translation
import podcast_vocabulary as vocabulary

ROOT = archive.ROOT
STATIC = ROOT / '99_系统文件/工具/播客界面'
DB = ROOT / '99_系统文件/索引/播客学习/ui.sqlite3'
TOKEN = secrets.token_urlsafe(32)
JOBS = []
JOB_LOCK = threading.Lock()
CATALOG_LOCK = threading.RLock()
ASR_PYTHON = ROOT / '99_系统文件/依赖/podcast-asr/Scripts/python.exe'
ASR_MODEL = ROOT / '99_系统文件/依赖/播客模型/whisper-large-v3-turbo'
ASR_PROCESSES = {}


@contextlib.contextmanager
def database():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    with database() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS state(asset TEXT PRIMARY KEY, episode TEXT NOT NULL,
          position REAL NOT NULL DEFAULT 0, speed REAL NOT NULL DEFAULT 1,
          notes TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bookmarks(id TEXT PRIMARY KEY, asset TEXT NOT NULL,
          episode TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
          note TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS bookmark_asset ON bookmarks(asset);
        CREATE TABLE IF NOT EXISTS transcripts(id TEXT PRIMARY KEY, asset TEXT NOT NULL,
          episode TEXT NOT NULL, name TEXT NOT NULL, raw TEXT NOT NULL,
          segments TEXT NOT NULL, aligned INTEGER NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS transcript_asset ON transcripts(asset,created);
        ''')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(transcripts)')}
        if 'source' not in columns:
            db.execute("ALTER TABLE transcripts ADD COLUMN source TEXT NOT NULL DEFAULT 'user_import'")
        if 'metadata' not in columns:
            db.execute("ALTER TABLE transcripts ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        db.executescript('''
        CREATE TABLE IF NOT EXISTS translations(transcript_id TEXT PRIMARY KEY,
          source_hash TEXT NOT NULL,status TEXT NOT NULL,translated TEXT NOT NULL DEFAULT '{}',
          metadata TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS translation_cache(cache_key TEXT PRIMARY KEY,
          translated TEXT NOT NULL,model TEXT NOT NULL,created REAL NOT NULL);
        ''')
        vocabulary.init(db)


def identify(key):
    if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9_-]+:[a-f0-9]{20}', key):
        raise ValueError('单集编号无效')
    show, eid = key.split(':')
    with CATALOG_LOCK:
        catalog = archive.read_json(archive.folder(show) / 'catalog.json')
    if not catalog:
        raise ValueError('找不到节目')
    ep = next((e for e in catalog['episodes'] if e['episode_id'] == eid), None)
    if not ep:
        raise ValueError('找不到单集')
    return show, ep


def asset_for(key):
    show, ep = identify(key)
    return media_for(show, ep, key)


def media_for(show, ep, key):
    base = archive.folder(show) / 'episodes' / ep['episode_id']
    manifest = archive.read_json(base / 'asset.json')
    if not manifest:
        return None, None, None
    media = (base / manifest['filename']).resolve()
    if not media.is_relative_to(base.resolve()) or not media.is_file() or media.stat().st_size != manifest['bytes']:
        return None, None, None
    return key + ':' + manifest['sha256'], media, manifest


def duration(value):
    try:
        result = 0
        for part in str(value).split(':'):
            result = result * 60 + float(part)
        return result
    except ValueError:
        return 0


def library():
    with database() as db:
        states = {r['asset']: dict(r) for r in db.execute('SELECT * FROM state')}
        bookmarks = [dict(r) for r in db.execute('SELECT * FROM bookmarks ORDER BY created DESC')]
        transcripts = {r['asset'] for r in db.execute('SELECT DISTINCT asset FROM transcripts')}
    registry = archive.read_json(ROOT/'99_系统文件/索引/播客学习/shows.json', [])
    registered = {s['id']:s for s in registry}
    episodes, shows = [], []
    with CATALOG_LOCK:
        for path in sorted(archive.LIBRARY.glob('*/catalog.json')):
            catalog = archive.read_json(path)
            sid = catalog['show_id']
            shows.append({'id': sid, 'title': catalog['title'], 'count': len(catalog['episodes']),
                          'name':registered.get(sid,{}).get('name',catalog['title']),
                          'category':registered.get(sid,{}).get('category','播客'),
                          'cover':'/covers/'+sid+'.jpg' if (archive.folder(sid)/'cover.jpg').is_file() else None})
            for e in catalog['episodes']:
                key = sid + ':' + e['episode_id']
                asset, media, manifest = media_for(sid,e,key)
                try:
                    date = email.utils.parsedate_to_datetime(e['published_at']).date().isoformat()
                except (ValueError, TypeError):
                    date = ''
                st = states.get(asset, {})
                episodes.append({'key': key, 'show': sid, 'show_title': catalog['title'],
                    'title': e['title'], 'date': date, 'duration': duration(e['declared_duration']),
                    'downloaded': bool(media), 'bytes': manifest['bytes'] if manifest else 0,
                    'position': st.get('position', 0), 'updated': st.get('updated', 0),
                    'has_transcript': asset in transcripts, 'asset': asset,
                    'search_notes': e.get('show_notes_text', '')[:800]})
    episodes.sort(key=lambda e: e['date'], reverse=True)
    order = list(registered)
    shows.sort(key=lambda s:order.index(s['id']) if s['id'] in order else len(order))
    return {'shows': shows, 'episodes': episodes, 'bookmarks': bookmarks, 'token': TOKEN}


def detail(key):
    show, ep = identify(key)
    asset, media, manifest = asset_for(key)
    with database() as db:
        state = db.execute('SELECT * FROM state WHERE asset=?', (asset,)).fetchone()
        marks = [dict(r) for r in db.execute('SELECT * FROM bookmarks WHERE asset=? ORDER BY start', (asset,))]
        transcript = db.execute('SELECT id,name,segments,aligned,created,source,metadata FROM transcripts WHERE asset=? ORDER BY created DESC LIMIT 1', (asset,)).fetchone()
    trans = dict(transcript) if transcript else None
    if trans:
        trans['segments'] = json.loads(trans['segments'])
        trans['metadata'] = json.loads(trans['metadata'])
        trans['translation'] = translation_state(trans)
    return {'key': key, 'title': ep['title'], 'show': show,
            'description': ep.get('show_notes_text', ''), 'asset': asset,
            'source_url': (manifest or {}).get('source_url') or (ep.get('audio_enclosure') or {}).get('url'),
            'state': dict(state) if state else {'position': 0, 'speed': 1, 'notes': ''},
            'bookmarks': marks, 'transcript': trans, 'downloaded': bool(media)}


def translation_state(trans):
    with database() as db:
        row = db.execute('SELECT * FROM translations WHERE transcript_id=?',(trans['id'],)).fetchone()
    result = dict(row) if row else {'status':'not_started','translated':'{}','metadata':'{}','error':''}
    values = json.loads(result.pop('translated'))
    result['metadata'] = json.loads(result['metadata'])
    result['completed'] = len(values)
    result['total'] = len(trans['segments'])
    for i,segment in enumerate(trans['segments']):
        if str(i) in values:
            segment['zh'] = values[str(i)]
    result['estimate'] = translation.estimated_cost(trans['segments'],translation.public_settings()['model'])
    return result


def number(value, low, high):
    v = float(value)
    if not math.isfinite(v) or not low <= v <= high:
        raise ValueError('时间或数值超出范围')
    return v


def parse_transcript(raw):
    if not isinstance(raw, str) or len(raw) > 2_000_000 or not raw.strip():
        raise ValueError('请选择非空的 TXT、SRT 或 VTT 文件（不超过 2 MB）')
    raw = raw.lstrip('\ufeff').replace('\r\n', '\n')
    pattern = re.compile(r'(?m)^(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})[^\n]*\n')
    matches = list(pattern.finditer(raw))
    if not matches:
        if '-->' in raw:
            raise ValueError('字幕时间戳无法识别，请使用标准 SRT 或 VTT')
        return [{'text': line.strip(), 'start': None, 'end': None} for line in raw.split('\n') if line.strip()]
    result = []
    previous = -1
    for i, match in enumerate(matches):
        g = match.groups()
        start = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
        end = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
        if start < previous or end <= start or end > 360000:
            raise ValueError('字幕时间戳倒序或超出范围')
        previous = start
        body = raw[match.end():matches[i+1].start() if i+1<len(matches) else len(raw)]
        body = re.split(r'\n\s*\n', body, maxsplit=1)[0]
        body = re.sub(r'<[^>]*>', '', body).strip()
        if body:
            result.append({'start': start, 'end': end, 'text': body})
    return result


def mutate(route, data):
    key = data.get('key')
    asset, media, manifest = asset_for(key)
    if not asset:
        raise ValueError('请先下载本集音频')
    if data.get('asset') != asset:
        raise ValueError('音频版本已变化，请重新打开本集')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if route == '/api/state':
            row = db.execute('SELECT * FROM state WHERE asset=?', (asset,)).fetchone()
            previous = dict(row) if row else {'position':0,'speed':1,'notes':''}
            position = number(data.get('position',previous['position']),0,360000)
            speed = number(data.get('speed',previous['speed']),0.5,3)
            notes = data.get('notes',previous['notes'])
            if not isinstance(notes,str) or len(notes)>100000:
                raise ValueError('笔记过长')
            db.execute('INSERT INTO state VALUES(?,?,?,?,?,?) ON CONFLICT(asset) DO UPDATE SET position=excluded.position,speed=excluded.speed,notes=excluded.notes,updated=excluded.updated',
                       (asset,key,position,speed,notes,time.time()))
        elif route == '/api/bookmark':
            start = number(data.get('start'),0,360000)
            end = number(data.get('end'),start+0.01,360000)
            note = str(data.get('note',''))[:5000]
            db.execute('INSERT INTO bookmarks VALUES(?,?,?,?,?,?,?)', (uuid.uuid4().hex,asset,key,start,end,note,time.time()))
        elif route == '/api/bookmark-delete':
            db.execute('DELETE FROM bookmarks WHERE id=? AND asset=?', (data.get('id'),asset))
        elif route == '/api/transcript':
            raw = data.get('text')
            segments = parse_transcript(raw)
            aligned = bool(data.get('aligned')) and any(s['start'] is not None for s in segments)
            db.execute('INSERT INTO transcripts(id,asset,episode,name,raw,segments,aligned,created) VALUES(?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,asset,key,str(data.get('name','导入的文字稿'))[:200],raw,json.dumps(segments,ensure_ascii=False),int(aligned),time.time()))
        else:
            raise ValueError('操作不存在')
    return {'ok':True}


def enqueue(data):
    sid = data.get('show')
    catalog = archive.read_json(archive.folder(sid) / 'catalog.json')
    if not catalog or data.get('kind') not in ('sync','download','transcribe','translate'):
        raise ValueError('任务无效')
    kind = data['kind']
    eid = data.get('episode')
    if kind in ('download','transcribe','translate'):
        identify(sid+':'+str(eid))
    asset = None
    if kind in ('transcribe','translate'):
        asset, media, _ = asset_for(sid+':'+eid)
        if not media:
            raise ValueError('请先下载本集音频')
        if data.get('asset') != asset:
            raise ValueError('音频版本已变化，请重新打开本集')
        if kind=='transcribe' and (not ASR_PYTHON.is_file() or not (ASR_MODEL/'download-receipt.json').is_file()):
            raise ValueError('本地转写环境还未安装完成')
        if kind=='translate' and not detail(sid+':'+eid)['transcript']:
            raise ValueError('请先生成英文稿')
    with JOB_LOCK:
        if any(j['status'] in ('queued','running') and j['show']==sid and j['kind']==kind and j.get('episode')==eid for j in JOBS):
            return {'ok':True,'duplicate':True}
        if sum(j['status'] in ('queued','running') for j in JOBS)>=10:
            raise ValueError('请等当前任务完成后再添加')
        job={'id':uuid.uuid4().hex,'show':sid,'kind':kind,'episode':eid,'status':'queued','error':'','asset':asset,'progress':0}
        JOBS.append(job)
    return {'ok':True,'id':job['id']}


def stop_asr_process(process):
    # Windows venv launchers can spawn a child Python; release its GPU too.
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        process.terminate()


def cancel_job(data):
    with JOB_LOCK:
        job = next((j for j in JOBS if j['id'] == data.get('id') and j['kind'] in ('transcribe','translate')), None)
        if not job:
            raise ValueError('找不到转写任务')
        if job['status'] in ('queued', 'running'):
            job['cancel_requested'] = True
            job['status'] = 'cancelled'
            process = ASR_PROCESSES.get(job['id'])
            if process and process.poll() is None:
                stop_asr_process(process)
    return {'ok': True}


def run_transcription(job):
    key = job['show'] + ':' + job['episode']
    asset, media, manifest = asset_for(key)
    if asset != job['asset'] or not media:
        raise ValueError('音频版本已变化，请重新添加转写任务')
    output = media.parent / 'transcripts' / (job['id'] + '.json')
    logs = ROOT / '99_系统文件/缓存/临时文件/podcast_asr'
    logs.mkdir(parents=True, exist_ok=True)
    error_log = logs / (job['id'] + '.log')
    command = [str(ASR_PYTHON), '-B', str(ROOT/'99_系统文件/工具/podcast_transcribe.py'),
               '--audio', str(media), '--output', str(output), '--sha256', manifest['sha256']]
    with error_log.open('w', encoding='utf-8') as errors:
        with JOB_LOCK:
            if job.get('cancel_requested'):
                return
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            ASR_PROCESSES[job['id']] = process
        try:
            for line in process.stdout:
                try:
                    update = json.loads(line)
                except ValueError:
                    continue
                with JOB_LOCK:
                    job.update({k:update[k] for k in ('stage','progress','error') if k in update})
            code = process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:
                stop_asr_process(process)
                process.wait()
            with JOB_LOCK:
                ASR_PROCESSES.pop(job['id'], None)
    if job.get('cancel_requested'):
        return
    if code != 0:
        raise ValueError(job.get('error') or '本地转写失败，可能是显存不足；关闭占用显卡的软件后重试。日志：'+str(error_log.relative_to(ROOT)))
    result = archive.read_json(output)
    if result['metadata']['audio_sha256'] != manifest['sha256'] or result['metadata']['sample_only']:
        raise ValueError('转写结果与完整音频不匹配')
    if asset_for(key)[0] != asset:
        raise ValueError('转写期间音频版本发生变化，未应用结果')
    with JOB_LOCK:
        if job.get('cancel_requested'):
            return
        with database() as db:
            db.execute('INSERT INTO transcripts(id,asset,episode,name,raw,segments,aligned,created,source,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (job['id'],asset,key,'Whisper Turbo 英文稿',output.with_suffix('.srt').read_text(encoding='utf-8'),
                 json.dumps(result['segments'],ensure_ascii=False),1,time.time(),'local_asr',json.dumps(result['metadata'])))
        job['progress'] = 100
        job['elapsed_seconds'] = result['metadata']['elapsed_seconds']
        job['transcript_id'] = job['id']
        job['english_ready'] = True


def run_translation(job):
    key=job['show']+':'+job['episode']
    asset,media,_=asset_for(key)
    if not media or asset!=job['asset']:
        raise ValueError('音频版本已变化，请重新打开本集')
    item=detail(key);trans=item['transcript']
    if not trans:
        raise ValueError('本集没有英文稿')
    transcript_id=job.get('transcript_id') or trans['id']
    if transcript_id!=trans['id']:
        raise ValueError('英文稿版本已变化，请翻译最新版本')
    segments=trans['segments'];fingerprint=translation.source_hash(segments)
    with database() as db:
        previous=db.execute('SELECT * FROM translations WHERE transcript_id=?',(transcript_id,)).fetchone()
    completed=json.loads(previous['translated']) if previous and previous['source_hash']==fingerprint else {}
    metadata=json.loads(previous['metadata']) if previous and previous['source_hash']==fingerprint else {'prompt_tokens':0,'completion_tokens':0,'models':{},'cache_batches':0}
    def persist(status,error=''):
        with database() as db:
            db.execute('INSERT INTO translations VALUES(?,?,?,?,?,?,?) ON CONFLICT(transcript_id) DO UPDATE SET source_hash=excluded.source_hash,status=excluded.status,translated=excluded.translated,metadata=excluded.metadata,error=excluded.error,updated=excluded.updated',
                (transcript_id,fingerprint,status,json.dumps(completed,ensure_ascii=False),json.dumps(metadata),error,time.time()))
    if len(completed)!=len(segments) and not translation.public_settings()['configured']:
        persist('waiting_key')
        job.update(status='waiting_key',stage='waiting_key',error='英文稿已保存，请在翻译设置中填写密钥后补齐中文')
        return
    model,api_key=translation.credential() if len(completed)!=len(segments) else (metadata.get('preferred_model',translation.MODELS[0]),'')
    metadata['preferred_model']=model
    metadata['prompt_version']=translation.VERSION
    pending=[b for b in translation.batches(segments) if not all(str(x['id']) in completed for x in b)]
    job.update(stage='translating',progress=round(len(completed)/len(segments)*100,1))
    persist('running')
    usage_lock=threading.Lock()
    def one_batch(batch):
        start,end=batch[0]['id'],batch[-1]['id']
        context={'show':item['show'],'episode':item['title'],
                 'before':[s['text'] for s in segments[max(0,start-3):start]],
                 'after':[s['text'] for s in segments[end+1:end+4]]}
        cache_key=hashlib.sha256(json.dumps([translation.VERSION,model,context,batch],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with database() as db:
            cached=db.execute('SELECT * FROM translation_cache WHERE cache_key=?',(cache_key,)).fetchone()
        if cached:
            return json.loads(cached['translated']),{},cached['model'],True
        usage_total={'prompt_tokens':0,'completion_tokens':0}
        candidates=[model,model]+(['deepseek-v4-pro'] if model=='deepseek-v4-flash' else [])
        for index,candidate in enumerate(candidates):
            if job.get('cancel_requested'):
                return {},usage_total,candidate,False
            content,usage=translation.request_translation(batch,context,candidate,api_key)
            with usage_lock:
                for field in usage_total:
                    metadata[field]=metadata.get(field,0)+int(usage.get(field,0))
                calls=metadata.setdefault('requests_by_model',{})
                calls[candidate]=calls.get(candidate,0)+1
            for field in usage_total:
                usage_total[field]+=int(usage.get(field,0))
            try:
                values=translation.validate_response(content,batch)
                # A name, title or quoted fragment may legitimately stay English. Only
                # reject a whole prose batch that was essentially echoed untranslated.
                prose=[x for x in batch if len(x['text'].split())>5]
                if len(prose)>=3 and sum(values[str(x['id'])].strip()==x['text'].strip() for x in prose)/len(prose)>.8:
                    raise ValueError('检测到整批原文未翻译')
            except (ValueError,TypeError,KeyError):
                if index+1<len(candidates):
                    continue
                if len(batch)>1:
                    # Smaller groups avoid model omissions without accepting shifted ids.
                    left=one_batch(batch[:len(batch)//2])
                    right=one_batch(batch[len(batch)//2:])
                    values={**left[0],**right[0]}
                    if len(values)!=len(batch):
                        return values,usage_total,candidate,False
                    break
                raise ValueError('翻译结果未通过逐句检查，已保留之前完成的中文') from None
            break
        with database() as db:
            db.execute('INSERT OR REPLACE INTO translation_cache VALUES(?,?,?,?)',
                (cache_key,json.dumps(values,ensure_ascii=False),candidate,time.time()))
        return values,usage_total,candidate,False
    failure=None
    batches_iter=iter(pending)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(one_batch,b) for b in [next(batches_iter,None) for _ in range(3)] if b}
        while futures:
            done,futures=wait(futures,return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    values,usage,used_model,cached=future.result()
                    completed.update(values)
                    if cached:
                        metadata['cache_batches']=metadata.get('cache_batches',0)+1
                    else:
                        metadata['models'][used_model]=metadata['models'].get(used_model,0)+1
                    job['progress']=round(len(completed)/len(segments)*100,1)
                    persist('running')
                except Exception as exc:
                    failure=str(exc)[:300]
                if not failure and not job.get('cancel_requested'):
                    batch=next(batches_iter,None)
                    if batch:
                        futures.add(pool.submit(one_batch,batch))
    if job.get('cancel_requested'):
        persist('cancelled')
        return
    if failure:
        persist('failed',failure)
        raise ValueError(failure)
    if len(completed)!=len(segments):
        persist('failed','中文尚未补齐')
        raise ValueError('中文尚未补齐，请重试')
    bilingual=[{**s,'zh':completed[str(i)]} for i,s in enumerate(segments)]
    output=media.parent/'transcripts'/('translation-'+transcript_id+'.json')
    archive.write_json(output,{'transcript_id':transcript_id,'audio_asset':asset,'source_hash':fingerprint,
                                'provider':'DeepSeek','metadata':metadata,'segments':bilingual})
    from podcast_transcribe import timestamp
    output.with_suffix('.txt').write_text('\n\n'.join(s['text']+'\n'+s['zh'] for s in bilingual),encoding='utf-8')
    output.with_suffix('.srt').write_text('\n\n'.join(f'{i}\n{timestamp(s["start"])} --> {timestamp(s["end"])}\n{s["text"]}\n{s["zh"]}'
        for i,s in enumerate(bilingual,1) if s['start'] is not None),encoding='utf-8')
    persist('succeeded')
    job.update(stage='translated',progress=100)


def worker():
    while True:
        with JOB_LOCK:
            job=next((j for j in JOBS if j['status']=='queued'),None)
            if job:
                job['status']='running'
        if not job:
            time.sleep(0.5)
            continue
        try:
            if job['kind']=='sync':
                cat=archive.read_json(archive.folder(job['show'])/'catalog.json')
                archive.sync(job['show'],cat['feed_url'])
            elif job['kind']=='transcribe':
                run_transcription(job)
                if not job.get('cancel_requested'):
                    run_translation(job)
            elif job['kind']=='translate':
                run_translation(job)
            else:
                _,ep=identify(job['show']+':'+job['episode'])
                url=(ep.get('audio_enclosure') or {}).get('url','')
                archive.download(job['show'],job['episode'],350,'traffic.megaphone.fm/' in url)
            if not job.get('cancel_requested') and job['status']!='waiting_key':
                job['status']='succeeded'
        except Exception as exc:
            if not job.get('cancel_requested'):
                job['status']='failed'
                job['error']=str(exc)[-700:]


def vocabulary_worker():
    # Saved captures are the durable queue; a server restart retries unfinished work.
    with database() as db:
        db.execute("UPDATE vocabulary SET status='pending' WHERE status='running'")
    while True:
        try:
            with database() as db:
                if translation.public_settings()['configured']:
                    db.execute("UPDATE vocabulary SET status='pending' WHERE status='waiting_key' AND deleted=0")
                row=db.execute("SELECT * FROM vocabulary WHERE status='pending' AND deleted=0 ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE vocabulary SET status='running' WHERE id=?",(row['id'],))
            if not row:
                time.sleep(.8)
                continue
            if not translation.public_settings()['configured']:
                with database() as db:db.execute("UPDATE vocabulary SET status='waiting_key',error='请在翻译设置中填写密钥，原词已保存' WHERE id=?",(row['id'],))
                continue
            try:
                result,metadata=vocabulary.enrich(dict(row),translation)
                with database() as db:
                    db.execute("UPDATE vocabulary SET english=?,chinese=?,status='ready',error='',metadata=?,updated=? WHERE id=?",
                               (result['english'],result['chinese'],json.dumps(metadata),time.time(),row['id']))
            except Exception as exc:
                with database() as db:db.execute("UPDATE vocabulary SET status='failed',error=?,updated=? WHERE id=?",(str(exc)[:300],time.time(),row['id']))
        except Exception:
            time.sleep(2)


def byte_range(header, size):
    if not header:
        return 0,size-1,200
    m=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
    if not m or not any(m.groups()):
        raise ValueError('Invalid range')
    if not m[1]:
        length=int(m[2])
        if length<=0: raise ValueError('Invalid suffix')
        start,end=max(0,size-length),size-1
    else:
        start,end=int(m[1]),min(int(m[2]) if m[2] else size-1,size-1)
    if start>=size or start>end:
        raise ValueError('Unsatisfiable range')
    return start,end,206


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass

    def trusted(self):
        host=self.headers.get('Host','')
        allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        origin=self.headers.get('Origin')
        return host in allowed and (not origin or origin in {'http://'+a for a in allowed})

    def output(self,body,status=200,mime='application/json; charset=utf-8',extra=None):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; media-src 'self' blob:; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(body)

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        if not self.trusted():
            self.output({'error':'只接受本机页面请求'},403); return
        p=urlsplit(self.path); q=parse_qs(p.query)
        try:
            if p.path in ('/','/app.js','/vocabulary.js','/style.css'):
                name={'/':'index.html','/app.js':'app.js','/vocabulary.js':'vocabulary.js','/style.css':'style.css'}[p.path]
                mime='text/javascript; charset=utf-8' if p.path.endswith('.js') else 'text/css; charset=utf-8' if p.path.endswith('.css') else 'text/html; charset=utf-8'
                self.output((STATIC/name).read_bytes(),mime=mime)
            elif p.path=='/api/health': self.output({'app':'podcast-local','version':3})
            elif re.fullmatch(r'/covers/[a-z0-9_-]+\.jpg',p.path):
                cover=archive.folder(p.path.split('/')[-1][:-4])/'cover.jpg'
                if not cover.is_file():
                    self.output({'error':'没有封面'},404)
                else:
                    self.output(cover.read_bytes(),mime='image/jpeg')
            elif p.path=='/api/translation-settings': self.output(translation.public_settings())
            elif p.path=='/api/library': self.output(library())
            elif p.path=='/api/episode': self.output(detail(q.get('key',[''])[0]))
            elif p.path=='/api/vocabulary':
                with database() as db:
                    items=[dict(r) for r in db.execute('SELECT * FROM vocabulary ORDER BY created DESC')] if q.get('include_deleted')==['1'] else vocabulary.rows(db)
                    self.output({'items':items})
            elif p.path=='/api/vocabulary-export':
                with database() as db:items=[dict(r) for r in db.execute('SELECT * FROM vocabulary ORDER BY created')]
                self.output({'format':'podcast-vocabulary-v1','exported_at':archive.now(),'items':items},extra={'Content-Disposition':'attachment; filename="vocabulary.json"'})
            elif p.path=='/api/transcript-export':
                item = detail(q.get('key',[''])[0])
                tr = item['transcript']
                if not tr:
                    raise ValueError('本集还没有英文稿')
                fmt = q.get('format',['txt'])[0]
                language=q.get('language',['en'])[0]
                if language not in ('en','zh','both'):
                    raise ValueError('字幕语言无效')
                if language!='en' and any(not s.get('zh') for s in tr['segments']):
                    raise ValueError('中文尚未全部生成，请完成翻译后再导出')
                def export_text(segment):
                    return segment['text'] if language=='en' else segment['zh'] if language=='zh' else segment['text']+'\n'+segment['zh']
                if fmt == 'txt':
                    body = '\n\n'.join(export_text(s) for s in tr['segments']) + '\n'
                elif fmt == 'srt':
                    if not tr['aligned']:
                        raise ValueError('这份文字稿没有确认过的时间轴')
                    from podcast_transcribe import timestamp
                    body = '\n\n'.join(f'{i}\n{timestamp(s["start"])} --> {timestamp(s["end"])}\n{export_text(s)}'
                        for i,s in enumerate(tr['segments'],1) if s['start'] is not None)
                else:
                    raise ValueError('不支持的导出格式')
                self.output(body.encode('utf-8'),mime='text/plain; charset=utf-8',extra={'Content-Disposition':f'attachment; filename="transcript.{fmt}"'})
            elif p.path=='/api/jobs':
                with JOB_LOCK: self.output({'jobs':JOBS[-20:]})
            elif p.path=='/api/export':
                with database() as db:
                    data={table:[dict(r) for r in db.execute('SELECT * FROM '+table)] for table in ('state','bookmarks','transcripts','translations','vocabulary')}
                data.update({'format':'podcast-study-export-v1','exported_at':archive.now()})
                self.output(data,extra={'Content-Disposition':'attachment; filename="podcast-study.json"'})
            elif p.path=='/media':
                _,path,manifest=asset_for(q.get('key',[''])[0])
                if not path: raise ValueError('尚未下载')
                size=path.stat().st_size
                try: start,end,status=byte_range(self.headers.get('Range'),size)
                except ValueError:
                    self.output({'error':'范围无效'},416,extra={'Content-Range':f'bytes */{size}'}); return
                self.send_response(status)
                self.send_header('Content-Type',manifest['mime'])
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Content-Length',str(end-start+1))
                if status==206: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                if self.command=='HEAD': return
                with path.open('rb') as f:
                    f.seek(start); remaining=end-start+1
                    while remaining:
                        chunk=f.read(min(262144,remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining-=len(chunk)
            else: self.output({'error':'不存在'},404)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): pass
        except (ValueError,KeyError) as exc: self.output({'error':str(exc)},400)
        except Exception: self.output({'error':'读取失败，请稍后重试'},500)

    def do_POST(self):
        if not self.trusted() or self.headers.get('X-Local-Token')!=TOKEN:
            self.output({'error':'页面连接已失效，请刷新'},403); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>3_000_000: raise ValueError('提交内容过大或为空')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError('提交格式错误')
            if self.path=='/api/vocabulary':
                item=detail(data.get('key'))
                with database() as db:result=vocabulary.capture(db,data,item)
            elif self.path in ('/api/vocabulary-delete','/api/vocabulary-restore','/api/vocabulary-retry'):
                with database() as db:
                    if not db.execute('SELECT id FROM vocabulary WHERE id=?',(data.get('id'),)).fetchone():
                        raise ValueError('找不到这条生词记录')
                    if self.path=='/api/vocabulary-retry':
                        db.execute("UPDATE vocabulary SET status='pending',error='',updated=? WHERE id=? AND status IN ('failed','waiting_key') AND deleted=0",(time.time(),data['id']))
                    else:
                        db.execute('UPDATE vocabulary SET deleted=?,updated=? WHERE id=?',(int(self.path.endswith('-delete')),time.time(),data['id']))
                result={'ok':True}
            elif self.path=='/api/translation-settings':
                result=translation.save_settings(data)
                if result['configured'] and data.get('resume_pending'):
                    queued=0
                    for ep in library()['episodes']:
                        if ep['downloaded'] and ep['has_transcript']:
                            d=detail(ep['key'])
                            if d['transcript']['translation']['status']!='succeeded':
                                enqueue({'kind':'translate','show':ep['show'],'episode':ep['key'].split(':')[1],'asset':ep['asset']})
                                queued+=1
                                if queued>=10:
                                    break
                    result['queued']=queued
            else:
                result=enqueue(data) if self.path=='/api/job' else cancel_job(data) if self.path=='/api/job-cancel' else mutate(self.path,data)
            self.output(result)
        except (ValueError,KeyError,TypeError) as exc: self.output({'error':str(exc)},400)
        except Exception: self.output({'error':'保存失败，内容未确认保存，请重试'},500)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args(); init_db()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    threading.Thread(target=worker,daemon=True).start()
    threading.Thread(target=vocabulary_worker,daemon=True).start()
    print(f'Podcast library: http://127.0.0.1:{args.port}',flush=True)
    server.serve_forever()
