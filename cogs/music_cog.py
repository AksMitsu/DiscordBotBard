import asyncio
import logging
import time
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import IDLE_DISCONNECT_SECONDS
from utils.queue_manager import QueueManager, GuildState
from utils.sources import (
    get_audio_source, search_yandex, search_youtube,
    _get_radio_stream, resolve_item, create_source, format_duration,
)

log = logging.getLogger('newbot.music')

PRESET_RADIOS = {
    'europa_plus': 'http://ep128.hostingradio.ru:8030/ep128',
    'retro_fm': 'http://hls-01-retro.emgsound.ru/12/128/playlist.m3u8',
    'record_super_90s': 'http://online.radiorecord.ru:8102/sd90_128',
    'russkoe_radio': 'https://rusradio.hostingradio.ru/rusradio128.mp3',
    'vesti_fm': 'http://icecast.vgtrk.cdnvideo.ru/vestifm_mp3_128kbps'
}

LOOP_MODES = {'off': '➡️ Выключен', 'track': '🔂 Повтор трека', 'queue': '🔁 Повтор очереди'}
QUICK_FAIL_SECONDS = 3
QUICK_FAIL_LIMIT = 5


def _radio_choices() -> List[app_commands.Choice[str]]:
    return [app_commands.Choice(name=name.replace('_', ' ').title(), value=name) for name in PRESET_RADIOS]


def _is_active(voice: Optional[discord.VoiceClient]) -> bool:
    return bool(voice and voice.is_connected() and (voice.is_playing() or voice.is_paused()))


def apply_volume(voice: Optional[discord.VoiceClient], volume: float):
    """Громкость текущего трека; учитывает обёртку саундборда (MixSource.base)."""
    source = voice.source if voice else None
    source = getattr(source, 'base', source)
    if isinstance(source, discord.PCMVolumeTransformer):
        source.volume = volume


