"""Режим просмотра: те же /api-ответы, но данные берутся напрямую из Discord REST.
Подключения к шлюзу нет, поэтому работающий на хосте бот этому режиму не мешает."""
import asyncio
import json
import os
import re
import time

import aiohttp
from aiohttp import web

API = 'https://discord.com/api/v10'
CDN = 'https://cdn.discordapp.com'
UNAVAILABLE = 'Недоступно в режиме просмотра: нужен админ-API на хосте (ADMIN_MODE=remote)'
EMPTY_PLAYER = {
    'connected': False, 'channel_id': None, 'channel_name': None, 'playing': False, 'paused': False,
    'latency_ms': None, 'current': None, 'elapsed': None, 'loop': 'off', 'volume': 100,
    'queue_len': 0, 'queue_duration': 0, 'queue': [], 'text_channel_id': None,
}
EMPTY_LISTS = {
    '/api/playlists': [], '/api/radios': [], '/api/sounds': [], '/api/events': [], '/api/errors': [],
    '/api/scheduled': [], '/api/logs': {'lines': []}, '/api/commands': {'slash': [], 'prefix': []},
}


class RestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class RestBackend:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        self.token = token
        self.session = session
        self.started = time.time()
        self._cache = {}
        self._voice = {}

    async def _call(self, method, path, *, json=None, params=None, ttl=0, form=None):
        key = (method, path, tuple(sorted((params or {}).items())))
        hit = self._cache.get(key)
        if ttl and hit and time.time() - hit[0] < ttl:
            return hit[1]
        headers = {'Authorization': f'Bot {self.token}', 'User-Agent': 'NBD-AdminUI (readonly, 1.0)'}
        for _ in range(3):
            async with self.session.request(method, API + path, json=json, data=form, params=params, headers=headers) as r:
                if r.status == 429:
                    retry = (await r.json(content_type=None) or {}).get('retry_after', 1)
                    await asyncio.sleep(min(float(retry), 5))
                    continue
                data = await r.json(content_type=None) if r.content_length != 0 else {}
                if r.status >= 400:
                    raise RestError(r.status, (data or {}).get('message', f'Discord API: {r.status}'))
                if ttl:
                    self._cache[key] = (time.time(), data)
                return data
        raise RestError(429, 'Discord ограничил запросы, повторите позже')

    async def voice_state(self, gid: str, uid: str):
        """Голосовой канал пользователя через REST; None, если не в голосе или нет доступа."""
        key = (gid, uid)
        hit = self._voice.get(key)
        if hit and time.time() - hit[0] < 10:
            return hit[1]
        try:
            state = await self._call('GET', f'/guilds/{gid}/voice-states/{uid}')
        except RestError:
            state = None
        self._voice[key] = (time.time(), state)
        return state

    async def owner_ids(self) -> set:
        ids = {x for x in os.getenv('OWNER_IDS', '').replace(' ', '').split(',') if x}
        try:
            app = await self._call('GET', '/oauth2/applications/@me', ttl=3600)
            ids.add((app.get('team') or {}).get('owner_user_id') or app['owner']['id'])
        except (RestError, KeyError):
            pass
        return ids

    async def member_info(self, gid: str, uid: str) -> dict:
        try:
            m = await self._call('GET', f'/guilds/{gid}/members/{uid}', ttl=300)
            u = m['user']
            return {'name': m.get('nick') or u.get('global_name') or u['username'], 'username': u['username'],
                    'discriminator': _disc(u)}
        except (RestError, KeyError):
            try:
                u = await self._call('GET', f'/users/{uid}', ttl=300)
                return {'name': u.get('global_name') or u['username'], 'username': u['username'],
                        'discriminator': _disc(u)}
            except (RestError, KeyError):
                return {'name': f'user {uid}', 'username': None, 'discriminator': None}

    async def me(self):
        return await self._call('GET', '/users/@me', ttl=300)

    async def guilds(self):
        return await self._call('GET', '/users/@me/guilds', params={'with_counts': 'true', 'limit': '200'}, ttl=20)

    async def overview(self) -> dict:
        me, guilds = await self.me(), await self.guilds()
        avatar = f"{CDN}/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get('avatar') else None
        return {
            'mode': 'readonly', 'ready': True, 'user': me['username'], 'user_id': me['id'], 'avatar': avatar,
            'uptime': None, 'ping_ms': None, 'guilds': len(guilds), 'voice_connections': 0, 'playing': 0,
            'memory_mb': None, 'extensions': [], 'errors': 0, 'versions': {}, 'platform': 'Discord REST', 'host': '',
            'presence': None,
        }

    def _guild_info(self, g) -> dict:
        icon = f"{CDN}/icons/{g['id']}/{g['icon']}.png?size=64" if g.get('icon') else None
        return {'id': g['id'], 'name': g['name'], 'members': g.get('approximate_member_count'),
                'icon': icon, 'player': EMPTY_PLAYER}

    async def snapshot(self) -> dict:
        me, guilds = await self.me(), await self.guilds()
        states = await asyncio.gather(*(self.voice_state(g['id'], me['id']) for g in guilds))
        infos = []
        for g, vs in zip(guilds, states):
            info = self._guild_info(g)
            if vs and vs.get('channel_id'):
                names = {c['id']: c['name'] for c in await self._channels(g['id'])}
                info['player'] = dict(EMPTY_PLAYER, connected=True, channel_id=vs['channel_id'],
                                      channel_name=names.get(vs['channel_id']))
            infos.append(info)
        overview = await self.overview()
        overview['voice_connections'] = sum(1 for i in infos if i['player']['connected'])
        return {'overview': overview, 'guilds': infos}

    async def _channels(self, gid: str) -> list:
        return await self._call('GET', f'/guilds/{gid}/channels', ttl=60)

    async def guild_detail(self, gid: str) -> dict:
        guilds = await self.guilds()
        g = next((x for x in guilds if x['id'] == gid), None)
        if not g:
            raise RestError(404, 'Сервер не найден')
        channels = await self._channels(gid)
        categories = {c['id']: c['name'] for c in channels if c['type'] == 4}
        info = self._guild_info(g)
        me = await self.me()
        present = {}
        for uid in {me['id'], *await self.owner_ids()}:
            vs = await self.voice_state(gid, uid)
            if vs and vs.get('channel_id'):
                present.setdefault(vs['channel_id'], []).append(
                    {'id': uid, 'bot': uid == me['id'],
                     **({'name': me['username'], 'username': me['username'], 'discriminator': _disc(me)}
                        if uid == me['id'] else await self.member_info(gid, uid))})
        info['voice_channels'] = [{'id': c['id'], 'name': c['name'], 'members': present.get(c['id'], []),
                                   'can_connect': False}
                                  for c in sorted(channels, key=lambda c: c.get('position', 0)) if c['type'] == 2]
        info['voice_partial'] = True
        ordered = sorted(channels, key=lambda c: (c['type'] != 0, c.get('position', 0)))
        info['text_channels'] = [{'id': c['id'], 'name': c['name'], 'category': categories.get(c.get('parent_id')),
                                  'voice': c['type'] != 0, 'can_send': True, 'can_read': True}
                                 for c in ordered if c['type'] in (0, 2, 13)]
        return info

    async def messages(self, cid: str, limit: int) -> list:
        me = await self.me()
        raw = await self._call('GET', f'/channels/{cid}/messages', params={'limit': str(limit)})
        out = [{
            'id': m['id'], 'author': (m['author'].get('global_name') or m['author']['username']),
            'author_id': m['author']['id'], 'username': m['author']['username'], 'discriminator': _disc(m['author']),
            'bot': bool(m['author'].get('bot')), 'self': m['author']['id'] == me['id'], 'content': m['content'][:1500],
            'pinned': bool(m.get('pinned')), 'attachments': [a['filename'] for a in m.get('attachments', [])], 'embeds': len(m.get('embeds', [])),
            'time': _ts(m['timestamp']),
        } for m in raw]
        out.reverse()
        return out

    async def say(self, body: dict) -> dict:
        content = str(body.get('content', '')).strip()
        if not content or len(content) > 2000:
            raise RestError(400, 'Сообщение должно быть от 1 до 2000 символов')
        payload = {'content': content,
                   'allowed_mentions': {'parse': ['users', 'roles', 'everyone'] if body.get('allow_mentions') else ['users']}}
        if body.get('reply_to'):
            payload['message_reference'] = {'message_id': str(body['reply_to']), 'fail_if_not_exists': False}
        msg = await self._call('POST', f"/channels/{body.get('channel_id')}/messages", json=payload)
        return {'ok': True, 'message_id': msg['id']}


