"""Админ-API (HTTP + WebSocket) внутри процесса бота для admin_ui. Доступ: мастер-ключ владельца, ADMIN_API_TOKEN, ADMIN_GUEST_TOKENS.
Аналога !eval здесь нет намеренно: выполнение кода по сети недопустимо."""
import asyncio
import datetime
import hashlib
import hmac
import itertools
import json
import logging
import math
import io
import os
import platform
import random
import re
import sys
import time
import uuid
from collections import deque

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

import config
from cogs.music_cog import PRESET_RADIOS, LOOP_MODES, _is_active, apply_volume
from utils import logs, playlists, sounds
from utils.mixer import MixSource
from utils.sources import (
    get_audio_source, get_source_type, search_yandex, search_youtube, _get_radio_stream,
)

log = logging.getLogger('newbot.api')

ADMIN_TAG = '🛠 Admin'
UPLOAD_MAX_MB = 25
UPLOAD_EXT = {'gif', 'png', 'jpg', 'jpeg', 'webp', 'mp4', 'webm', 'mov'}
CHAT_CHANNELS = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
ACTIVITY_TYPES = {
    'playing': discord.ActivityType.playing, 'listening': discord.ActivityType.listening,
    'watching': discord.ActivityType.watching, 'competing': discord.ActivityType.competing,
}
STATUSES = {
    'online': discord.Status.online, 'idle': discord.Status.idle,
    'dnd': discord.Status.dnd, 'invisible': discord.Status.invisible,
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _num(value, name='id') -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f'Некорректное значение: {name}')


def _mentions(request, body) -> discord.AllowedMentions:
    """@everyone/@here и роли разрешаются только явным флагом и только полному (не гостевому) доступу."""
    if not body.get('allow_mentions'):
        return discord.AllowedMentions(everyone=False, roles=False, users=True)
    if request['role'] != 'admin':
        raise ApiError(403, 'Гостевому доступу нельзя упоминать всех и роли')
    return discord.AllowedMentions.all()


def _disc(user):
    d = getattr(user, 'discriminator', None)
    return d if d and d != '0' else None


def _track(item: dict) -> dict:
    if not item:
        return None
    return {
        'type': item.get('type'), 'provider': item.get('provider'), 'title': item.get('title'),
        'url': item.get('webpage_url') or (item.get('url') if item.get('type') == 'radio' else None),
        'duration': item.get('duration') or 0, 'requester': item.get('requester'),
    }


