"""Offline Whisper Turbo worker. Run with the isolated podcast-asr Python."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / '99_系统文件/依赖/播客模型/whisper-large-v3-turbo'
REPO = 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'
DLL_HANDLES = []


def emit(**data):
    print(json.dumps(data, ensure_ascii=True), flush=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.part')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def setup_dlls():
    if os.name == 'nt':
        bins = list((Path(sys.prefix) / 'Lib/site-packages/nvidia').glob('*/bin'))
        os.environ['PATH'] = os.pathsep.join(str(p) for p in bins) + os.pathsep + os.environ.get('PATH', '')
        for directory in bins:
            DLL_HANDLES.append(os.add_dll_directory(str(directory)))


def download():
    # Download only the data files used by the upstream faster-whisper Turbo mapping.
    os.environ['HF_HOME'] = str(ROOT / '99_系统文件/缓存/临时文件/podcast_asr/huggingface')
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    # Plain HTTPS avoids Xet stalling on some Windows/network combinations.
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    import ssl
    import httpx
    from huggingface_hub import HfApi, snapshot_download, set_client_factory
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    set_client_factory(lambda: httpx.Client(verify=context, follow_redirects=True,
                                            timeout=httpx.Timeout(120, connect=30)))
    for attempt in range(3):
        try:
            info = HfApi().model_info(REPO, token=False)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
    emit(stage='download', repository=REPO, revision=info.sha)
    for attempt in range(3):
        try:
            snapshot_download(REPO, revision=info.sha, local_dir=str(MODEL), token=False, max_workers=1,
                              allow_patterns=['config.json', 'preprocessor_config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*'])
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
    files = {}
    for path in MODEL.iterdir():
        if path.is_file() and path.name != 'download-receipt.json':
            with path.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            files[path.name] = {'bytes': path.stat().st_size, 'sha256': digest}
    write_json(MODEL / 'download-receipt.json', {'repository': REPO, 'revision': info.sha, 'files': files})
    emit(stage='downloaded', bytes=sum(f['bytes'] for f in files.values()))


def timestamp(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}'


def study_segments(original):
    """Split long recognition blocks using their own word times; never rewrite words."""
    result = []
    for segment in original:
        if not segment['words']:
            result.append(segment)
            continue
        group = []
        for i, word in enumerate(segment['words']):
            group.append(word)
            text = ''.join(w['word'] for w in group).strip()
            if (text.endswith(('.', '?', '!')) or word['end']-group[0]['start'] >= 10
                    or len(text) >= 180 or i == len(segment['words'])-1):
                start = max(segment['start'], group[0]['start'])
                end = min(segment['end'], group[-1]['end'])
                if end > start:
                    result.append({'start': start, 'end': end, 'text': text, 'words': group})
                    group = []
        if group:
            # Preserve any zero-duration trailing words rather than dropping content.
            if result:
                result[-1]['text'] += ''.join(w['word'] for w in group)
                result[-1]['words'] = result[-1]['words'] + group
            else:
                result.append(segment)
    return result


def transcribe(audio, output, expected_hash, seconds=0):
    setup_dlls()
    os.environ['HF_HUB_OFFLINE'] = '1'
    started = time.perf_counter()
    emit(stage='checking', progress=0)
    with audio.open('rb') as f:
        actual_hash = hashlib.file_digest(f, 'sha256').hexdigest()
    if actual_hash != expected_hash:
        raise ValueError('音频校验失败，请重新下载这期节目')
    if not (MODEL / 'model.bin').is_file():
        raise ValueError('本地模型尚未下载完成')
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    from faster_whisper.audio import decode_audio
    emit(stage='loading', progress=0)
    model = WhisperModel(str(MODEL), device='cuda', compute_type='int8_float16',
                         cpu_threads=4, local_files_only=True)
    samples = decode_audio(str(audio), sampling_rate=16000)
    if seconds:
        samples = samples[:int(seconds * 16000)]
    duration = len(samples) / 16000
    loaded = time.perf_counter()
    emit(stage='transcribing', progress=0, duration=duration)
    stream, _ = BatchedInferencePipeline(model).transcribe(
        samples, batch_size=4, language='en', task='transcribe', beam_size=5,
        word_timestamps=True, vad_filter=True,
        vad_parameters={'min_silence_duration_ms': 500})
    segments = []
    for segment in stream:
        start, end = max(0, segment.start), min(duration, segment.end)
        if end > start and segment.text.strip():
            segments.append({'start': round(start, 3), 'end': round(end, 3),
                             'text': segment.text.strip(),
                             'words': [{'start': round(w.start, 3), 'end': round(w.end, 3),
                                        'word': w.word, 'probability': round(w.probability, 4)}
                                       for w in (segment.words or [])]})
        emit(stage='transcribing', progress=min(99, round(end / max(duration, 1) * 100, 1)))
    if not segments:
        raise ValueError('没有识别到英文语音，未保存空稿件')
    recognizer_segments = segments
    segments = study_segments(recognizer_segments)
    finished = time.perf_counter()
    metadata = {'model': 'whisper-large-v3-turbo', 'engine': 'faster-whisper',
                'device': 'cuda', 'compute_type': 'int8_float16', 'batch_size': 4,
                'language': 'en', 'audio_sha256': actual_hash, 'audio_seconds': duration,
                'sample_only': bool(seconds), 'elapsed_seconds': round(finished-started, 2),
                'inference_seconds': round(finished-loaded, 2),
                'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    receipt = MODEL / 'download-receipt.json'
    if receipt.is_file():
        metadata['model_revision'] = json.loads(receipt.read_text(encoding='utf-8'))['revision']
    srt = '\n\n'.join(f'{i}\n{timestamp(s["start"])} --> {timestamp(s["end"])}\n{s["text"]}'
                        for i, s in enumerate(segments, 1)) + '\n'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.txt').write_text('\n'.join(s['text'] for s in segments)+'\n', encoding='utf-8')
    output.with_suffix('.srt').write_text(srt, encoding='utf-8')
    write_json(output, {'source': 'local_asr', 'metadata': metadata, 'segments': segments,
                        'recognizer_segments': recognizer_segments})
    emit(stage='complete', progress=100, metadata=metadata, segments=len(segments))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--audio', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--sample-seconds', type=int, default=0)
    args = parser.parse_args()
    try:
        if args.download:
            download()
        elif args.audio and args.output and args.sha256:
            transcribe(args.audio, args.output, args.sha256, args.sample_seconds)
        else:
            parser.error('Provide --download or --audio, --output and --sha256')
    except Exception as exc:
        emit(stage='error', error=str(exc))
        sys.exit(1)
