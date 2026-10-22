"""Сохранённые плейлисты владельца (data/playlists.json). Простое файловое хранилище."""
import json
import os
import time
import uuid

from config import DATA_DIR

PATH = os.path.join(DATA_DIR, 'playlists.json')
TRACK_KEYS = ('type', 'provider', 'title', 'webpage_url', 'url', 'yandex_id', 'duration')


def _load() -> dict:
    try:
        with open(PATH, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def list_all() -> list:
    return sorted(_load().values(), key=lambda p: p['created'])


def get(pid: str):
    return _load().get(pid)


def create(name: str) -> dict:
    data = _load()
    pid = uuid.uuid4().hex[:8]
    data[pid] = {'id': pid, 'name': name.strip()[:80] or 'Без названия', 'created': time.time(), 'tracks': []}
    _save(data)
    return data[pid]


def rename(pid: str, name: str):
    data = _load()
    if pid not in data:
        return None
    data[pid]['name'] = name.strip()[:80] or data[pid]['name']
    _save(data)
    return data[pid]


def delete(pid: str) -> bool:
    data = _load()
    if data.pop(pid, None) is None:
        return False
    _save(data)
    return True


def add_tracks(pid: str, tracks: list):
    data = _load()
    if pid not in data:
        return None
    for t in tracks:
        data[pid]['tracks'].append({k: t[k] for k in TRACK_KEYS if t.get(k) is not None})
    _save(data)
    return data[pid]


def remove_track(pid: str, index: int):
    data = _load()
    pl = data.get(pid)
    if pl is None or not (0 <= index < len(pl['tracks'])):
        return None
    pl['tracks'].pop(index)
    _save(data)
    return pl
