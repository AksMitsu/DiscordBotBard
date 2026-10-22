"""Команды для владельца бота: отладка и ремонт. Работают только у владельца (префиксные, например !status)."""
import asyncio
import contextlib
import datetime
import importlib
import io
import os
import platform
import subprocess
import sys
import textwrap
import time
import traceback

import aiohttp
import discord
from discord.ext import commands

import config
from utils import logs

MUSIC_EXT = 'cogs.music_cog'


def _strip_code(code: str) -> str:
    code = code.strip()
    if code.startswith('```'):
        lines = code.split('\n')
        lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        return '\n'.join(lines)
    return code.strip('`')


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        return await self.bot.is_owner(ctx.author)

    # ---------- утилиты ----------
    @property
    def music(self):
        return self.bot.get_cog('MusicCog')

    async def _send(self, ctx, text: str, lang: str = ''):
        """Отправляет текст; если он длинный — файлом."""
        text = str(text) or '(пусто)'
        if len(text) <= 1900:
            await ctx.send(f"```{lang}\n{text}\n```")
        else:
            await ctx.send("📎 Вывод слишком длинный:", file=discord.File(io.BytesIO(text.encode('utf-8')), 'output.txt'))

    def _guild_id(self, ctx, guild_id):
        return guild_id or (ctx.guild.id if ctx.guild else None)

    # ---------- справка ----------
    @commands.command(name='ahelp')
    async def ahelp(self, ctx):
        """Список админ-команд"""
        await self._send(ctx, textwrap.dedent(f"""
            Диагностика:
              {ctx.prefix}status                — состояние бота
              {ctx.prefix}guilds                — серверы бота
              {ctx.prefix}voice [guild_id]      — голос/очередь/состояние плеера
              {ctx.prefix}test <запрос>         — проверить получение трека (yt-dlp)
              {ctx.prefix}radiocheck            — проверить доступность радиостанций
              {ctx.prefix}ffmpeg                — проверить ffmpeg
              {ctx.prefix}cookies               — проверить файл cookies
              {ctx.prefix}errors [n]            — последние ошибки
              {ctx.prefix}trace <id>            — полный traceback ошибки
              {ctx.prefix}clear_errors          — очистить список ошибок
              {ctx.prefix}logs [n]              — последние строки bot.log
            Ремонт плеера:
              {ctx.prefix}reset [guild_id]      — полный сброс плеера сервера
              {ctx.prefix}fskip [guild_id]      — принудительно пропустить трек
              {ctx.prefix}force_play [guild_id] — принудительно запустить очередь
              {ctx.prefix}leave_all             — выйти из всех голосовых каналов
              {ctx.prefix}clear_queue [guild_id]
            Управление ботом:
              {ctx.prefix}reload [ext|all]      — перезагрузить модуль (music, admin)
              {ctx.prefix}load / unload <ext>
              {ctx.prefix}extensions
              {ctx.prefix}sync [guild|global|clear]  — слэш-команды
              {ctx.prefix}update_ytdlp          — обновить yt-dlp (потом !restart)
              {ctx.prefix}restart / shutdown
              {ctx.prefix}presence <текст>      — статус бота
              {ctx.prefix}eval <код>            — выполнить Python-код
        """).strip())

    # ---------- диагностика ----------
    @commands.command(name='status')
    async def status(self, ctx):
        """Общее состояние бота"""
        uptime = datetime.timedelta(seconds=int(time.time() - self.bot.started_at))
        voice_clients = self.bot.voice_clients
        playing = sum(1 for v in voice_clients if v.is_playing())
        try:
            import yt_dlp
            ytdlp_ver = yt_dlp.version.__version__
        except Exception:
            ytdlp_ver = '?'
        embed = discord.Embed(title="🛠 Статус бота", color=discord.Color.orange())
        embed.add_field(name="Аптайм", value=str(uptime))
        embed.add_field(name="Пинг", value=f"{round(self.bot.latency * 1000)} мс")
        embed.add_field(name="Серверов", value=str(len(self.bot.guilds)))
        embed.add_field(name="Голосовых подключений", value=f"{len(voice_clients)} (играют: {playing})")
        embed.add_field(name="Ошибок в буфере", value=str(len(logs.ERRORS)))
        embed.add_field(name="Расширения", value=", ".join(self.bot.extensions) or "—", inline=False)
        embed.add_field(name="Версии", inline=False, value=(
            f"Python {platform.python_version()} · discord.py {discord.__version__} · yt-dlp {ytdlp_ver}"))
        try:
            import psutil
            proc = psutil.Process()
            embed.add_field(name="Память", value=f"{proc.memory_info().rss / 1024 ** 2:.0f} МБ")
        except ImportError:
            pass
        await ctx.send(embed=embed)

    @commands.command(name='guilds')
    async def guilds(self, ctx):
        """Список серверов"""
        lines = [f"{g.id}  {g.name}  ({g.member_count} уч.)" for g in self.bot.guilds]
        await self._send(ctx, "\n".join(lines))

    @commands.command(name='voice')
    async def voice(self, ctx, guild_id: int = None):
        """Состояние плеера: без аргумента — все серверы с подключением"""
        music = self.music
        if not music:
            return await ctx.send("❌ MusicCog не загружен")
        if guild_id is None and ctx.guild and not self.bot.voice_clients:
            guild_id = ctx.guild.id
        ids = [guild_id] if guild_id else [v.guild.id for v in self.bot.voice_clients]
        if not ids:
            return await ctx.send("ℹ️ Нет активных голосовых подключений")
        out = []
        for gid in ids:
            guild = self.bot.get_guild(gid)
            v = guild.voice_client if guild else None
            st = music.state(gid)
            queue = music.queue_manager.get_queue(gid)
            out.append(
                f"== {guild.name if guild else gid} ({gid}) ==\n"
                f"voice: {'нет' if not v else f'channel={v.channel.name if v.channel else None} connected={v.is_connected()} playing={v.is_playing()} paused={v.is_paused()} latency={v.latency * 1000:.0f}ms'}\n"
                f"current: {st.current.get('title') if st.current else None}\n"
                f"loop={st.loop} volume={st.volume} lock_locked={st.lock.locked()} "
                f"text_ch={st.text_channel_id} voice_ch={st.voice_channel_id}\n"
                f"queue ({len(queue)}): " + "; ".join(i.get('title', '?')[:40] for i in queue[:10])
                + (" ..." if len(queue) > 10 else ""))
        await self._send(ctx, "\n\n".join(out))

    @commands.command(name='test')
    async def test(self, ctx, *, query: str):
        """Проверить получение трека и построить ffmpeg-источник"""
        from utils import sources
        async with ctx.typing():
            t0 = time.monotonic()
            try:
                result = await sources.get_audio_source(query, limit=3)
                took = time.monotonic() - t0
                if result['type'] == 'playlist':
                    info = f"playlist '{result['title']}', треков: {len(result['tracks'])}"
                else:
                    item = result
                    src = sources.create_source(item)
                    src.cleanup()
                    url = item.get('stream_url') or item.get('url') or ''
                    info = (f"track '{item['title']}'\nduration={item.get('duration')} "
                            f"url={url[:80]}...\nffmpeg запускается ✅")
                await self._send(ctx, f"OK за {took:.1f}с\n{info}")
            except Exception as e:
                await self._send(ctx, f"FAIL: {e}\n\n{traceback.format_exc()}", 'py')

    @commands.command(name='radiocheck')
    async def radiocheck(self, ctx):
        """Проверить, отвечают ли предустановленные радиостанции"""
        from cogs.music_cog import PRESET_RADIOS
        results = []
        timeout = aiohttp.ClientTimeout(total=10)
        async with ctx.typing():
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for name, url in PRESET_RADIOS.items():
                    try:
                        async with session.get(url, headers={'Icy-MetaData': '0'}) as r:
                            results.append(f"{'✅' if r.status < 400 else '❌'} {name}: HTTP {r.status} {r.content_type}")
                    except Exception as e:
                        results.append(f"❌ {name}: {type(e).__name__}: {e}")
        await self._send(ctx, "\n".join(results))

    @commands.command(name='ffmpeg')
    async def ffmpeg(self, ctx):
        """Проверить ffmpeg"""
        try:
            proc = await asyncio.create_subprocess_exec(
                config.FFMPEG_PATH, '-version',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            first = out.decode(errors='replace').splitlines()[0]
            await self._send(ctx, f"path: {config.FFMPEG_PATH}\n{first}")
        except Exception as e:
            await self._send(ctx, f"❌ ffmpeg не запускается ({config.FFMPEG_PATH}): {e}")

    @commands.command(name='cookies')
    async def cookies(self, ctx):
        """Проверить файл cookies для YouTube"""
        path = config.COOKIES_FILE
        if not os.path.isfile(path):
            return await ctx.send(f"❌ Файл не найден: `{path}` (yt-dlp работает без cookies, но YouTube может блокировать)")
        age = datetime.timedelta(seconds=int(time.time() - os.path.getmtime(path)))
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = [l for l in f if l.strip() and not l.startswith('#')]
        await ctx.send(f"✅ `{path}`: {len(lines)} записей, файл обновлялся {age} назад")

    @commands.command(name='errors')
    async def errors(self, ctx, n: int = 10):
        """Последние ошибки (краткий список)"""
        if not logs.ERRORS:
            return await ctx.send("✅ Ошибок нет")
        lines = []
        for e in list(logs.ERRORS)[-n:]:
            when = datetime.datetime.fromtimestamp(e['time']).strftime('%H:%M:%S')
            last = e['trace'].strip().splitlines()[-1][:120]
            lines.append(f"#{e['id']} [{when}] {e['context']}\n    {last}")
        await self._send(ctx, "\n".join(lines))

    @commands.command(name='trace')
    async def trace(self, ctx, error_id: int):
        """Полный traceback ошибки по номеру"""
        for e in logs.ERRORS:
            if e['id'] == error_id:
                return await self._send(ctx, f"{e['context']}\n\n{e['trace']}", 'py')
        await ctx.send("❌ Ошибка с таким номером не найдена")

    @commands.command(name='clear_errors')
    async def clear_errors(self, ctx):
        logs.ERRORS.clear()
        await ctx.send("🗑️ Список ошибок очищен")

    @commands.command(name='logs')
    async def logs_cmd(self, ctx, n: int = 40):
        """Последние строки bot.log"""
        try:
            with open(config.LOG_FILE, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()[-n:]
        except FileNotFoundError:
            return await ctx.send("❌ Лог-файл ещё не создан")
        await self._send(ctx, "".join(lines))

    # ---------- ремонт плеера ----------
    @commands.command(name='reset')
    async def reset(self, ctx, guild_id: int = None):
        """Полный сброс плеера сервера: остановка, очистка, отключение, сброс состояния"""
        gid = self._guild_id(ctx, guild_id)
        music = self.music
        if not music or not gid:
            return await ctx.send("❌ Нет MusicCog или сервера")
        await music._full_stop(gid)
        for task in (music.state(gid).idle_task, music.state(gid).alone_task):
            if task:
                task.cancel()
        music.states.pop(gid, None)
        guild = self.bot.get_guild(gid)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect(force=True)
        await ctx.send(f"♻️ Плеер сервера `{gid}` полностью сброшен")

    @commands.command(name='fskip')
    async def fskip(self, ctx, guild_id: int = None):
        """Принудительно пропустить трек"""
        gid = self._guild_id(ctx, guild_id)
        guild = self.bot.get_guild(gid) if gid else None
        v = guild.voice_client if guild else None
        if not v:
            return await ctx.send("❌ Нет голосового подключения")
        self.music.state(gid).force_next = True
        v.stop()
        await ctx.send("⏭️ Пропущено")

    @commands.command(name='force_play')
    async def force_play(self, ctx, guild_id: int = None):
        """Принудительно запустить следующий трек (если плеер завис)"""
        gid = self._guild_id(ctx, guild_id)
        guild = self.bot.get_guild(gid) if gid else None
        if guild and guild.voice_client:
            guild.voice_client.stop()
        st = self.music.state(gid)
        if st.lock.locked():
            return await ctx.send("⚠️ Блокировка плеера занята (идёт загрузка трека). Если висит долго — `!reset`")
        started = await self.music._play_next(gid)
        await ctx.send(f"▶️ {started['title']}" if started else "📭 Запускать нечего")

    @commands.command(name='leave_all')
    async def leave_all(self, ctx):
        """Выйти из всех голосовых каналов"""
        count = 0
        for v in list(self.bot.voice_clients):
            await self.music._full_stop(v.guild.id)
            count += 1
        await ctx.send(f"👋 Отключено подключений: {count}")

    @commands.command(name='clear_queue')
    async def clear_queue(self, ctx, guild_id: int = None):
        gid = self._guild_id(ctx, guild_id)
        self.music.queue_manager.clear(gid)
        await ctx.send("🗑️ Очередь очищена")

    # ---------- управление ботом ----------
    def _ext_name(self, name: str) -> str:
        return name if name.startswith('cogs.') else f"cogs.{name.replace('_cog', '')}_cog"

    @commands.command(name='extensions')
    async def extensions(self, ctx):
        await ctx.send("Загружены: " + (", ".join(f"`{e}`" for e in self.bot.extensions) or "—"))

    @commands.command(name='reload')
    async def reload(self, ctx, name: str = 'all'):
        """Перезагрузить расширение (music/admin) или все; подгружает свежий код utils"""
        for mod in ('utils.sources', 'utils.playlists', 'utils.sounds'):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
        names = list(self.bot.extensions) if name == 'all' else [self._ext_name(name)]
        results = []
        for ext in names:
            if ext == __name__ and name == 'all':
                continue  # себя перезагружаем только явно
            try:
                if ext in self.bot.extensions:
                    await self.bot.reload_extension(ext)
                else:
                    await self.bot.load_extension(ext)
                results.append(f"✅ {ext}")
            except Exception as e:
                logs.record_error(f"reload {ext}", e)
                results.append(f"❌ {ext}: {type(e).__name__}: {e}")
        await self._send(ctx, "\n".join(results))

    @commands.command(name='load')
    async def load(self, ctx, name: str):
        try:
            await self.bot.load_extension(self._ext_name(name))
            await ctx.send(f"✅ Загружено `{self._ext_name(name)}`")
        except Exception as e:
            await self._send(ctx, f"{type(e).__name__}: {e}")

    @commands.command(name='unload')
    async def unload(self, ctx, name: str):
        if self._ext_name(name) == __name__:
            return await ctx.send("❌ Нельзя выгрузить админ-модуль")
        try:
            await self.bot.unload_extension(self._ext_name(name))
            await ctx.send(f"✅ Выгружено `{self._ext_name(name)}`")
        except Exception as e:
            await self._send(ctx, f"{type(e).__name__}: {e}")

    @commands.command(name='sync')
    async def sync(self, ctx, mode: str = 'global'):
        """sync global — на все серверы; sync guild — мгновенно на этот сервер; sync clear — удалить команды на этом сервере"""
        try:
            if mode == 'guild':
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(f"✅ На сервер синхронизировано команд: {len(synced)}")
            elif mode == 'clear':
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send("✅ Серверные команды удалены (глобальные не затронуты)")
            else:
                synced = await self.bot.tree.sync()
                await ctx.send(f"✅ Глобально синхронизировано команд: {len(synced)}: "
                               + ", ".join(c.name for c in synced))
        except Exception as e:
            logs.record_error("sync", e)
            await ctx.send(f"❌ {type(e).__name__}: {e}")

    @commands.command(name='update_ytdlp')
    async def update_ytdlp(self, ctx):
        """pip install -U yt-dlp (после этого нужен !restart)"""
        async with ctx.typing():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
        text = out.decode(errors='replace')
        await self._send(ctx, text[-1500:])
        await ctx.send("✅ Готово. Выполните `!restart`, чтобы применить." if proc.returncode == 0
                       else "❌ pip завершился с ошибкой")

    @commands.command(name='restart')
    async def restart(self, ctx):
        """Перезапустить процесс бота"""
        await ctx.send("🔄 Перезапускаюсь...")
        for v in list(self.bot.voice_clients):
            await v.disconnect(force=True)
        await self.bot.close()
        if os.name == 'nt':
            subprocess.Popen([sys.executable] + sys.argv, cwd=config.BASE_DIR)
            os._exit(0)
        os.execv(sys.executable, [sys.executable] + sys.argv)  # Linux: замена процесса (systemd/docker не заметят)

    @commands.command(name='shutdown')
    async def shutdown(self, ctx):
        await ctx.send("👋 Выключаюсь")
        await self.bot.close()

    @commands.command(name='presence')
    async def presence(self, ctx, *, text: str = None):
        await self.bot.change_presence(activity=discord.Game(text) if text else None)
        await ctx.send("✅ Статус обновлён")

    @commands.command(name='eval')
    async def eval_cmd(self, ctx, *, code: str):
        """Выполнить Python. Доступны: bot, ctx, discord, asyncio, music, qm. Поддерживается await."""
        code = _strip_code(code)
        env = {
            'bot': self.bot, 'ctx': ctx, 'discord': discord, 'asyncio': asyncio,
            'music': self.music, 'qm': self.bot.queue_manager, 'logs': logs,
        }
        stdout = io.StringIO()
        try:
            try:
                compiled = compile(f"async def __eval():\n    return ({code})", '<eval>', 'exec')
            except SyntaxError:
                compiled = compile("async def __eval():\n" + textwrap.indent(code, '    '), '<eval>', 'exec')
            exec(compiled, env)
            with contextlib.redirect_stdout(stdout):
                result = await env['__eval']()
        except Exception:
            return await self._send(ctx, traceback.format_exc(), 'py')
        output = stdout.getvalue() + (repr(result) if result is not None else '')
        await self._send(ctx, output or '✅ Выполнено (нет вывода)', 'py')


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