def _safe(obj):
    """ID Discord не помещаются в JS-число (>2^53) — отдаём их строками."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return str(obj) if abs(obj) > 2 ** 53 else obj
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, deque)):
        return [_safe(v) for v in obj]
    return obj


def _dumps(obj) -> str:
    return json.dumps(_safe(obj), ensure_ascii=False, default=str)


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=_dumps)


RESTRICTED_FOR_GUESTS = ('/api/admin/', '/api/logs', '/api/errors')


def _match(supplied: str, tokens: dict):
    found = None
    for token, name in tokens.items():
        if hmac.compare_digest(supplied.encode(), token.encode()):
            found = name
    return found


def _authenticate(supplied: str):
    if hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), config.MASTER_KEY_HASH):
        return 'admin', 'owner'
    name = _match(supplied, config.ADMIN_TOKENS)
    if name:
        return 'admin', name
    name = _match(supplied, config.GUEST_TOKENS)
    return ('guest', name) if name else (None, None)


@web.middleware
async def _middleware(request: web.Request, handler):
    supplied = request.headers.get('Authorization', '')
    supplied = supplied[7:] if supplied.startswith('Bearer ') else request.query.get('token', '')
    role, who = _authenticate(supplied)
    if not role:
        await asyncio.sleep(0.5)
        return _json({'error': 'unauthorized'}, 401)
    if role == 'guest' and request.path.startswith(RESTRICTED_FOR_GUESTS):
        return _json({'error': 'Недостаточно прав для гостевого доступа'}, 403)
    request['who'] = who
    request['role'] = role
    try:
        return await handler(request)
    except ApiError as e:
        return _json({'error': e.message}, e.status)
    except web.HTTPException:
        raise
    except discord.Forbidden:
        return _json({'error': 'У бота нет прав на это действие в Discord'}, 403)
    except discord.NotFound:
        return _json({'error': 'Не найдено в Discord (сообщение, канал или пользователь удалены)'}, 404)
    except discord.HTTPException as e:
        return _json({'error': f'Discord отклонил запрос: {e.text or e.status}'}, 502)
    except asyncio.TimeoutError:
        return _json({'error': 'Операция не уложилась во время ожидания'}, 504)
    except Exception as e:
        eid = logs.record_error(f'api {request.method} {request.path}', e)
        return _json({'error': f'{type(e).__name__}: {str(e)[:300]}', 'error_id': eid}, 500)


class ApiCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.events = deque(maxlen=300)
        self._event_ids = itertools.count(1)
        self.clients: set = set()
        self.runner = None
        self.task = None
        self._signature = {}
        self.leave_after = set()
        self.scheduled = {}
        self._tasks = set()
        self.site_task = None

    @property
    def music(self):
        cog = self.bot.get_cog('MusicCog')
        if not cog:
            raise ApiError(503, 'MusicCog не загружен')
        return cog

    async def cog_load(self):
        app = web.Application(middlewares=[_middleware], client_max_size=(max(config.SOUND_MAX_MB, UPLOAD_MAX_MB) + 1) * 1024 * 1024)
        app['cog'] = self
        r = app.router
        r.add_get('/api/ping', self.h_ping)
        r.add_get('/api/me', self.h_me)
        r.add_get('/api/overview', self.h_overview)
        r.add_get('/api/guilds', self.h_guilds)
        r.add_get('/api/guilds/{gid}', self.h_guild)
        r.add_post('/api/guilds/{gid}/player/{action}', self.h_player)
        r.add_post('/api/guilds/{gid}/say', self.h_say)
        r.add_get('/api/channels/{cid}/messages', self.h_messages)
        r.add_get('/api/radios', self.h_radios)
        r.add_get('/api/playlists', self.h_pl_list)
        r.add_post('/api/playlists', self.h_pl_create)
        r.add_patch('/api/playlists/{pid}', self.h_pl_rename)
        r.add_delete('/api/playlists/{pid}', self.h_pl_delete)
        r.add_post('/api/playlists/{pid}/tracks', self.h_pl_add)
        r.add_delete('/api/playlists/{pid}/tracks/{idx}', self.h_pl_remove)
        r.add_get('/api/sounds', self.h_sounds)
        r.add_post('/api/sounds', self.h_sound_upload)
        r.add_patch('/api/sounds/{sid}', self.h_sound_edit)
        r.add_delete('/api/sounds/{sid}', self.h_sound_delete)
        r.add_get('/api/sounds/{sid}/file', self.h_sound_file)
        r.add_post('/api/guilds/{gid}/sound/{action}', self.h_sound_play)
        r.add_post('/api/guilds/{gid}/chat/{action}', self.h_chat)
        r.add_post('/api/guilds/{gid}/nick', self.h_nick)
        r.add_post('/api/guilds/{gid}/upload', self.h_upload)
        r.add_post('/api/guilds/{gid}/queue/save', self.h_queue_save)
        r.add_get('/api/scheduled', self.h_scheduled)
        r.add_delete('/api/scheduled/{sid}', self.h_scheduled_cancel)
        r.add_get('/api/commands', self.h_commands)
        r.add_post('/api/admin/{action}', self.h_admin)
        r.add_get('/api/errors', self.h_errors)
        r.add_get('/api/logs', self.h_logs)
        r.add_get('/api/events', self.h_events)
        r.add_get('/ws', self.h_ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site_task = asyncio.create_task(self._start_site())
        self.task = asyncio.create_task(self._watch_loop())

    async def _start_site(self):
        """Порт может быть ещё занят старым процессом после перезапуска — пробуем, пока не освободится."""
        attempt = 0
        while True:
            try:
                await web.TCPSite(self.runner, config.ADMIN_API_HOST, config.ADMIN_API_PORT).start()
                log.info('Админ-API слушает %s:%s', config.ADMIN_API_HOST, config.ADMIN_API_PORT)
                return
            except OSError as e:
                attempt += 1
                if attempt in (1, 10) or attempt % 60 == 0:
                    log.warning('Порт админ-API %s занят (%s), повторяю каждые 5 с', config.ADMIN_API_PORT, e)
                await asyncio.sleep(5)

    async def cog_unload(self):
        for task in (self.task, self.site_task, *self._tasks):
            if task:
                task.cancel()
        for item in self.scheduled.values():
            item['task'].cancel()
        for ws in list(self.clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()

    def emit(self, kind: str, text: str, guild_id: int = None):
        ev = {'id': next(self._event_ids), 'time': time.time(), 'kind': kind, 'text': text, 'guild_id': guild_id}
        self.events.append(ev)
        self._broadcast({'type': 'event', 'data': ev})

    def _broadcast(self, payload: dict):
        if not self.clients:
            return
        text = _dumps(payload)
        for ws in list(self.clients):
            self._background(self._safe_send(ws, text), quiet=True)

    async def _safe_send(self, ws, text):
        try:
            await ws.send_str(text)
        except Exception:
            self.clients.discard(ws)

    async def _watch_loop(self):
        while True:
            try:
                await asyncio.sleep(2)
                snap = self.snapshot()
                for g in snap['guilds']:
                    p = g['player']
                    sig = (p['channel_id'], p['current']['title'] if p['current'] else None, p['paused'])
                    old = self._signature.get(g['id'])
                    if old is not None and old != sig:
                        if sig[1] and sig[1] != old[1]:
                            self.emit('track', f'▶ {sig[1]}', g['id'])
                        elif old[1] and not sig[1]:
                            self.emit('track', '⏹ Воспроизведение закончилось', g['id'])
                        if sig[2] != old[2] and sig[1]:
                            self.emit('player', '⏸ Пауза' if sig[2] else '▶ Продолжено', g['id'])
                    self._signature[g['id']] = sig
                self._broadcast({'type': 'snapshot', 'data': snap})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning('watch loop: %s', e)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        params = ' '.join(f'{k}={str(v)[:40]}' for k, v in (interaction.namespace.__dict__ or {}).items())
        where = interaction.guild.name if interaction.guild else 'ЛС'
        self.emit('command', f'{interaction.user.display_name} → /{command.qualified_name} {params} ({where})',
                  interaction.guild_id)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        self.emit('command', f'{ctx.author.display_name} → {ctx.prefix}{ctx.command} (админ)',
                  ctx.guild.id if ctx.guild else None)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self.emit('guild', f'Бот добавлен на сервер {guild.name}', guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self.emit('guild', f'Бот удалён с сервера {guild.name}', guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id != self.bot.user.id or before.channel == after.channel:
            return
        if after.channel and not before.channel:
            self.emit('voice', f'Зашёл в 🔊 {after.channel.name}', member.guild.id)
        elif before.channel and not after.channel:
            self.emit('voice', f'Вышел из 🔊 {before.channel.name}', member.guild.id)
        else:
            self.emit('voice', f'Перешёл 🔊 {before.channel.name} → {after.channel.name}', member.guild.id)

    def player_state(self, guild: discord.Guild, with_queue=True) -> dict:
        music = self.bot.get_cog('MusicCog')
        vc = guild.voice_client
        connected = bool(vc and vc.is_connected())
        out = {
            'connected': connected, 'channel_id': vc.channel.id if connected and vc.channel else None,
            'channel_name': vc.channel.name if connected and vc.channel else None,
            'playing': bool(vc and vc.is_playing()), 'paused': bool(vc and vc.is_paused()),
            'latency_ms': None, 'current': None, 'elapsed': None, 'loop': 'off', 'volume': 100,
            'queue_len': 0, 'queue_duration': 0, 'queue': [], 'text_channel_id': None,
        }
        if vc and connected and math.isfinite(vc.latency):
            out['latency_ms'] = round(vc.latency * 1000)
        if music:
            st = music.state(guild.id)
            queue = music.queue_manager.get_queue(guild.id)
            out.update(current=_track(st.current), loop=st.loop, volume=round(st.volume * 100),
                       queue_len=len(queue), queue_duration=sum(i.get('duration') or 0 for i in queue),
                       text_channel_id=st.text_channel_id)
            if st.current and out['playing']:
                out['elapsed'] = int(time.monotonic() - st.started_at)
            if with_queue:
                out['queue'] = [_track(i) for i in queue[:100]]
        return out

    def guild_info(self, guild: discord.Guild) -> dict:
        return {
            'id': guild.id, 'name': guild.name, 'members': guild.member_count,
            'icon': guild.icon.with_size(64).url if guild.icon else None,
            'player': self.player_state(guild),
        }

    def snapshot(self) -> dict:
        return {'overview': self.overview(), 'guilds': [self.guild_info(g) for g in self.bot.guilds]}

    def overview(self) -> dict:
        bot = self.bot
        vcs = bot.voice_clients
        mem = None
        try:
            import psutil
            mem = round(psutil.Process().memory_info().rss / 1024 ** 2)
        except Exception:
            pass
        try:
            import yt_dlp
            ytdlp = yt_dlp.version.__version__
        except Exception:
            ytdlp = '?'
        return {
            'mode': 'full', 'ready': bot.is_ready(), 'user': str(bot.user) if bot.user else None,
            'user_id': bot.user.id if bot.user else None,
            'avatar': bot.user.display_avatar.with_size(64).url if bot.user else None,
            'uptime': int(time.time() - bot.started_at),
            'ping_ms': round(bot.latency * 1000) if math.isfinite(bot.latency) else None,
            'guilds': len(bot.guilds), 'voice_connections': len(vcs),
            'playing': sum(1 for v in vcs if v.is_playing()), 'memory_mb': mem,
            'extensions': list(bot.extensions), 'errors': len(logs.ERRORS),
            'versions': {'python': platform.python_version(), 'discord.py': discord.__version__, 'yt-dlp': ytdlp},
            'platform': platform.platform(), 'host': platform.node(),
            'presence': getattr(bot.activity, 'name', None),
        }

    def _guild(self, gid) -> discord.Guild:
        g = self.bot.get_guild(_num(gid, 'guild_id'))
        if not g:
            raise ApiError(404, 'Сервер не найден')
        return g

    async def h_ping(self, request):
        return _json({'ok': True, 'ready': self.bot.is_ready()})

    async def h_me(self, request):
        return _json({'who': request['who'], 'role': request['role']})

    async def h_overview(self, request):
        return _json(self.overview())

    async def h_guilds(self, request):
        return _json([self.guild_info(g) for g in self.bot.guilds])

    async def h_guild(self, request):
        g = self._guild(request.match_info['gid'])
        me = g.me
        info = self.guild_info(g)
        info['voice_channels'] = []
        for ch in g.voice_channels:
            members = []
            for uid in ch.voice_states:
                m = g.get_member(uid)
                members.append({'id': uid, 'name': m.display_name if m else f'user {uid}', 'username': m.name if m else None,
                                'discriminator': _disc(m), 'bot': bool(m and m.bot)})
            perms = ch.permissions_for(me)
            info['voice_channels'].append({
                'id': ch.id, 'name': ch.name, 'members': members,
                'can_connect': perms.connect and perms.speak,
            })
        info['text_channels'] = [
            {'id': ch.id, 'name': ch.name, 'category': ch.category.name if ch.category else None,
             'voice': isinstance(ch, (discord.VoiceChannel, discord.StageChannel)),
             'can_send': ch.permissions_for(me).send_messages, 'can_read': ch.permissions_for(me).read_message_history}
            for ch in (*g.text_channels, *g.voice_channels, *g.stage_channels) if ch.permissions_for(me).view_channel
        ]
        info['info'] = {
            'owner_id': g.owner_id, 'created': g.created_at.timestamp(), 'boosts': g.premium_subscription_count,
            'tier': g.premium_tier, 'roles': len(g.roles), 'emojis': len(g.emojis), 'nick': me.nick,
            'text': len(g.text_channels), 'voice': len(g.voice_channels),
        }
        return _json(info)

    async def h_messages(self, request):
        ch = self.bot.get_channel(_num(request.match_info['cid'], 'channel_id'))
        if not isinstance(ch, discord.abc.Messageable) or not hasattr(ch, 'guild'):
            raise ApiError(404, 'Канал не найден')
        limit = max(1, min(_num(request.query.get('limit', 30), 'limit'), 100))
        out = []
        async for m in ch.history(limit=limit):
            out.append({
                'id': m.id, 'author': m.author.display_name, 'author_id': m.author.id, 'username': m.author.name,
                'discriminator': _disc(m.author), 'bot': m.author.bot,
                'self': m.author.id == self.bot.user.id, 'content': m.content[:1500],
                'attachments': [a.filename for a in m.attachments], 'embeds': len(m.embeds),
                'pinned': m.pinned, 'time': m.created_at.timestamp(),
            })
        out.reverse()
        return _json(out)

    async def h_radios(self, request):
        return _json([{'id': k, 'name': k.replace('_', ' ').title(), 'url': v} for k, v in PRESET_RADIOS.items()])

    async def h_errors(self, request):
        return _json([{'id': e['id'], 'time': e['time'], 'context': e['context'], 'trace': e['trace']}
                      for e in reversed(logs.ERRORS)])

    async def h_logs(self, request):
        n = max(1, min(_num(request.query.get('n', 200), 'n'), 2000))
        try:
            with open(config.LOG_FILE, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()[-n:]
        except FileNotFoundError:
            lines = []
        return _json({'lines': [l.rstrip('\n') for l in lines]})

    async def h_events(self, request):
        since = _num(request.query.get('since', 0), 'since')
        return _json([e for e in self.events if e['id'] > since])

    async def h_commands(self, request):
        slash = []
        for c in self.bot.tree.get_commands():
            slash.append({'name': c.name, 'description': c.description,
                          'params': [p.name + ('?' if not p.required else '') for p in c.parameters]})
        prefix = [{'name': c.name, 'usage': f'{config.DEFAULT_PREFIX}{c.name} {c.signature}'.strip(),
                   'description': c.help or c.short_doc or ''}
                  for c in sorted(self.bot.commands, key=lambda c: c.name)
                  if c.cog_name == 'Admin']
        return _json({'slash': sorted(slash, key=lambda c: c['name']), 'prefix': prefix})

    async def h_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_str(_dumps({'type': 'snapshot', 'data': self.snapshot()}))
            await ws.send_str(_dumps({'type': 'events', 'data': list(self.events)[-100:]}))
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)
        return ws

    async def h_player(self, request):
        guild = self._guild(request.match_info['gid'])
        action = request.match_info['action']
        body = await self._body(request)
        result = await self.player_action(guild, action, body)
        self.emit('admin', f'{request["who"]}: {action}' + (f' {str(body.get("query"))[:60]}' if body.get('query') else ''),
                  guild.id)
        return _json({'ok': True, **(result or {}), 'player': self.player_state(guild)})

    @staticmethod
    async def _body(request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except json.JSONDecodeError:
            raise ApiError(400, 'Некорректный JSON')
        return data if isinstance(data, dict) else {}

    async def _prepare_voice(self, guild, body, *, require_channel=True):
        music = self.music
        st = music.state(guild.id)
        vc = guild.voice_client
        if body.get('text_channel_id'):
            st.text_channel_id = _num(body['text_channel_id'], 'text_channel_id')
        if body.get('channel_id'):
            ch = guild.get_channel(_num(body['channel_id'], 'channel_id'))
            if not isinstance(ch, discord.VoiceChannel):
                raise ApiError(400, 'Это не голосовой канал')
            if not ch.permissions_for(guild.me).connect:
                raise ApiError(403, 'У бота нет права подключаться к этому каналу')
            st.voice_channel_id = ch.id
            try:
                if vc and vc.is_connected():
                    if vc.channel.id != ch.id:
                        if _is_active(vc) and not body.get('force'):
                            raise ApiError(409, f'Бот играет в «{vc.channel.name}». Остановите воспроизведение или переместите бота.')
                        await vc.move_to(ch)
                else:
                    if vc:
                        await vc.disconnect(force=True)
                    await ch.connect(timeout=20, reconnect=True)
            except (asyncio.TimeoutError, discord.ClientException) as e:
                raise ApiError(504, f'Не удалось подключиться к «{ch.name}»: {e or "таймаут"}')
        elif require_channel and not (vc and vc.is_connected()):
            raise ApiError(400, 'Бот не в голосовом канале — выберите канал')
        return st

    def _need_vc(self, guild) -> discord.VoiceClient:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            raise ApiError(409, 'Бот не в голосовом канале')
        return vc

    async def _enqueue(self, guild, items, *, announce=False, position='end') -> dict:
        music = self.music
        for it in items:
            it['requester'] = ADMIN_TAG
        was_idle = not _is_active(guild.voice_client)
        queue = music.queue_manager.get_queue(guild.id)
        if position == 'next':
            queue[0:0] = items
        else:
            queue.extend(items)
        started = await music._play_next(guild.id, notify=announce) if was_idle else None
        if was_idle and not started and not _is_active(guild.voice_client):
            raise ApiError(502, 'Не удалось начать воспроизведение — подробности во вкладке «Ошибки»')
        return {'added': len(items), 'started': started['title'] if started else None}

    async def _resolve_query(self, query, source, limit):
        query = query.strip()
        if not query:
            raise ApiError(400, 'Пустой запрос')
        if source == 'yandex' or query.lower().startswith('ya:'):
            return [await search_yandex(query[3:].strip() if query.lower().startswith('ya:') else query)]
        if source == 'radio':
            if not query.startswith(('http://', 'https://')):
                raise ApiError(400, 'Для радио нужна прямая ссылка на поток (http/https)')
            return [await _get_radio_stream(query, '📻 Радио')]
        result = await get_audio_source(query, limit=limit)
        return list(result['tracks']) if result['type'] == 'playlist' else [result]

    async def player_action(self, guild, action, body):
        music = self.music
        gid = guild.id
        st = music.state(gid)
        qm = music.queue_manager

        if action == 'join':
            await self._prepare_voice(guild, body)
            if not _is_active(guild.voice_client):
                music._schedule_idle_disconnect(gid)
            return {}
        if action == 'play':
            await self._prepare_voice(guild, body)
            items = await self._resolve_query(str(body.get('query', '')), body.get('source', 'auto'),
                                              max(1, min(_num(body.get('limit', 30), 'limit'), 100)))
            return await self._enqueue(guild, items, announce=bool(body.get('announce')),
                                       position=body.get('position', 'end'))
        if action == 'radio':
            name = body.get('station')
            if name not in PRESET_RADIOS:
                raise ApiError(400, 'Неизвестная станция')
            await self._prepare_voice(guild, body)
            item = await _get_radio_stream(PRESET_RADIOS[name], f"📻 {name.replace('_', ' ').title()}")
            return await self._enqueue(guild, [item], announce=bool(body.get('announce')),
                                       position=body.get('position', 'end'))
        if action == 'playlist':
            pl = playlists.get(str(body.get('playlist_id')))
            if not pl or not pl['tracks']:
                raise ApiError(404, 'Плейлист не найден или пуст')
            await self._prepare_voice(guild, body)
            items = [dict(t) for t in pl['tracks']]
            if body.get('shuffle'):
                random.shuffle(items)
            return await self._enqueue(guild, items, announce=bool(body.get('announce')),
                                       position=body.get('position', 'end'))

        vc = self._need_vc(guild)
        if action == 'pause':
            if not vc.is_playing():
                raise ApiError(409, 'Ничего не играет')
            vc.pause()
        elif action == 'resume':
            if not vc.is_paused():
                raise ApiError(409, 'Музыка не на паузе')
            vc.resume()
        elif action == 'skip':
            if not _is_active(vc):
                raise ApiError(409, 'Ничего не играет')
            st.force_next = True
            vc.stop()
        elif action == 'skipto':
            pos = _num(body.get('position'), 'position')
            queue = qm.get_queue(gid)
            if not 1 <= pos <= len(queue):
                raise ApiError(400, f'В очереди {len(queue)} треков')
            skipped = queue[:pos - 1]
            del queue[:pos - 1]
            if st.loop == 'queue':
                queue.extend(skipped)
            st.force_next = True
            if _is_active(vc):
                vc.stop()
            else:
                await music._play_next(gid, notify=False)
        elif action in ('stop', 'leave'):
            self.leave_after.discard(gid)
            await music._full_stop(gid)
        elif action == 'volume':
            pct = max(0, min(_num(body.get('percent'), 'percent'), 200))
            st.volume = pct / 100
            apply_volume(vc, st.volume)
        elif action == 'loop':
            mode = body.get('mode')
            if mode not in LOOP_MODES:
                raise ApiError(400, 'mode: off | track | queue')
            st.loop = mode
        elif action == 'shuffle':
            qm.shuffle(gid)
        elif action == 'clear':
            qm.clear(gid)
        elif action == 'remove':
            if qm.remove(gid, _num(body.get('index'), 'index')) is None:
                raise ApiError(400, 'Нет такого трека')
        elif action == 'move':
            if qm.move(gid, _num(body.get('src'), 'src'), _num(body.get('dst'), 'dst')) is None:
                raise ApiError(400, 'Нет такого трека')
        else:
            raise ApiError(404, f'Неизвестное действие: {action}')
        return {}

    async def h_say(self, request):
        guild = self._guild(request.match_info['gid'])
        body = await self._body(request)
        ch = self._text_channel(guild, body)
        content = self._text(body)
        reference = None
        if body.get('reply_to'):
            reference = discord.MessageReference(message_id=_num(body['reply_to'], 'reply_to'), channel_id=ch.id,
                                                 fail_if_not_exists=False)
        msg = await ch.send(content, reference=reference, allowed_mentions=_mentions(request, body))
        self.emit('say', f'{request["who"]} написал в #{ch.name}: {content[:80]}', guild.id)
        return _json({'ok': True, 'message_id': msg.id})

    async def h_pl_list(self, request):
        return _json(playlists.list_all())

    async def h_pl_create(self, request):
        body = await self._body(request)
        return _json(playlists.create(str(body.get('name', ''))))

    async def h_pl_rename(self, request):
        body = await self._body(request)
        pl = playlists.rename(request.match_info['pid'], str(body.get('name', '')))
        if not pl:
            raise ApiError(404, 'Плейлист не найден')
        return _json(pl)

    async def h_pl_delete(self, request):
        if not playlists.delete(request.match_info['pid']):
            raise ApiError(404, 'Плейлист не найден')
        return _json({'ok': True})

    async def h_pl_add(self, request):
        body = await self._body(request)
        q = str(body.get('query', '')).strip()
        if not q:
            raise ApiError(400, 'Пустой запрос')
        if q.lower().startswith('ya:'):
            tracks = [await search_yandex(q[3:].strip())]
        else:
            kind = get_source_type(q)
            if kind == 'youtube_search':
                found = await search_youtube(q, 1)
                if not found:
                    raise ApiError(404, 'Ничего не найдено')
                tracks = found
            elif kind in ('youtube_playlist', 'youtube_video'):
                res = await get_audio_source(q, limit=100)
                tracks = res['tracks'] if res['type'] == 'playlist' else [res]
            elif kind == 'radio_stream':
                tracks = [await _get_radio_stream(q, '📻 ' + q[:60])]
            else:
                raise ApiError(400, 'Поддерживаются YouTube-ссылки, поиск, ya:запрос и ссылки на радиопотоки')
        pl = playlists.add_tracks(request.match_info['pid'], tracks)
        if not pl:
            raise ApiError(404, 'Плейлист не найден')
        return _json({'playlist': pl, 'added': len(tracks)})

    async def h_pl_remove(self, request):
        pl = playlists.remove_track(request.match_info['pid'], _num(request.match_info['idx'], 'idx'))
        if not pl:
            raise ApiError(404, 'Не найдено')
        return _json(pl)

    async def h_sounds(self, request):
        return _json(sounds.list_all())

    @staticmethod
    async def _read_upload(request, limit: int):
        fields, data, filename = {}, None, ''
        async for part in await request.multipart():
            if part.name == 'file':
                filename = part.filename or ''
                buf = bytearray()
                while chunk := await part.read_chunk(65536):
                    buf += chunk
                    if len(buf) > limit:
                        raise ApiError(413, f'Файл больше {limit // 1024 // 1024} МБ')
                data = bytes(buf)
            elif part.name:
                fields[part.name] = (await part.text())[:2000]
        return fields, data, filename

    async def h_upload(self, request):
        guild = self._guild(request.match_info['gid'])
        limit = min(guild.filesize_limit, UPLOAD_MAX_MB * 1024 * 1024)
        fields, data, filename = await self._read_upload(request, limit)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if not data:
            raise ApiError(400, 'Файл не передан')
        if ext not in UPLOAD_EXT:
            raise ApiError(400, 'Допустимые форматы: ' + ', '.join(sorted(UPLOAD_EXT)))
        ch = self._text_channel(guild, fields)
        safe_name = re.sub(r'[^\w.\-]+', '_', filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]) or f'file.{ext}'
        msg = await ch.send(content=fields.get('caption', '').strip() or None,
                            file=discord.File(io.BytesIO(data), filename=safe_name),
                            allowed_mentions=_mentions(request, {'allow_mentions': fields.get('allow_mentions') == '1'}))
        self.emit('say', f'{request["who"]} отправил файл {safe_name} в #{ch.name}', guild.id)
        return _json({'ok': True, 'message_id': msg.id})

    async def h_sound_upload(self, request):
        fields, data, filename = await self._read_upload(request, config.SOUND_MAX_MB * 1024 * 1024)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if not data:
            raise ApiError(400, 'Файл не передан')
        if ext not in sounds.ALLOWED_EXT:
            raise ApiError(400, 'Допустимые форматы: ' + ', '.join(sorted(sounds.ALLOWED_EXT)))
        name = fields.get('name') or filename.rsplit('.', 1)[0]
        sound = sounds.add(name, fields.get('emoji', ''), _num(fields.get('volume', 100), 'volume'), ext, data)
        duration = await self._probe_duration(sounds.file_path(sound))
        if duration is None:
            sounds.delete(sound['id'])
            raise ApiError(400, 'Не удалось прочитать аудио: файл повреждён или это не звук')
        sound = sounds.update(sound['id'], duration=duration)
        self.emit('admin', 'Загружен звук «' + sound['name'] + '»', None)
        return _json(sound)

    @staticmethod
    async def _probe_duration(path: str):
        proc = await asyncio.create_subprocess_exec(
            config.FFMPEG_PATH, '-v', 'info', '-i', path, '-f', 'null', '-',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        if proc.returncode != 0:
            return None
        match = re.search(r'Duration: (\d+):(\d+):([\d.]+)', err.decode(errors='replace'))
        if not match:
            return 0
        h, m, sec = match.groups()
        return round(int(h) * 3600 + int(m) * 60 + float(sec), 1)

    async def h_sound_edit(self, request):
        body = await self._body(request)
        sound = sounds.update(request.match_info['sid'], name=body.get('name'), emoji=body.get('emoji'),
                              volume=body.get('volume'))
        if not sound:
            raise ApiError(404, 'Звук не найден')
        return _json(sound)

    async def h_sound_delete(self, request):
        if not sounds.delete(request.match_info['sid']):
            raise ApiError(404, 'Звук не найден')
        return _json({'ok': True})

    async def h_sound_file(self, request):
        sound = sounds.get(request.match_info['sid'])
        if not sound or not os.path.isfile(sounds.file_path(sound)):
            raise ApiError(404, 'Звук не найден')
        return web.FileResponse(sounds.file_path(sound))

    async def h_sound_play(self, request):
        guild = self._guild(request.match_info['gid'])
        body = await self._body(request)
        if request.match_info['action'] == 'stop':
            vc = guild.voice_client
            if vc and isinstance(vc.source, MixSource):
                vc.source.stop_overlays()
            return _json({'ok': True, 'player': self.player_state(guild)})
        sound = await self.play_sound(guild, body)
        self.emit('sound', request['who'] + ' запустил звук «' + sound['name'] + '»', guild.id)
        return _json({'ok': True, 'player': self.player_state(guild)})

    async def play_sound(self, guild, body) -> dict:
        sound = sounds.get(str(body.get('sound_id')))
        path = sounds.file_path(sound) if sound else None
        if not sound or not os.path.isfile(path):
            raise ApiError(404, 'Звук не найден')
        await self._prepare_voice(guild, body)
        vc = guild.voice_client
        if vc.is_paused():
            raise ApiError(409, 'Музыка на паузе: снимите с паузы')
        volume = max(0, min(_num(body.get('volume', sound['volume']), 'volume'), 200)) / 100
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(path, executable=config.FFMPEG_PATH, options='-vn'), volume=volume)
        loop = asyncio.get_running_loop()
        current = vc.source if vc.is_playing() else None
        sounds_only = current is None or (isinstance(current, MixSource) and current.base is None)
        if sounds_only and body.get('leave_after'):
            self.leave_after.add(guild.id)
        elif sounds_only:
            self.leave_after.discard(guild.id)
        try:
            if isinstance(current, MixSource):
                current.add(source)
            elif current is not None:
                mixer = MixSource(current)
                mixer.on_idle = lambda: loop.call_soon_threadsafe(self._unwrap, guild.id, mixer)
                mixer.add(source)
                vc.source = mixer
            else:
                mixer = MixSource()
                mixer.add(source)
                vc.play(mixer, after=lambda err: loop.call_soon_threadsafe(
                    self._background, self._after_sound(guild.id)))
        except Exception:
            source.cleanup()
            raise
        return sound

    def _unwrap(self, guild_id: int, mixer: MixSource):
        mixer._notified = False
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if vc and vc.source is mixer and not mixer.overlays and not mixer.base_done:
            mixer.detached = True
            vc.source = mixer.base

    async def _after_sound(self, guild_id: int):
        music = self.bot.get_cog('MusicCog')
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not music or not vc or _is_active(vc):
            return
        if guild_id in self.leave_after and music.queue_manager.is_empty(guild_id):
            self.leave_after.discard(guild_id)
            await vc.disconnect()
        elif music.queue_manager.is_empty(guild_id):
            music._schedule_idle_disconnect(guild_id)
        else:
            await music._play_next(guild_id, notify=False)

    def _text_channel(self, guild, body):
        ch = guild.get_channel(_num(body.get('channel_id'), 'channel_id'))
        if not isinstance(ch, CHAT_CHANNELS):
            raise ApiError(400, 'Это не текстовый канал')
        if not ch.permissions_for(guild.me).send_messages:
            raise ApiError(403, 'У бота нет права писать в этот канал')
        return ch

    @staticmethod
    def _text(body, key='content', limit=2000) -> str:
        text = str(body.get(key, '')).strip()
        if not text or len(text) > limit:
            raise ApiError(400, f'Текст должен быть от 1 до {limit} символов')
        return text

    def _background(self, coro, *, quiet=False):
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t):
            self._tasks.discard(t)
            if not quiet and not t.cancelled() and t.exception():
                logs.record_error('api background task', t.exception())

        task.add_done_callback(_done)
        return task

    async def h_chat(self, request):
        guild = self._guild(request.match_info['gid'])
        action = request.match_info['action']
        body = await self._body(request)
        ch = self._text_channel(guild, body)
        mentions = _mentions(request, body)
        result = {}

        if action == 'typing':
            seconds = max(1, min(_num(body.get('seconds', 5), 'seconds'), 30))

            async def _typing():
                async with ch.typing():
                    await asyncio.sleep(seconds)

            self._background(_typing())
        elif action == 'embed':
            color = str(body.get('color', '#6c8cff')).lstrip('#')
            try:
                colour = discord.Colour(int(color, 16))
            except ValueError:
                raise ApiError(400, 'Цвет должен быть в формате #rrggbb')
            embed = discord.Embed(title=str(body.get('title', ''))[:256] or None,
                                  description=self._text(body, 'text', 4000), colour=colour)
            image = str(body.get('image', '')).strip()
            if image:
                if not image.startswith(('http://', 'https://')):
                    raise ApiError(400, 'Ссылка на картинку должна начинаться с http(s)://')
                embed.set_image(url=image)
            msg = await ch.send(embed=embed)
            result['message_id'] = msg.id
        elif action == 'poll':
            options = [str(o).strip()[:55] for o in body.get('options', []) if str(o).strip()]
            if not 2 <= len(options) <= 10:
                raise ApiError(400, 'Нужно от 2 до 10 вариантов ответа')
            if not hasattr(discord, 'Poll'):
                raise ApiError(501, 'Нужен discord.py 2.4 или новее')
            poll = discord.Poll(question=self._text(body, 'question', 300),
                                duration=datetime.timedelta(hours=max(1, min(_num(body.get('hours', 24), 'hours'), 168))),
                                multiple=bool(body.get('multiple')))
            for option in options:
                poll.add_answer(text=option)
            msg = await ch.send(poll=poll)
            result['message_id'] = msg.id
        elif action in ('react', 'edit', 'delete'):
            msg = await ch.fetch_message(_num(body.get('message_id'), 'message_id'))
            if action == 'react':
                try:
                    await msg.add_reaction(str(body.get('emoji', '')).strip())
                except discord.HTTPException:
                    raise ApiError(400, 'Неизвестный эмодзи')
            elif action == 'edit':
                if msg.author.id != self.bot.user.id:
                    raise ApiError(403, 'Редактировать можно только сообщения бота')
                await msg.edit(content=self._text(body), allowed_mentions=mentions)
            else:
                await msg.delete()
        elif action == 'typewriter':
            text = self._text(body, limit=600)
            try:
                delay = max(0.5, min(float(body.get('delay', 0.9)), 3))
            except (TypeError, ValueError):
                raise ApiError(400, 'Некорректное значение: delay')

            async def _typewriter():
                words = text.split()
                step = max(1, math.ceil(len(words) / 40))  # не больше ~40 правок, чтобы не упереться в лимиты Discord
                msg = await ch.send(' '.join(words[:step]), allowed_mentions=mentions)
                for i in range(2 * step, len(words) + step, step):
                    await asyncio.sleep(delay)
                    await msg.edit(content=' '.join(words[:i]), allowed_mentions=mentions)

            self._background(_typewriter())
        elif action == 'schedule':
            text = self._text(body)
            delay = max(5, min(_num(body.get('delay'), 'delay'), 3600))
            sid = uuid.uuid4().hex[:6]

            async def _scheduled():
                try:
                    await asyncio.sleep(delay)
                    await ch.send(text, allowed_mentions=mentions)
                    self.emit('say', f'Отложенное сообщение отправлено в #{ch.name}: {text[:80]}', guild.id)
                finally:
                    self.scheduled.pop(sid, None)

            task = self._background(_scheduled())
            self.scheduled[sid] = {'id': sid, 'guild_id': guild.id, 'channel': ch.name, 'content': text[:200],
                                   'at': time.time() + delay, 'task': task}
            result['id'] = sid
        elif action in ('pin', 'unpin'):
            msg = await ch.fetch_message(_num(body.get('message_id'), 'message_id'))
            await (msg.pin() if action == 'pin' else msg.unpin())
        elif action in ('purge', 'slowmode'):
            if request['role'] != 'admin':
                raise ApiError(403, 'Доступно только полному доступу')
            if action == 'purge':
                only_bot = bool(body.get('only_bot', True))
                bot_id = self.bot.user.id
                deleted = await ch.purge(limit=max(1, min(_num(body.get('limit', 10), 'limit'), 100)),
                                         check=(lambda m: m.author.id == bot_id) if only_bot else (lambda m: True))
                result['deleted'] = len(deleted)
            else:
                await ch.edit(slowmode_delay=max(0, min(_num(body.get('seconds', 0), 'seconds'), 21600)))
        else:
            raise ApiError(404, f'Неизвестное действие: {action}')

        self.emit('say', f'{request["who"]}: {action} в #{ch.name}', guild.id)
        return _json({'ok': True, **result})

    async def h_nick(self, request):
        if request['role'] != 'admin':
            raise ApiError(403, 'Доступно только полному доступу')
        guild = self._guild(request.match_info['gid'])
        body = await self._body(request)
        await guild.me.edit(nick=str(body.get('nick', '')).strip()[:32] or None)
        self.emit('admin', f'{request["who"]}: ник бота на сервере {guild.name} → {guild.me.nick or "по умолчанию"}', guild.id)
        return _json({'ok': True, 'nick': guild.me.nick})

    async def h_queue_save(self, request):
        guild = self._guild(request.match_info['gid'])
        body = await self._body(request)
        music = self.music
        st = music.state(guild.id)
        tracks = ([st.current] if st.current else []) + list(music.queue_manager.get_queue(guild.id))
        if not tracks:
            raise ApiError(409, 'Очередь пуста')
        pl = playlists.get(str(body.get('playlist_id'))) if body.get('playlist_id') else playlists.create(
            str(body.get('name') or 'Из очереди ' + time.strftime('%d.%m %H:%M')))
        if not pl:
            raise ApiError(404, 'Плейлист не найден')
        playlists.add_tracks(pl['id'], tracks)
        return _json({'ok': True, 'playlist': pl['name'], 'added': len(tracks)})

    async def h_scheduled(self, request):
        return _json([{k: v for k, v in s.items() if k != 'task'} for s in self.scheduled.values()])

    async def h_scheduled_cancel(self, request):
        item = self.scheduled.pop(request.match_info['sid'], None)
        if not item:
            raise ApiError(404, 'Не найдено')
        item['task'].cancel()
        return _json({'ok': True})

    async def h_admin(self, request):
        action = request.match_info['action']
        body = await self._body(request)
        bot = self.bot
        result = {}

        if action == 'presence':
            text = str(body.get('text', '')).strip()
            act = None
            if text:
                kind = ACTIVITY_TYPES.get(body.get('type', 'playing'), discord.ActivityType.playing)
                act = discord.Activity(type=kind, name=text[:128])
            await bot.change_presence(activity=act, status=STATUSES.get(body.get('status', 'online')))
        elif action == 'sync':
            synced = await bot.tree.sync()
            result['message'] = f'Синхронизировано глобальных команд: {len(synced)}'
        elif action == 'reload':
            result['message'] = await self._reload(str(body.get('name', 'all')))
        elif action == 'leave_all':
            n = 0
            for v in list(bot.voice_clients):
                await self.music._full_stop(v.guild.id)
                n += 1
            result['message'] = f'Отключено подключений: {n}'
        elif action == 'reset':
            gid = self._guild(body.get('guild_id')).id
            music = self.music
            await music._full_stop(gid)
            st = music.state(gid)
            for t in (st.idle_task, st.alone_task):
                if t:
                    t.cancel()
            music.states.pop(gid, None)
            result['message'] = 'Плеер сервера сброшен'
        elif action == 'clear_errors':
            logs.ERRORS.clear()
        elif action == 'ffmpeg':
            try:
                proc = await asyncio.create_subprocess_exec(
                    config.FFMPEG_PATH, '-version', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), 10)
                first = (out.decode(errors='replace').splitlines() or ['(пустой вывод)'])[0]
                result['message'] = f'✅ {config.FFMPEG_PATH}\n{first}'
            except (OSError, asyncio.TimeoutError) as e:
                result['message'] = f'❌ ffmpeg не запускается ({config.FFMPEG_PATH}): {e}'
        elif action == 'cookies':
            path = config.COOKIES_FILE
            if not os.path.isfile(path):
                result['message'] = f'Файл не найден: {path}'
            else:
                age = datetime.timedelta(seconds=int(time.time() - os.path.getmtime(path)))
                result['message'] = f'{path}: обновлялся {age} назад'
        elif action == 'radiocheck':
            lines = []
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                for name, url in PRESET_RADIOS.items():
                    try:
                        async with s.get(url, headers={'Icy-MetaData': '0'}) as r:
                            lines.append(f"{'✅' if r.status < 400 else '❌'} {name}: HTTP {r.status}")
                    except Exception as e:
                        lines.append(f'❌ {name}: {type(e).__name__}')
            result['message'] = '\n'.join(lines)
        elif action == 'test':
            query = self._text(body, 'query', 500)
            t0 = time.monotonic()
            res = await get_audio_source(query, limit=3)
            title = res['title'] + f" ({len(res['tracks'])} треков)" if res['type'] == 'playlist' else res['title']
            result['message'] = f'OK за {time.monotonic() - t0:.1f}с: {title}'
        elif action == 'update_ytdlp':
            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            result['message'] = out.decode(errors='replace')[-1500:] + (
                '\nГотово. Перезапустите бота.' if proc.returncode == 0 else '\npip завершился с ошибкой')
        elif action in ('restart', 'shutdown'):
            asyncio.get_running_loop().call_later(0.5, self._background, self._exit(action))
            result['message'] = 'Перезапускаюсь…' if action == 'restart' else 'Выключаюсь…'
        else:
            raise ApiError(404, f'Неизвестное действие: {action}')

        self.emit('admin', f'{request["who"]}: {action}', None)
        return _json({'ok': True, **result})

    async def _reload(self, name: str) -> str:
        import importlib
        for mod in ('utils.sources', 'utils.playlists', 'utils.sounds'):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
        # перезагрузка api_cog из его же HTTP-запроса оборвала бы ответ
        if name == 'all':
            names = [e for e in self.bot.extensions if e != __name__]
        else:
            ext = name if name.startswith('cogs.') else f"cogs.{name.replace('_cog', '')}_cog"
            if ext == __name__:
                raise ApiError(400, 'api_cog перезагружается только перезапуском бота')
            names = [ext]
        results = []
        for ext in names:
            try:
                if ext in self.bot.extensions:
                    await self.bot.reload_extension(ext)
                else:
                    await self.bot.load_extension(ext)
                results.append(f'✅ {ext}')
            except Exception as e:
                logs.record_error(f'reload {ext}', e)
                results.append(f'❌ {ext}: {type(e).__name__}: {e}')
        return '\n'.join(results)

    async def _exit(self, action):
        import subprocess
        for v in list(self.bot.voice_clients):
            await v.disconnect(force=True)
        await self.bot.close()
        if action == 'shutdown':
            return
        if os.name == 'nt':
            subprocess.Popen([sys.executable] + sys.argv, cwd=config.BASE_DIR)
            os._exit(0)
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def setup(bot):
    if any(len(t) < 16 for t in (*config.ADMIN_TOKENS, *config.GUEST_TOKENS)):
        raise ValueError('Токены админ-API должны быть не короче 16 символов')
    await bot.add_cog(ApiCog(bot))
