import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import podcast_transcribe as asr


class TranscriptionTests(unittest.TestCase):
    def test_short_lines_preserve_words_and_time_bounds(self):
        words = [{'word': text, 'start': i, 'end': i+0.8}
                 for i,text in enumerate(['Hello', ' world.', ' This', ' is', ' a', ' camera!'])]
        result = asr.study_segments([{'start':0,'end':6,'text':'Hello world. This is a camera!','words':words}])
        self.assertEqual([s['text'] for s in result],['Hello world.','This is a camera!'])
        self.assertEqual([w for s in result for w in s['words']],words)
        self.assertTrue(all(0 <= s['start'] < s['end'] <= 6 for s in result))

    def test_zero_duration_trailing_word_is_not_lost(self):
        words=[{'word':'Hello.', 'start':0,'end':1}, {'word':' Again.', 'start':1,'end':1}]
        result=asr.study_segments([{'start':0,'end':1,'text':'Hello. Again.','words':words}])
        self.assertEqual(' '.join(s['text'] for s in result),'Hello. Again.')

    def test_modified_audio_rejected_before_model_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            media=Path(folder)/'audio.mp3';media.write_bytes(b'modified media')
            with patch.object(asr,'setup_dlls'),patch.object(asr,'emit'):
                with self.assertRaisesRegex(ValueError,'音频校验失败'):
                    asr.transcribe(media,Path(folder)/'out.json','0'*64)
            self.assertFalse((Path(folder)/'out.json').exists())


if __name__ == '__main__':
    unittest.main()
