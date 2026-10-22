import asyncio
import logging
import os
import time

import discord
import yt_dlp as youtube_dl

from config import FFMPEG_PATH, COOKIES_FILE, YANDEX_TOKEN

log = logging.getLogger('newbot.sources')

# Ссылка на трек живёт ограниченное время — после этого её надо получить заново
STREAM_URL_TTL = 10 * 60

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}


def _base_ydl_opts(**extra):
    opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 15,
        'source_address': '0.0.0.0',  # IPv4: у YouTube через IPv6 часто 403
    }
    # Без файла cookies yt-dlp не должен падать
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    opts.update(extra)
    return opts


def get_source_type(url_or_query: str) -> str:
    url_or_query = url_or_query.strip()
    if url_or_query.startswith(('http://', 'https://')):
        if 'list=' in url_or_query and 'watch?v=' not in url_or_query and 'youtu.be/' not in url_or_query:
            return 'youtube_playlist'
        if 'youtube.com/playlist' in url_or_query:
            return 'youtube_playlist'
        if 'list=' in url_or_query:
            # ссылка на видео внутри плейлиста — трактуем как плейлист
            return 'youtube_playlist'
        if 'youtube.com' in url_or_query or 'youtu.be' in url_or_query:
            return 'youtube_video'
        path = url_or_query.split('?')[0].lower()
        if path.endswith(('.mp3', '.aac', '.ogg', '.m3u8', '.pls', '.opus', '.flac', '.wav')):
            return 'radio_stream'
        return 'other_url'
    return 'youtube_search'


yandex_music = None
if YANDEX_TOKEN:
    try:
        from yandex_music import Client
        yandex_music = Client(YANDEX_TOKEN).init()
        log.info("Яндекс.Музыка подключена")
    except Exception as e:
        log.warning("Ошибка Яндекс.Музыки: %s", e)


def _pick_url(info: dict) -> str:
    if info.get('url'):
        return info['url']
    for fmt in info.get('requested_formats') or []:
        if fmt.get('url'):
            return fmt['url']
    audio = [f for f in info.get('formats') or [] if f.get('url') and f.get('acodec') not in (None, 'none')]
    if audio:
        return audio[-1]['url']
    raise Exception("Не удалось получить ссылку на аудио")


def _info_to_item(info: dict) -> dict:
    return {
        'type': 'track',
        'provider': 'youtube',
        'title': info.get('title') or 'Unknown',
        'webpage_url': info.get('webpage_url') or info.get('original_url') or info.get('url'),
        'duration': info.get('duration') or 0,
        'stream_url': _pick_url(info),
        'resolved_at': time.monotonic(),
    }


async def get_audio_source(query: str, limit: int = 30):
    source_type = get_source_type(query)
    if source_type == 'youtube_playlist':
        return await _get_youtube_playlist(query, limit)
    elif source_type == 'radio_stream':
        return await _get_radio_stream(query)
    else:
        return await _get_youtube_track(query)


async def _get_youtube_track(query: str):
    """Один трек (ссылка или поиск). Ссылка на поток запрашивается сразу, FFmpeg запускается позже."""
    def _extract():
        with youtube_dl.YoutubeDL(_base_ydl_opts()) as ydl:
            if query.startswith(('http://', 'https://')):
                info = ydl.extract_info(query, download=False)
            else:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and info.get('entries'):
                info = next((e for e in info['entries'] if e), None)
            if not info:
                raise Exception("Трек не найден")
            return _info_to_item(info)

    return await asyncio.get_running_loop().run_in_executor(None, _extract)


async def search_youtube(query: str, count: int = 5):
    """Быстрый поиск без получения ссылок на потоки."""
    def _search():
        with youtube_dl.YoutubeDL(_base_ydl_opts(extract_flat=True)) as ydl:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
        results = []
        for e in (info or {}).get('entries') or []:
            if not e:
                continue
            results.append({
                'type': 'track',
                'provider': 'youtube',
                'title': e.get('title') or 'Unknown',
                'webpage_url': e.get('url') or f"https://www.youtube.com/watch?v={e.get('id')}",
                'duration': e.get('duration') or 0,
            })
        return results

    return await asyncio.get_running_loop().run_in_executor(None, _search)


