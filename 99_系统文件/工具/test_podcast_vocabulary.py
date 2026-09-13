import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import podcast_server as server
import podcast_vocabulary as vocabulary


class VocabularyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        db=patch.object(server,'DB',Path(tmp.name)/'test.sqlite3');db.start();self.addCleanup(db.stop)
        server.init_db()
        self.item={'key':'demo:'+'a'*20,'asset':'audio-v1','downloaded':True,'transcript':{'id':'transcript-v1','segments':[
            {'text':'A prime lens is sharp.','zh':'定焦镜头很锐利。','start':12.3,'end':15.2},
            {'text':'A different lens.','zh':'另一个镜头。','start':15.2,'end':18}]}}
        detail=patch.object(server,'detail',return_value=self.item);detail.start();self.addCleanup(detail.stop)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=http.serve_forever,daemon=True).start()
        self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        self.url='http://127.0.0.1:'+str(http.server_port)

    def call(self,path,data=None,token=True):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Local-Token']=server.TOKEN
        request=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)

    def payload(self,**extra):
        return {'key':self.item['key'],'asset':'audio-v1','transcript_id':'transcript-v1','cue_index':0,'selected':'prime lens','language':'en',**extra}

    def test_capture_keeps_bilingual_snapshot_and_deduplicates(self):
        result=self.call('/api/vocabulary',self.payload())
        self.assertTrue(self.call('/api/vocabulary',self.payload())['duplicate'])
        self.item['transcript']['segments'][0]['text']='Revised text'
        server.init_db()
        rows=self.call('/api/vocabulary')['items'];self.assertEqual(len(rows),1)
        row=rows[0];self.assertEqual(row['id'],result['id']);self.assertEqual(row['context_en'],'A prime lens is sharp.')
        self.assertEqual(row['context_zh'],'定焦镜头很锐利。');self.assertEqual(row['start'],12.3)
        self.assertEqual(row['status'],'pending');self.assertEqual(row['english'],'prime lens')
        self.assertEqual(len(self.call('/api/export')['vocabulary']),1)

    def test_chinese_capture_keeps_original_and_version(self):
        self.call('/api/vocabulary',self.payload(selected='定焦镜头',language='zh'))
        row=self.call('/api/vocabulary')['items'][0]
        self.assertEqual(row['chinese'],'定焦镜头');self.assertEqual(row['transcript_id'],'transcript-v1')

    def test_reject_stale_unrelated_or_oversized_capture(self):
        for extra in ({'asset':'old'},{'transcript_id':'old'},{'cue_index':True},{'cue_index':99},{'selected':'not in transcript'},{'selected':'x'*501},{'language':'fr'}):
            with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/vocabulary',self.payload(**extra))
            self.assertEqual(error.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/vocabulary',self.payload(),token=False)
        self.assertEqual(error.exception.code,403)
        self.assertEqual(self.call('/api/vocabulary')['items'],[])

    def test_remove_restore_and_export_keep_archive(self):
        result=self.call('/api/vocabulary',self.payload())
        self.call('/api/vocabulary-delete',{'id':result['id']})
        self.assertEqual(self.call('/api/vocabulary')['items'],[])
        self.assertEqual(self.call('/api/vocabulary-export')['items'][0]['deleted'],1)
        self.call('/api/vocabulary-restore',{'id':result['id']})
        self.assertEqual(len(self.call('/api/vocabulary')['items']),1)

    def test_lookup_matches_chinese_to_original_english_and_pro_fallback(self):
        row={'selected':'定焦镜头','language':'zh','context_en':'A prime lens is sharp.','context_zh':'定焦镜头很锐利。'}
        responses=[(json.dumps({'english':'invented words','chinese':'改写'}),{'prompt_tokens':10}),
                   (json.dumps({'english':'prime lens','chinese':'改写'}),{'prompt_tokens':20})]
        with patch.object(server.translation,'credential',return_value=('deepseek-v4-flash','test-key')),patch.object(server.translation,'request_translation',side_effect=responses) as request:
            result,metadata=vocabulary.enrich(row,server.translation)
        self.assertEqual(result,{'english':'prime lens','chinese':'定焦镜头'})
        self.assertEqual(request.call_args.args[2],'deepseek-v4-pro');self.assertEqual(metadata['prompt_tokens'],30)

    def test_english_lookup_preserves_exact_selection(self):
        row={'selected':'prime lens','language':'en','context_en':'A prime lens is sharp.','context_zh':'定焦镜头很锐利。'}
        with patch.object(server.translation,'credential',return_value=('deepseek-v4-flash','test-key')),patch.object(server.translation,'request_translation',return_value=(json.dumps({'english':'changed','chinese':'定焦镜头'}),{})):
            result,_=vocabulary.enrich(row,server.translation)
        self.assertEqual(result['english'],'prime lens');self.assertEqual(result['chinese'],'定焦镜头')


if __name__=='__main__':unittest.main()
