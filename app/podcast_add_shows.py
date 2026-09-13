"""Import the ten previously recommended shows using Apple's public lookup result.

Kept for provenance: this is how the original library was seeded. New shows are
added through the "AI 发现" panel or podcast_discover.py instead.
"""
import json
from pathlib import Path

import paths
import podcast_archive as archive

SHOWS = {
    1474429475: ('waveform', 'Waveform', '数码与生活'),
    1691452657: ('petapixel', 'PetaPixel', '摄影'),
    957171516: ('gamescast', 'Kinda Funny Gamescast', '主机游戏'),
    1528594034: ('hardfork', 'Hard Fork', 'AI 与科技'),
    430333725: ('vergecast', 'The Vergecast', '数码与科技'),
    503494956: ('cultcast', 'The CultCast', 'Apple'),
    617416468: ('atp', 'Accidental Tech Podcast', '科技与编程'),
    262026947: ('bbc6minute', '6 Minute English', '英语与日常'),
    751574016: ('allears', 'All Ears English', '日常交流'),
    1448201565: ('easystories', 'Easy Stories in English', '故事'),
}


if __name__ == '__main__':
    lookup = json.loads((paths.CACHE/'podcast-shows-lookup.json').read_text(encoding='utf-8-sig'))
    registry = []
    for row in lookup:
        sid, name, category = SHOWS[row['collectionId']]
        record = {'id':sid, 'name':name, 'category':category, 'feed_url':row['feedUrl'],
                  'apple_url':row['collectionViewUrl'], 'artwork_url':row.get('artworkUrl600'), 'apple_id':row['collectionId']}
        registry.append(record)
        try:
            if not (archive.folder(sid)/'catalog.json').exists():
                archive.sync(sid, row['feedUrl'])
            cover = archive.folder(sid)/'cover.jpg'
            if not cover.exists() and record['artwork_url']:
                with archive.open_url(record['artwork_url'], timeout=45) as response:
                    raw = response.read(5_000_001)
                if len(raw)>5_000_000 or not raw.startswith(b'\xff\xd8'):
                    raise ValueError('Unexpected cover format')
                cover.write_bytes(raw)
            print(json.dumps({'show':sid,'status':'ready'},ensure_ascii=True),flush=True)
        except Exception as exc:
            print(json.dumps({'show':sid,'error':str(exc)},ensure_ascii=True),flush=True)
    order = list(SHOWS)
    registry.sort(key=lambda x:order.index(x['apple_id']))
    archive.write_json(paths.SHOWS_JSON,registry)
