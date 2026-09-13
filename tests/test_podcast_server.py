import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import json
import io
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock
from http.server import ThreadingHTTPServer
import podcast_server as ui


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.tmp.name)
        cls.db_patch=patch.object(ui,'DB',cls.root/'study.sqlite3'); cls.db_patch.start()
        cls.lib_patch=patch.object(ui.archive,'LIBRARY',cls.root/'library');cls.lib_patch.start()
        cls.key='demo:'+'a'*20
        cls.bytes=b'ID3'+bytes(range(256))*20
        ep={'episode_id':'a'*20,'title':'Test episode','published_at':'Sun, 06 Sep 2026 00:00:00 GMT','declared_duration':'100','show_notes_text':'notes','audio_enclosure':{'url':'https://example.com/a.mp3'}}
        base=ui.archive.LIBRARY/'demo'; dest=base/'episodes'/ep['episode_id'];dest.mkdir(parents=True)
        ui.archive.write_json(base/'catalog.json',{'show_id':'demo','title':'Demo','feed_url':'https://example.com/feed','episodes':[ep]})
        (dest/'audio.mp3').write_bytes(cls.bytes)
        ui.archive.write_json(dest/'asset.json',{'filename':'audio.mp3','bytes':len(cls.bytes),'sha256':'b'*64,'mime':'audio/mpeg'})
        ui.init_db()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),ui.Handler)
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.lib_patch.stop();cls.db_patch.stop();cls.tmp.cleanup()

    def call(self,path,data=None,headers=None):
        h={'X-Local-Token':ui.TOKEN,'Content-Type':'application/json',**(headers or {})}
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=h)
        return urllib.request.urlopen(req,timeout=5)

    def payload(self):return {'key':self.key,'asset':self.key+':'+'b'*64}

    def test_library_and_static(self):
        with self.call('/api/library') as r:
            data=json.load(r);self.assertEqual(len(data['episodes']),1);self.assertTrue(data['episodes'][0]['downloaded'])
        with self.call('/') as r:
            self.assertEqual(r.status,200);self.assertIn('text/html',r.headers['Content-Type'])

    def test_persistent_state_partial_updates(self):
        with self.call('/api/state',{**self.payload(),'notes':'我的英文笔记'}):pass
        with self.call('/api/state',{**self.payload(),'position':61.5,'speed':1.25}):pass
        with self.call('/api/episode?key='+self.key) as r:
            state=json.load(r)['state'];self.assertEqual(state['notes'],'我的英文笔记');self.assertEqual(state['position'],61.5)
        ui.init_db()  # A restart must not recreate or erase state.
        with self.call('/api/episode?key='+self.key) as r:self.assertEqual(json.load(r)['state']['notes'],'我的英文笔记')

    def test_bookmark_roundtrip_and_delete(self):
        with self.call('/api/bookmark',{**self.payload(),'start':10,'end':20,'note':'Test mark'}):pass
        with self.call('/api/episode?key='+self.key) as r:marks=json.load(r)['bookmarks']
        self.assertEqual(marks[0]['note'],'Test mark')
        with self.call('/api/bookmark-delete',{**self.payload(),'id':marks[0]['id']}):pass
        with self.call('/api/episode?key='+self.key) as r:self.assertEqual(json.load(r)['bookmarks'],[])

    def test_transcript_import_and_revisions(self):
        srt='1\n00:00:01,000 --> 00:00:03,000\nHello camera.\n\n2\n00:00:04,000 --> 00:00:05,000\nAnother sentence.\n'
        with self.call('/api/transcript',{**self.payload(),'name':'test.srt','text':srt,'aligned':True}):pass
        with self.call('/api/episode?key='+self.key) as r:
            tr=json.load(r)['transcript'];self.assertEqual(len(tr['segments']),2);self.assertEqual(tr['segments'][0]['start'],1);self.assertTrue(tr['aligned'])
        with self.call('/api/transcript',{**self.payload(),'name':'plain.txt','text':'Plain text','aligned':True}):pass
        with self.call('/api/export') as r:
            data=json.load(r);self.assertGreaterEqual(len(data['transcripts']),2);self.assertFalse(data['transcripts'][-1]['aligned'])

    def test_media_range_and_suffix(self):
        for header,start,end in [('bytes=10-30',10,30),('bytes=-10',len(self.bytes)-10,len(self.bytes)-1),('bytes=0-',0,len(self.bytes)-1)]:
            with self.call('/media?key='+self.key,headers={'Range':header}) as r:
                self.assertEqual(r.status,206);self.assertEqual(r.read(),self.bytes[start:end+1]);self.assertEqual(r.headers['Content-Range'],f'bytes {start}-{end}/{len(self.bytes)}')
        with self.assertRaises(urllib.error.HTTPError) as cm:self.call('/media?key='+self.key,headers={'Range':'bytes=999999-'})
        self.assertEqual(cm.exception.code,416)

    def test_origin_version_and_content_boundaries(self):
        for headers in [{'X-Local-Token':'bad'},{'Origin':'https://attacker.example'},{'Host':'attacker.example'}]:
            with self.assertRaises(urllib.error.HTTPError) as cm:self.call('/api/state',self.payload(),headers)
            self.assertEqual(cm.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as cm:self.call('/api/state',{**self.payload(),'asset':'wrong'})
        self.assertEqual(cm.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError):self.call('/api/state',{**self.payload(),'position':float('nan')})
        with self.assertRaises(urllib.error.HTTPError):self.call('/media?key=../../secret')
        with self.assertRaises(ValueError):ui.parse_transcript('00:00:10,000 --> 00:00:01,000\nBackwards')

    def test_download_queue_deduplication(self):
        with patch.object(ui,'JOBS',[]):
            a=ui.enqueue({'kind':'download','show':'demo','episode':'a'*20})
            b=ui.enqueue({'kind':'download','show':'demo','episode':'a'*20})
            self.assertIn('id',a);self.assertTrue(b['duplicate']);self.assertEqual(len(ui.JOBS),1)

    def test_transcription_queue_version_and_cancellation(self):
        model = self.root/'model'; model.mkdir(exist_ok=True)
        (model/'download-receipt.json').write_text('{}')
        python = self.root/'python.exe'; python.touch()
        with patch.object(ui,'JOBS',[]), patch.object(ui,'ASR_PYTHON',python), patch.object(ui,'ASR_MODEL',model):
            payload = {'kind':'transcribe','show':'demo','episode':'a'*20,'asset':self.payload()['asset']}
            with self.assertRaises(ValueError):ui.enqueue({**payload,'asset':'wrong'})
            result = ui.enqueue(payload)
            self.assertTrue(ui.enqueue(payload)['duplicate'])
            ui.cancel_job({'id':result['id']})
            self.assertEqual(ui.JOBS[0]['status'],'cancelled')
            self.assertIn('id',ui.enqueue(payload))

    def test_generated_transcript_provenance_export_and_sample_guard(self):
        job = {'id':'generated-test','show':'demo','episode':'a'*20,'asset':self.payload()['asset'],'status':'running'}
        _,media,_ = ui.asset_for(self.key)
        output = media.parent/'transcripts'/'generated-test.json'
        metadata = {'audio_sha256':'b'*64,'sample_only':False,'elapsed_seconds':2.5}
        data = {'segments':[{'start':1,'end':3,'text':'Generated text.'}], 'metadata':metadata}
        ui.archive.write_json(output,data)
        output.with_suffix('.srt').write_text('1\n00:00:01,000 --> 00:00:03,000\nGenerated text.\n')
        def process():
            p=MagicMock();p.stdout=io.StringIO('{"progress":50}\n');p.wait.return_value=0;p.poll.return_value=0
            return p
        with patch.object(ui,'ROOT',self.root),patch.object(ui.subprocess,'Popen',return_value=process()):
            ui.run_transcription(job)
        tr=ui.detail(self.key)['transcript']
        self.assertEqual(tr['source'],'local_asr');self.assertEqual(tr['metadata']['elapsed_seconds'],2.5)
        with self.call('/api/transcript-export?key='+self.key+'&format=srt') as r:
            self.assertIn(b'00:00:01,000 --> 00:00:03,000',r.read())
        with self.call('/api/transcript-export?key='+self.key+'&format=txt') as r:
            self.assertEqual(r.read(),b'Generated text.\n')
        metadata['sample_only']=True;ui.archive.write_json(output,data)
        with patch.object(ui,'ROOT',self.root),patch.object(ui.subprocess,'Popen',return_value=process()):
            with self.assertRaises(ValueError):ui.run_transcription(job)


class TranslationTests(ServerTests):
    # Reuse the isolated HTTP fixture, but do not repeat its inherited test methods.
    def setUp(self):
        with ui.database() as db:
            for table in ('transcripts','translations','translation_cache'):
                db.execute('DELETE FROM '+table)
        self.cfg=patch.object(ui.translation,'CONFIG',self.root/'translation-settings.json')
        self.cfg.start()
        ui.translation.CONFIG.unlink(missing_ok=True)
        self.request=patch.object(ui.translation,'request_translation',side_effect=AssertionError('Network is forbidden in tests'))
        self.request.start()
        self.addCleanup(self.request.stop);self.addCleanup(self.cfg.stop)

    def seed(self,count=4):
        text='\n\n'.join(f'{i+1}\n00:00:{i*3:02d},000 --> 00:00:{i*3+2:02d},000\nCamera sentence {i}.' for i in range(count))
        with self.call('/api/transcript',{**self.payload(),'name':'translation.srt','text':text,'aligned':True}):pass
        return {'id':'translation-test','show':'demo','episode':'a'*20,'asset':self.payload()['asset'],'status':'running'}

    def configure(self):
        return ui.translation.save_settings({'model':ui.translation.MODELS[0],'api_key':'fake-test-credential-only'})

    def response(self,lines,*args):
        return json.dumps({'lines':[{'id':r['id'],'zh':'相机句子 '+str(r['id'])} for r in lines]}),{'prompt_tokens':10,'completion_tokens':20}

    def test_translation_missing_key_and_incomplete_export(self):
        job=self.seed();ui.run_translation(job)
        self.assertEqual(job['status'],'waiting_key')
        self.assertEqual(len(ui.detail(self.key)['transcript']['segments']),4)
        with self.assertRaises(urllib.error.HTTPError):self.call('/api/transcript-export?key='+self.key+'&language=both')

    def test_translation_credentials_never_exposed(self):
        self.configure()
        self.assertNotIn('fake-test-credential-only',ui.translation.CONFIG.read_text())
        self.assertEqual(ui.translation.credential()[1],'fake-test-credential-only')
        for path in ('/api/translation-settings','/api/export'):
            with self.call(path) as response:
                body=response.read();self.assertNotIn(b'fake-test-credential-only',body);self.assertNotIn(b'encrypted_key',body)

    def test_translation_preserves_timing_export_and_resumes_without_calls(self):
        self.configure();job=self.seed();before=ui.detail(self.key)['transcript']['segments']
        with patch.object(ui.translation,'request_translation',side_effect=self.response) as request:
            ui.run_translation(job);self.assertEqual(request.call_count,1)
            ui.run_translation(job);self.assertEqual(request.call_count,1)
        tr=ui.detail(self.key)['transcript'];self.assertEqual(tr['translation']['status'],'succeeded')
        self.assertEqual([(s['start'],s['end'],s['text']) for s in before],[(s['start'],s['end'],s['text']) for s in tr['segments']])
        with self.call('/api/transcript-export?key='+self.key+'&format=srt&language=both') as response:
            text=response.read().decode();self.assertIn('00:00:00,000 --> 00:00:02,000',text);self.assertIn('Camera sentence 0.\n相机句子 0',text)

    def test_translation_falls_back_then_splits_without_losing_rows(self):
        self.configure();job=self.seed();models=[]
        def partial(lines,context,model,key):
            models.append(model)
            return self.response(lines[:-1] if len(lines)>2 else lines)
        with patch.object(ui.translation,'request_translation',side_effect=partial):ui.run_translation(job)
        tr=ui.detail(self.key)['transcript'];self.assertEqual(tr['translation']['completed'],4)
        self.assertIn('deepseek-v4-pro',models)
        self.assertEqual(tr['translation']['metadata']['prompt_tokens'],50)

    def test_translation_failure_and_retry_reuses_cache(self):
        self.configure();job=self.seed()
        def one_each(segments):
            for i,s in enumerate(segments):yield [{'id':i,'text':s['text']}]
        def failing(lines,*args):
            if lines[0]['id']==1:raise ValueError('Test interruption')
            return self.response(lines)
        with patch.object(ui.translation,'batches',side_effect=one_each),patch.object(ui.translation,'request_translation',side_effect=failing):
            with self.assertRaisesRegex(ValueError,'Test interruption'):ui.run_translation(job)
        partial=ui.detail(self.key)['transcript']['translation']['completed'];self.assertGreater(partial,0)
        with patch.object(ui.translation,'batches',side_effect=one_each),patch.object(ui.translation,'request_translation',side_effect=self.response) as request:
            ui.run_translation(job);self.assertEqual(request.call_count,4-partial)

    def test_translation_response_rejects_missing_or_shifted_ids(self):
        lines=[{'id':4,'text':'Hello'},{'id':5,'text':'Camera'}]
        for rows in ([{'id':4,'zh':'你好'}],[{'id':5,'zh':'你好'},{'id':4,'zh':'相机'}]):
            with self.assertRaises(ValueError):ui.translation.validate_response(json.dumps({'lines':rows}),lines)


for name in list(ServerTests.__dict__):
    if name.startswith('test_'):
        setattr(TranslationTests,name,None)

if __name__=='__main__':unittest.main()