async def _get_youtube_playlist(url: str, limit: int = 30):
    """Плейлист: список треков без ссылок на потоки (они запрашиваются при воспроизведении)."""
    limit = max(1, min(int(limit or 30), 100))

    def _extract():
        opts = _base_ydl_opts(extract_flat='in_playlist', noplaylist=False, playlistend=limit)
        with youtube_dl.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info or 'entries' not in info:
                raise Exception("Не удалось извлечь плейлист")
            return info

    info = await asyncio.get_running_loop().run_in_executor(None, _extract)
    tracks = []
    for entry in list(info['entries'])[:limit]:
        if not entry:
            continue
        webpage_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
        if not webpage_url.startswith('http'):
            webpage_url = f"https://www.youtube.com/watch?v={entry.get('id')}"
        tracks.append({
            'type': 'track',
            'provider': 'youtube',
            'title': entry.get('title') or 'Unknown',
            'webpage_url': webpage_url,
            'duration': entry.get('duration') or 0,
        })
    if not tracks:
        raise Exception("Плейлист пуст или недоступен")
    return {
        'type': 'playlist',
        'title': info.get('title') or 'Неизвестный плейлист',
        'tracks': tracks,
    }


async def _get_radio_stream(url: str, title: str = None):
    return {'type': 'radio', 'provider': 'radio', 'title': title or url, 'url': url}


def _yandex_item(track) -> dict:
    artists = ", ".join(a.name for a in (track.artists or [])) or "Unknown"
    return {
        'type': 'track',
        'provider': 'yandex',
        'title': f"{artists} - {track.title}",
        'yandex_id': str(track.id),
        'duration': (track.duration_ms or 0) // 1000,
    }


def _yandex_direct_link(track) -> str:
    infos = track.get_download_info()
    if not infos:
        raise Exception("Нет ссылки на трек Яндекс.Музыки")
    mp3 = [i for i in infos if i.codec == 'mp3'] or list(infos)
    best = max(mp3, key=lambda i: i.bitrate_in_kbps or 0)
    return best.get_direct_link()


async def search_yandex(query: str):
    if not yandex_music:
        raise Exception("Яндекс.Музыка не настроена")

    def _search():
        res = yandex_music.search(query, type_='track')
        if not res or not res.tracks or not res.tracks.results:
            raise Exception("Трек не найден")
        track = res.tracks.results[0]
        item = _yandex_item(track)
        item['stream_url'] = _yandex_direct_link(track)
        item['resolved_at'] = time.monotonic()
        return item

    return await asyncio.get_running_loop().run_in_executor(None, _search)


async def resolve_item(item: dict) -> dict:
    """Гарантирует, что у трека есть свежая ссылка на поток (stream_url / url для радио)."""
    if item.get('type') == 'radio':
        return item
    fresh = item.get('stream_url') and (time.monotonic() - item.get('resolved_at', 0)) < STREAM_URL_TTL
    if fresh:
        return item

    if item.get('provider') == 'yandex':
        if not yandex_music:
            raise Exception("Яндекс.Музыка не настроена")

        def _refresh():
            track = yandex_music.tracks([item['yandex_id']])[0]
            return _yandex_direct_link(track)

        item['stream_url'] = await asyncio.get_running_loop().run_in_executor(None, _refresh)
        item['resolved_at'] = time.monotonic()
        return item

    target = item.get('webpage_url')
    if not target:
        raise Exception("У трека нет ссылки")
    new = await _get_youtube_track(target)
    item['stream_url'] = new['stream_url']
    item['resolved_at'] = new['resolved_at']
    if not item.get('duration'):
        item['duration'] = new.get('duration', 0)
    return item


def create_source(item: dict, volume: float = 1.0) -> discord.PCMVolumeTransformer:
    """Создаёт FFmpeg-источник. Процесс ffmpeg запускается сразу, поэтому вызывать надо прямо перед play()."""
    url = item['url'] if item.get('type') == 'radio' else item['stream_url']
    audio = discord.FFmpegPCMAudio(url, executable=FFMPEG_PATH, **ffmpeg_options)
    return discord.PCMVolumeTransformer(audio, volume=volume)


def format_duration(seconds) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