class PaginationView(discord.ui.View):
    def __init__(self, cog, guild_id, current_page=1, items_per_page=10):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.current_page = current_page
        self.items_per_page = items_per_page
        self.message = None

    async def _render(self):
        embed, total_pages = await self.cog._get_queue_embed(self.guild_id, self.current_page, self.items_per_page)
        self.current_page = max(1, min(self.current_page, total_pages or 1))
        self.previous_button.disabled = self.current_page <= 1
        self.next_button.disabled = self.current_page >= total_pages
        return embed

    async def _refresh_message(self):
        if self.message:
            try:
                await self.message.edit(embed=await self._render(), view=self)
            except discord.HTTPException:
                pass

    def _can_control(self, interaction: discord.Interaction) -> bool:
        voice = interaction.guild.voice_client if interaction.guild else None
        member = interaction.user
        if voice and voice.channel and getattr(member, 'voice', None) and member.voice.channel == voice.channel:
            return True
        return interaction.permissions.manage_guild if interaction.permissions else False

    async def _deny(self, interaction):
        await interaction.response.send_message("❌ Нужно находиться в голосовом канале с ботом", ephemeral=True)

    def _page_slice(self, queue):
        start = (self.current_page - 1) * self.items_per_page
        return start, queue[start:start + self.items_per_page]

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(1, self.current_page - 1)
        await interaction.response.edit_message(embed=await self._render(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        await interaction.response.edit_message(embed=await self._render(), view=self)

    @discord.ui.button(label="⭐ Вперёд", style=discord.ButtonStyle.success, row=1)
    async def move_to_top_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_control(interaction):
            return await self._deny(interaction)
        queue = self.cog.queue_manager.get_queue(self.guild_id)
        start, page_items = self._page_slice(queue)
        if not page_items:
            return await interaction.response.send_message("📭 На этой странице пусто", ephemeral=True)
        await self._ask_track(interaction, "Выберите трек для перемещения в начало очереди:", start, page_items, 'top')

    @discord.ui.button(label="🗑️ Удалить", style=discord.ButtonStyle.danger, row=1)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_control(interaction):
            return await self._deny(interaction)
        queue = self.cog.queue_manager.get_queue(self.guild_id)
        start, page_items = self._page_slice(queue)
        if not page_items:
            return await interaction.response.send_message("📭 На этой странице пусто", ephemeral=True)
        await self._ask_track(interaction, "Выберите трек для удаления:", start, page_items, 'remove')

    async def _ask_track(self, interaction, text, start, page_items, action):
        select = discord.ui.Select(
            placeholder=text[:100],
            options=[
                discord.SelectOption(label=f"{start + i + 1}. {item.get('title', 'Неизвестно')}"[:100],
                                     value=str(start + i))
                for i, item in enumerate(page_items)
            ]
        )

        async def select_callback(select_interaction: discord.Interaction):
            index = int(select.values[0])
            qm = self.cog.queue_manager
            if action == 'top':
                if index == 0:
                    await select_interaction.response.send_message("ℹ️ Трек уже на первом месте", ephemeral=True)
                    return
                track = qm.move(self.guild_id, index, 0)
                text_ = f"⭐ **{track['title']}** перемещён на первое место" if track else "❌ Очередь изменилась"
            else:
                track = qm.remove(self.guild_id, index)
                text_ = f"🗑️ Удалён из очереди: **{track['title']}**" if track else "❌ Очередь изменилась"
            await select_interaction.response.send_message(text_, ephemeral=True)
            await self._refresh_message()

        select.callback = select_callback
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(text, view=view, ephemeral=True)

    @discord.ui.button(label="⏹️ Очистить", style=discord.ButtonStyle.secondary, row=1)
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_control(interaction):
            return await self._deny(interaction)
        self.cog.queue_manager.clear(self.guild_id)
        self.current_page = 1
        await interaction.response.edit_message(embed=await self._render(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SearchView(discord.ui.View):
    """Выбор трека из результатов поиска."""
    def __init__(self, cog, interaction, results):
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = interaction.user.id
        self.results = results
        select = discord.ui.Select(
            placeholder="Выберите трек",
            options=[
                discord.SelectOption(
                    label=f"{i + 1}. {r['title']}"[:100], value=str(i),
                    description=format_duration(r.get('duration')))
                for i, r in enumerate(results)
            ])
        select.callback = self._on_select
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Это меню не для вас", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        item = dict(self.results[int(self.select.values[0])])
        await interaction.response.edit_message(content=f"🔎 Выбрано: **{item['title']}**", view=None)
        self.stop()
        await self.cog._enqueue_and_start(interaction, [item], followup=True)


class MusicCog(commands.Cog):
    def __init__(self, bot, queue_manager: QueueManager):
        self.bot = bot
        self.queue_manager = queue_manager
        if not hasattr(bot, 'guild_states'):
            bot.guild_states = {}
        self.states = bot.guild_states  # переживает перезагрузку cog'а

    # ---------- Вспомогательное ----------
    def state(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildState()
        return self.states[guild_id]

    @property
    def current_track(self):
        """Совместимость: guild_id -> текущий трек."""
        return {gid: st.current for gid, st in self.states.items()}

    async def cog_unload(self):
        for st in self.states.values():
            for task in (st.idle_task, st.alone_task):
                if task:
                    task.cancel()

    async def _send_notification(self, guild_id, content=None, embed=None):
        st = self.state(guild_id)
        channel = self.bot.get_channel(st.text_channel_id) if st.text_channel_id else None
        if channel:
            try:
                await channel.send(content=content, embed=embed)
            except discord.HTTPException as e:
                log.warning("Не удалось отправить уведомление: %s", e)

    def _remember_context(self, interaction: discord.Interaction):
        st = self.state(interaction.guild.id)
        st.text_channel_id = interaction.channel.id
        user_voice = getattr(interaction.user, 'voice', None)
        if user_voice and user_voice.channel:
            st.voice_channel_id = user_voice.channel.id
        return st

    async def _check_user_voice(self, interaction: discord.Interaction) -> bool:
        """Для команд, которые запускают воспроизведение."""
        user_voice = getattr(interaction.user, 'voice', None)
        if not user_voice or not user_voice.channel:
            await self._reply(interaction, "❌ Сначала зайдите в голосовой канал")
            return False
        bot_voice = interaction.guild.voice_client
        if _is_active(bot_voice) and bot_voice.channel != user_voice.channel:
            await self._reply(interaction, f"❌ Бот уже играет в канале **{bot_voice.channel.name}**")
            return False
        return True

    async def _check_control(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        """Для команд управления: бот в канале, пользователь там же."""
        voice = interaction.guild.voice_client
        if not voice or not voice.is_connected():
            await self._reply(interaction, "❌ Бот не в голосовом канале")
            return None
        user_voice = getattr(interaction.user, 'voice', None)
        if not user_voice or user_voice.channel != voice.channel:
            if not (interaction.permissions and interaction.permissions.manage_guild):
                await self._reply(interaction, "❌ Вы должны быть в одном голосовом канале с ботом")
                return None
        return voice

    @staticmethod
    async def _reply(interaction: discord.Interaction, content=None, *, embed=None, ephemeral=False, view=None):
        kwargs = {'content': content, 'embed': embed}
        if view is not None:
            kwargs['view'] = view
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs, ephemeral=ephemeral)
        return await interaction.response.send_message(**kwargs, ephemeral=ephemeral)

    async def _get_queue_embed(self, guild_id, page: int = 1, items_per_page: int = 10):
        queue = self.queue_manager.get_queue(guild_id)
        st = self.state(guild_id)
        embed = discord.Embed(title="📋 Очередь воспроизведения", color=discord.Color.blue())
        current = st.current
        if current:
            embed.add_field(name="🎵 Сейчас играет",
                            value=f"**{current.get('title', 'Неизвестно')[:80]}**", inline=False)
        if not queue:
            embed.description = "📭 Очередь пуста"
            return embed, 0
        total = len(queue)
        total_pages = (total + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        start = (page - 1) * items_per_page
        total_dur = sum(i.get('duration') or 0 for i in queue)
        embed.description = f"Всего треков: {total} · Длительность: {format_duration(total_dur)}"
        lines = []
        for i, item in enumerate(queue[start:start + items_per_page], start=start + 1):
            icon = "📻" if item.get('type') == 'radio' else "🎵"
            lines.append(f"`{i}.` {icon} {item.get('title', 'Неизвестно')[:70]}")
        embed.add_field(name=f"📝 В очереди (страница {page}/{total_pages})",
                        value="\n".join(lines) or "Пусто", inline=False)
        embed.set_footer(text=f"Повтор: {LOOP_MODES[st.loop]} · Громкость: {int(st.volume * 100)}%")
        return embed, total_pages

    # ---------- Ядро воспроизведения ----------
    def _cancel_task(self, task):
        if task and not task.done():
            task.cancel()

    def _schedule_idle_disconnect(self, guild_id: int):
        st = self.state(guild_id)
        self._cancel_task(st.idle_task)

        async def _idle():
            try:
                await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
                guild = self.bot.get_guild(guild_id)
                voice = guild.voice_client if guild else None
                if voice and not voice.is_playing() and not voice.is_paused() \
                        and self.queue_manager.is_empty(guild_id):
                    await voice.disconnect()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("idle disconnect: %s", e)

        st.idle_task = asyncio.create_task(_idle())

    def _next_item(self, st: GuildState, guild_id: int):
        finished = st.current
        st.current = None
        if finished and st.loop == 'track' and not st.force_next:
            return finished
        st.force_next = False
        if finished and st.loop == 'queue':
            self.queue_manager.add(guild_id, finished)
        return self.queue_manager.pop(guild_id)

    async def _ensure_voice(self, guild: discord.Guild, st: GuildState) -> Optional[discord.VoiceClient]:
        voice = guild.voice_client
        if voice and voice.is_connected():
            target = guild.get_channel(st.voice_channel_id) if st.voice_channel_id else None
            if target and voice.channel != target and not _is_active(voice):
                try:
                    await voice.move_to(target)
                except Exception as e:
                    log.warning("Не удалось перейти в канал %s: %s", target, e)
            return voice
        if voice:  # «зомби»-подключение
            try:
                await voice.disconnect(force=True)
            except Exception:
                pass
        channel = guild.get_channel(st.voice_channel_id) if st.voice_channel_id else None
        if not channel:
            await self._send_notification(guild.id, "❌ Неизвестный голосовой канал. Используйте /play заново.")
            return None
        try:
            return await channel.connect(timeout=20, reconnect=True)
        except Exception as e:
            log.exception("Не удалось подключиться к голосовому каналу")
            await self._send_notification(guild.id, f"❌ Не удалось подключиться к каналу: {e}")
            return None

    def _after(self, guild_id: int, error, started_at: float):
        if error:
            log.error("Ошибка воспроизведения на %s: %s", guild_id, error)
        st = self.state(guild_id)
        if time.monotonic() - started_at < QUICK_FAIL_SECONDS and not st.force_next:
            # трек, умерший за пару секунд, не должен зацикливаться
            if st.loop == 'track':
                st.loop = 'off'
            st.quick_fails += 1
        else:
            st.quick_fails = 0
        if st.quick_fails >= QUICK_FAIL_LIMIT:
            st.quick_fails = 0
            asyncio.run_coroutine_threadsafe(self._halt_broken(guild_id), self.bot.loop)
            return
        asyncio.run_coroutine_threadsafe(self._play_next(guild_id), self.bot.loop)

    async def _halt_broken(self, guild_id: int):
        st = self.state(guild_id)
        st.current = None
        st.loop = 'off'
        log.error("Сервер %s: %s треков подряд оборвались сразу после старта, воспроизведение остановлено",
                  guild_id, QUICK_FAIL_LIMIT)
        await self._send_notification(
            guild_id, f"⚠️ {QUICK_FAIL_LIMIT} трека подряд оборвались сразу после старта — останавливаюсь, "
                      "чтобы не спамить. Очередь сохранена: `/repair` запустит её снова.")
        self._schedule_idle_disconnect(guild_id)

    async def _play_next(self, guild_id: int, notify: bool = True) -> Optional[dict]:
        """Запустить следующий трек. Возвращает начатый трек или None."""
        st = self.state(guild_id)
        async with st.lock:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return None
            voice = guild.voice_client
            if _is_active(voice):
                return None  # уже что-то играет
            self._cancel_task(st.idle_task)

            while True:
                item = self._next_item(st, guild_id)
                if item is None:
                    if guild.voice_client:
                        self._schedule_idle_disconnect(guild_id)
                    return None
                voice = await self._ensure_voice(guild, st)
                if voice is None:
                    self.queue_manager.get_queue(guild_id).insert(0, item)
                    return None
                try:
                    item = await resolve_item(item)
                    source = create_source(item, st.volume)
                except Exception as e:
                    log.exception("Не удалось подготовить трек %s", item.get('title'))
                    await self._send_notification(
                        guild_id, f"⚠️ Пропускаю трек **{item.get('title', '?')}**: {str(e)[:200]}")
                    if st.loop == 'track':
                        st.loop = 'off'
                    continue
                if _is_active(voice):
                    # пока трек загружался, запустилось другое аудио (звук саундборда): трек дождётся своей очереди
                    source.cleanup()
                    self.queue_manager.get_queue(guild_id).insert(0, item)
                    return None
                st.current = item
                st.started_at = time.monotonic()
                started = st.started_at
                try:
                    voice.play(source, after=lambda err, s=started: self._after(guild_id, err, s))
                except Exception as e:
                    log.exception("voice.play")
                    source.cleanup()
                    st.current = None
                    await self._send_notification(guild_id, f"⚠️ Ошибка запуска: {e}")
                    continue
                if notify:
                    await self._send_notification(guild_id, f"🎵 Сейчас играет: **{item['title']}**")
                return item

    async def _enqueue_and_start(self, interaction: discord.Interaction, items: list, *, followup=True):
        """Добавляет треки в очередь и запускает воспроизведение, если ничего не играет."""
        guild_id = interaction.guild.id
        for it in items:
            it['requester'] = interaction.user.display_name
        was_idle = not _is_active(interaction.guild.voice_client)
        position = self.queue_manager.get_queue_len(guild_id) + 1
        self.queue_manager.add_many(guild_id, items)
        label = f"**{items[0]['title']}**" if len(items) == 1 else f"**{len(items)} треков**"
        if was_idle:
            started = await self._play_next(guild_id, notify=False)
            if started:
                msg = f"▶️ Играет: **{started['title']}**"
                if len(items) > 1:
                    msg += f"\n➕ Ещё в очереди: {len(items) - 1}"
            elif _is_active(interaction.guild.voice_client):
                msg = f"⏳ Добавлено в очередь: {label}"
            else:
                msg = "❌ Не удалось начать воспроизведение (подробности выше или в `!errors`)"
        else:
            msg = f"⏳ Добавлено в очередь (позиция {position}): {label}"
        await interaction.followup.send(msg)

    # ---------- События ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        st = self.state(guild.id)
        if member.id == self.bot.user.id:
            if after.channel is None and before.channel is not None:
                # бота отключили / кикнули / он вышел сам
                self.queue_manager.clear(guild.id)
                st.current = None
                st.loop = 'off'
                self._cancel_task(st.idle_task)
                self._cancel_task(st.alone_task)
            elif after.channel:
                st.voice_channel_id = after.channel.id
            return
        voice = guild.voice_client
        if not voice or not voice.channel:
            return
        humans = [m for m in voice.channel.members if not m.bot]
        if humans:
            self._cancel_task(st.alone_task)
        elif before.channel == voice.channel or after.channel == voice.channel:
            self._cancel_task(st.alone_task)

            async def _alone():
                try:
                    await asyncio.sleep(60)
                    v = guild.voice_client
                    if v and v.channel and not [m for m in v.channel.members if not m.bot]:
                        self.queue_manager.clear(guild.id)
                        await v.disconnect()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning("alone disconnect: %s", e)

            st.alone_task = asyncio.create_task(_alone())

    # ---------- Slash-команды: воспроизведение ----------
    @app_commands.command(name="play", description="Воспроизвести трек (название/ссылка) или плейлист")
    @app_commands.guild_only()
    @app_commands.describe(query="Название, ссылка YouTube или плейлист", limit="Сколько треков брать из плейлиста (1-100)")
    async def slash_play(self, interaction: discord.Interaction, query: str, limit: app_commands.Range[int, 1, 100] = 30):
        if not await self._check_user_voice(interaction):
            return
        await interaction.response.defer()
        self._remember_context(interaction)
        try:
            result = await get_audio_source(query, limit=limit)
        except Exception as e:
            log.warning("play '%s': %s", query, e)
            return await interaction.followup.send(f"❌ Ошибка: {str(e)[:500]}")
        if result['type'] == 'playlist':
            await interaction.followup.send(f"📋 Плейлист **{result['title']}**: {len(result['tracks'])} треков")
            await self._enqueue_and_start(interaction, list(result['tracks']))
        else:
            await self._enqueue_and_start(interaction, [result])

    @app_commands.command(name="playlist", description="Воспроизвести плейлист YouTube")
    @app_commands.guild_only()
    async def slash_playlist(self, interaction: discord.Interaction, url: str,
                             limit: app_commands.Range[int, 1, 100] = 30):
        if 'list=' not in url and 'playlist' not in url:
            return await interaction.response.send_message("❌ Это не похоже на ссылку плейлиста", ephemeral=True)
        await self.slash_play.callback(self, interaction, url, limit)

    @app_commands.command(name="search", description="Поиск на YouTube с выбором результата")
    @app_commands.guild_only()
    async def slash_search(self, interaction: discord.Interaction, query: str):
        if not await self._check_user_voice(interaction):
            return
        await interaction.response.defer()
        self._remember_context(interaction)
        try:
            results = await search_youtube(query, 5)
        except Exception as e:
            return await interaction.followup.send(f"❌ Ошибка поиска: {str(e)[:500]}")
        if not results:
            return await interaction.followup.send("❌ Ничего не найдено")
        await interaction.followup.send(f"🔎 Результаты по запросу **{query}**:",
                                        view=SearchView(self, interaction, results))

    @app_commands.command(name="yandex", description="Поиск и воспроизведение трека из Яндекс.Музыки")
    @app_commands.guild_only()
    async def slash_yandex(self, interaction: discord.Interaction, query: str):
        if not await self._check_user_voice(interaction):
            return
        await interaction.response.defer()
        self._remember_context(interaction)
        try:
            track_info = await search_yandex(query)
        except Exception as e:
            return await interaction.followup.send(f"❌ {str(e)[:500]}")
        await self._enqueue_and_start(interaction, [track_info])

    @app_commands.command(name="radio", description="Воспроизвести радиостанцию")
    @app_commands.guild_only()
    @app_commands.choices(station=_radio_choices())
    async def radio(self, interaction: discord.Interaction, station: str):
        if station not in PRESET_RADIOS:
            names = "\n".join(f"• `{n}`" for n in PRESET_RADIOS)
            return await interaction.response.send_message(f"❌ Неизвестная станция. Доступны:\n{names}", ephemeral=True)
        if not await self._check_user_voice(interaction):
            return
        await interaction.response.defer()
        self._remember_context(interaction)
        item = await _get_radio_stream(PRESET_RADIOS[station], f"📻 {station.replace('_', ' ').title()}")
        await self._enqueue_and_start(interaction, [item])

    @app_commands.command(name="radio_list", description="Список радиостанций")
    async def radio_list(self, interaction: discord.Interaction):
        stations = "\n".join(f"• `{n.replace('_', ' ').title()}`" for n in PRESET_RADIOS)
        embed = discord.Embed(title="📻 Доступные радиостанции",
                              description=f"Используй `/radio <название>`\n{stations}",
                              color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="radio_link", description="Воспроизвести радио по прямой ссылке (URL)")
    @app_commands.guild_only()
    async def radio_link(self, interaction: discord.Interaction, url: str):
        if not url.startswith(('http://', 'https://')):
            return await interaction.response.send_message(
                "❌ Отправьте корректную ссылку (http:// или https://)", ephemeral=True)
        if not await self._check_user_voice(interaction):
            return
        await interaction.response.defer()
        self._remember_context(interaction)
        item = await _get_radio_stream(url, "📻 Пользовательское радио")
        await self._enqueue_and_start(interaction, [item])

    # ---------- Slash-команды: управление ----------
    @app_commands.command(name="join", description="Позвать бота в ваш голосовой канал")
    @app_commands.guild_only()
    async def slash_join(self, interaction: discord.Interaction):
        if not await self._check_user_voice(interaction):
            return
        await interaction.response.defer()
        st = self._remember_context(interaction)
        voice = interaction.guild.voice_client
        channel = interaction.user.voice.channel
        try:
            if voice and voice.is_connected():
                await voice.move_to(channel)
            else:
                await channel.connect(timeout=20, reconnect=True)
        except Exception as e:
            return await interaction.followup.send(f"❌ Не удалось подключиться: {e}")
        st.voice_channel_id = channel.id
        if not _is_active(interaction.guild.voice_client):
            self._schedule_idle_disconnect(interaction.guild.id)
        await interaction.followup.send(f"✅ Подключился к **{channel.name}**")

    @app_commands.command(name="leave", description="Выйти из голосового канала и очистить очередь")
    @app_commands.guild_only()
    async def slash_leave(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        await self._full_stop(interaction.guild.id)
        await interaction.response.send_message("👋 Отключился")

    async def _full_stop(self, guild_id: int):
        st = self.state(guild_id)
        self.queue_manager.clear(guild_id)
        st.current = None
        st.loop = 'off'
        st.force_next = True
        st.quick_fails = 0
        guild = self.bot.get_guild(guild_id)
        voice = guild.voice_client if guild else None
        if voice:
            voice.stop()
            await voice.disconnect(force=True)

    @app_commands.command(name="stop", description="Остановить музыку и очистить очередь")
    @app_commands.guild_only()
    async def slash_stop(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        await self._full_stop(interaction.guild.id)
        await interaction.response.send_message("⏹️ Остановлено и отключено")

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    @app_commands.guild_only()
    async def slash_skip(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        st = self.state(interaction.guild.id)
        if not _is_active(voice):
            return await interaction.response.send_message("❌ Ничего не играет")
        current_title = st.current.get('title', 'Неизвестно') if st.current else 'Неизвестно'
        queue = self.queue_manager.get_queue(interaction.guild.id)
        next_title = queue[0].get('title', 'Неизвестно') if queue else None
        st.force_next = True
        voice.stop()  # after-callback запустит следующий трек
        msg = f"⏭️ Пропущено: **{current_title}**"
        msg += f"\n➡️ Следующий: **{next_title}**" if next_title else "\n📭 Очередь пуста"
        await interaction.response.send_message(msg)

    @app_commands.command(name="skipto", description="Перейти к треку очереди по номеру")
    @app_commands.guild_only()
    async def slash_skipto(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 10000]):
        voice = await self._check_control(interaction)
        if not voice:
            return
        gid = interaction.guild.id
        queue = self.queue_manager.get_queue(gid)
        if position > len(queue):
            return await interaction.response.send_message(f"❌ В очереди всего {len(queue)} треков")
        skipped = queue[:position - 1]
        del queue[:position - 1]
        st = self.state(gid)
        if st.loop == 'queue':
            queue.extend(skipped)
        st.force_next = True
        if _is_active(voice):
            voice.stop()
        else:
            await self._play_next(gid, notify=False)
        await interaction.response.send_message(f"⏭️ Перехожу к треку №{position}")

    @app_commands.command(name="pause", description="Поставить на паузу")
    @app_commands.guild_only()
    async def slash_pause(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        if voice.is_playing():
            voice.pause()
            st = self.state(interaction.guild.id)
            title = st.current.get('title', 'Трек') if st.current else 'Трек'
            await interaction.response.send_message(f"⏸️ Пауза: **{title}**")
        else:
            await interaction.response.send_message("❌ Ничего не играет")

    @app_commands.command(name="resume", description="Снять с паузы")
    @app_commands.guild_only()
    async def slash_resume(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        if voice.is_paused():
            voice.resume()
            st = self.state(interaction.guild.id)
            title = st.current.get('title', 'Трек') if st.current else 'Трек'
            await interaction.response.send_message(f"▶️ Продолжаем: **{title}**")
        else:
            await interaction.response.send_message("❌ Музыка не на паузе")

    @app_commands.command(name="volume", description="Громкость (0-200%)")
    @app_commands.guild_only()
    async def slash_volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
        voice = await self._check_control(interaction)
        if not voice:
            return
        st = self.state(interaction.guild.id)
        st.volume = percent / 100
        apply_volume(voice, st.volume)
        await interaction.response.send_message(f"🔊 Громкость: **{percent}%**")

    @app_commands.command(name="loop", description="Режим повтора")
    @app_commands.guild_only()
    @app_commands.choices(mode=[app_commands.Choice(name=v, value=k) for k, v in LOOP_MODES.items()])
    async def slash_loop(self, interaction: discord.Interaction, mode: str):
        voice = await self._check_control(interaction)
        if not voice:
            return
        self.state(interaction.guild.id).loop = mode
        await interaction.response.send_message(f"Повтор: {LOOP_MODES[mode]}")

    @app_commands.command(name="shuffle", description="Перемешать очередь")
    @app_commands.guild_only()
    async def slash_shuffle(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        if self.queue_manager.get_queue_len(interaction.guild.id) < 2:
            return await interaction.response.send_message("❌ В очереди меньше двух треков")
        self.queue_manager.shuffle(interaction.guild.id)
        await interaction.response.send_message("🔀 Очередь перемешана")

    @app_commands.command(name="remove", description="Удалить трек из очереди по номеру")
    @app_commands.guild_only()
    async def slash_remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 10000]):
        voice = await self._check_control(interaction)
        if not voice:
            return
        removed = self.queue_manager.remove(interaction.guild.id, position - 1)
        if removed:
            await interaction.response.send_message(f"🗑️ Удалён: **{removed['title']}**")
        else:
            await interaction.response.send_message("❌ Нет трека с таким номером")

    @app_commands.command(name="move", description="Переместить трек в очереди")
    @app_commands.guild_only()
    async def slash_move(self, interaction: discord.Interaction,
                         position: app_commands.Range[int, 1, 10000], to: app_commands.Range[int, 1, 10000] = 1):
        voice = await self._check_control(interaction)
        if not voice:
            return
        item = self.queue_manager.move(interaction.guild.id, position - 1, to - 1)
        if item:
            await interaction.response.send_message(f"↕️ **{item['title']}** → позиция {to}")
        else:
            await interaction.response.send_message("❌ Нет трека с таким номером")

    @app_commands.command(name="clear", description="Очистить очередь (текущий трек продолжит играть)")
    @app_commands.guild_only()
    async def slash_clear(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        self.queue_manager.clear(interaction.guild.id)
        await interaction.response.send_message("🗑️ Очередь очищена")

    @app_commands.command(name="queue", description="Показать очередь")
    @app_commands.guild_only()
    async def slash_queue(self, interaction: discord.Interaction):
        embed, total_pages = await self._get_queue_embed(interaction.guild.id, 1)
        if total_pages == 0:
            return await interaction.response.send_message(embed=embed)
        view = PaginationView(self, interaction.guild.id)
        view.previous_button.disabled = True
        view.next_button.disabled = total_pages <= 1
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="now", description="Информация о текущем треке")
    @app_commands.guild_only()
    async def slash_now(self, interaction: discord.Interaction):
        voice = interaction.guild.voice_client
        st = self.state(interaction.guild.id)
        current = st.current
        if not current or not _is_active(voice):
            return await interaction.response.send_message("❌ Ничего не играет")
        embed = discord.Embed(title="🎵 Сейчас играет",
                              description=f"**{current.get('title', 'Неизвестно')[:100]}**",
                              color=discord.Color.green())
        if current.get('webpage_url'):
            embed.url = current['webpage_url']
        embed.add_field(name="Тип", value="📻 Радио" if current.get('type') == 'radio' else "🎧 Трек")
        if current.get('duration'):
            embed.add_field(name="Длительность", value=format_duration(current['duration']))
        embed.add_field(name="Статус", value="⏸️ На паузе" if voice.is_paused() else "▶️ Играет")
        embed.add_field(name="В очереди", value=str(self.queue_manager.get_queue_len(interaction.guild.id)))
        embed.add_field(name="Повтор", value=LOOP_MODES[st.loop])
        embed.add_field(name="Громкость", value=f"{int(st.volume * 100)}%")
        if current.get('requester'):
            embed.set_footer(text=f"Заказал: {current['requester']}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="repair", description="Починить зависшее воспроизведение")
    @app_commands.guild_only()
    async def slash_repair(self, interaction: discord.Interaction):
        voice = await self._check_control(interaction)
        if not voice:
            return
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild.id
        st = self.state(gid)
        if st.lock.locked():
            return await interaction.followup.send("⏳ Бот сейчас занят загрузкой трека, подождите немного", ephemeral=True)
        if _is_active(voice):
            st.force_next = True
            voice.stop()  # after-callback запустит следующий трек
            await interaction.followup.send("🔧 Текущий трек остановлен, запускаю следующий", ephemeral=True)
        else:
            started = await self._play_next(gid, notify=True)
            await interaction.followup.send(
                "🔧 Запущено: " + started['title'] if started else "📭 Запускать нечего — очередь пуста", ephemeral=True)

    @app_commands.command(name="help", description="Список команд бота")
    async def slash_help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🎶 Команды бота", color=discord.Color.blurple())
        embed.add_field(name="Воспроизведение", inline=False, value=(
            "`/play` — трек, ссылка или плейлист\n`/search` — поиск с выбором\n`/yandex` — Яндекс.Музыка\n"
            "`/radio` `/radio_list` `/radio_link` — радио"))
        embed.add_field(name="Управление", inline=False, value=(
            "`/pause` `/resume` `/skip` `/skipto` `/stop` `/leave` `/join`\n"
            "`/volume` `/loop` `/shuffle` `/remove` `/move` `/clear`"))
        embed.add_field(name="Информация", inline=False, value="`/queue` `/now` `/repair` `/ping`")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ping", description="Проверить задержку бота")
    async def slash_ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🏓 {round(self.bot.latency * 1000)} мс", ephemeral=True)


async def setup(bot):
    await bot.add_cog(MusicCog(bot, bot.queue_manager))