async def upload(backend: RestBackend, request: web.Request, cid_hint=None) -> dict:
    fields, data, filename = {}, None, ''
    async for part in await request.multipart():
        if part.name == 'file':
            filename = part.filename or ''
            data = await part.read(decode=False)
        elif part.name:
            fields[part.name] = (await part.text())[:2000]
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if not data or ext not in {'gif', 'png', 'jpg', 'jpeg', 'webp', 'mp4', 'webm', 'mov'}:
        raise RestError(400, 'Допустимые форматы: gif, png, jpg, jpeg, webp, mp4, webm, mov')
    payload = {'content': fields.get('caption', '').strip(),
               'allowed_mentions': {'parse': ['users', 'roles', 'everyone'] if fields.get('allow_mentions') == '1' else ['users']}}
    form = aiohttp.FormData()
    form.add_field('payload_json', json.dumps(payload), content_type='application/json')
    form.add_field('files[0]', data, filename=re.sub(r'[^\w.\-]+', '_', filename) or f'file.{ext}')
    msg = await backend._call('POST', f"/channels/{fields.get('channel_id')}/messages", form=form)
    return {'ok': True, 'message_id': msg['id']}


def _disc(user: dict):
    d = user.get('discriminator')
    return d if d and d != '0' else None


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


async def handle(backend: RestBackend, request: web.Request) -> web.Response:
    path, method = request.path, request.method
    try:
        if path == '/api/ping':
            return web.json_response({'ok': True, 'ready': True, 'mode': 'readonly'})
        if path == '/api/me':
            return web.json_response({'who': 'owner', 'role': 'admin'})
        if method == 'GET':
            if path == '/api/overview':
                return web.json_response(await backend.overview())
            if path == '/api/guilds':
                return web.json_response((await backend.snapshot())['guilds'])
            if path in EMPTY_LISTS:
                return web.json_response(EMPTY_LISTS[path])
            parts = path.strip('/').split('/')
            if len(parts) == 3 and parts[1] == 'guilds':
                return web.json_response(await backend.guild_detail(parts[2]))
            if len(parts) == 4 and parts[1] == 'channels' and parts[3] == 'messages':
                limit = max(1, min(int(request.query.get('limit', 30)), 100))
                return web.json_response(await backend.messages(parts[2], limit))
        if method == 'POST' and path.startswith('/api/guilds/') and path.endswith('/upload'):
            return web.json_response(await upload(backend, request))
        if method == 'POST' and path.startswith('/api/guilds/') and path.endswith('/say'):
            return web.json_response(await backend.say(await request.json()))
        raise RestError(501, UNAVAILABLE)
    except RestError as e:
        return web.json_response({'error': e.message}, status=e.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return web.json_response({'error': f'Нет связи с Discord: {type(e).__name__}'}, status=502)
    except (ValueError, KeyError) as e:
        return web.json_response({'error': f'Некорректный запрос: {e}'}, status=400)
