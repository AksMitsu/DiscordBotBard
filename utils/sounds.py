"""Хранилище звуков саундборда: data/sounds/<id>.<ext> и индекс index.json."""
import json
import os
import time
import uuid

from config import SOUNDS_DIR

INDEX = os.path.join(SOUNDS_DIR, 'index.json')
ALLOWED_EXT = {'mp3', 'wav', 'ogg', 'opus', 'm4a', 'aac', 'flac', 'webm'}


def _load() -> dict:
    try:
        with open(INDEX, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    tmp = INDEX + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)


def list_all() -> list:
    return sorted(_load().values(), key=lambda s: s['created'])


def get(sid: str):
    return _load().get(sid)


def file_path(sound: dict) -> str:
    return os.path.join(SOUNDS_DIR, sound['file'])


def add(name: str, emoji: str, volume: int, ext: str, data: bytes) -> dict:
    sid = uuid.uuid4().hex[:8]
    filename = f'{sid}.{ext}'
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    with open(os.path.join(SOUNDS_DIR, filename), 'wb') as f:
        f.write(data)
    index = _load()
    index[sid] = {
        'id': sid, 'name': name.strip()[:60] or 'Без названия', 'emoji': emoji.strip()[:8] or '🔊',
        'volume': max(0, min(volume, 200)), 'file': filename, 'ext': ext, 'size': len(data),
        'duration': 0, 'created': time.time(),
    }
    _save(index)
    return index[sid]


def update(sid: str, **fields):
    index = _load()
    sound = index.get(sid)
    if not sound:
        return None
    if fields.get('name') is not None:
        sound['name'] = str(fields['name']).strip()[:60] or sound['name']
    if fields.get('emoji') is not None:
        sound['emoji'] = str(fields['emoji']).strip()[:8] or sound['emoji']
    if fields.get('volume') is not None:
        sound['volume'] = max(0, min(int(fields['volume']), 200))
    if fields.get('duration') is not None:
        sound['duration'] = fields['duration']
    _save(index)
    return sound


def delete(sid: str) -> bool:
    index = _load()
    sound = index.pop(sid, None)
    if not sound:
        return False
    _save(index)
    try:
        os.remove(file_path(sound))
    except FileNotFoundError:
        pass
    return True
