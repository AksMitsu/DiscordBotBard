import logging
import time
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_TOKEN, DEFAULT_PREFIX, OWNER_IDS, ERROR_CHANNEL_ID
from utils.logs import setup_logging, record_error
from utils.queue_manager import QueueManager

setup_logging()
log = logging.getLogger('newbot')

EXTENSIONS = ('cogs.music_cog', 'cogs.admin_cog', 'cogs.fun_cog', 'cogs.api_cog')


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=DEFAULT_PREFIX,
            intents=intents,
            help_command=None,  # админ-команды не должны светиться в !help
            owner_ids=OWNER_IDS or None,
        )
        self.queue_manager = QueueManager()
        self.guild_states = {}
        self.started_at = time.time()

    async def setup_hook(self):
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("Загружено расширение %s", ext)
            except Exception as e:
                record_error(f"load {ext}", e)
        self.tree.on_error = self.on_tree_error
        synced = await self.tree.sync()
        log.info("Слэш-команд синхронизировано: %s", [c.name for c in synced])

    async def on_ready(self):
        log.info("Бот %s запущен и готов к работе", self.user)

    async def _report_to_owner_channel(self, error_id, context, error):
        if not ERROR_CHANNEL_ID:
            return
        channel = self.get_channel(ERROR_CHANNEL_ID)
        if channel:
            try:
                await channel.send(f"🚨 Ошибка #{error_id} в `{context}`: `{type(error).__name__}: {str(error)[:300]}`\n"
                                   f"Подробности: `!trace {error_id}`")
            except discord.HTTPException:
                pass

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            text = "❌ Эта команда вам недоступна"
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"⏳ Подождите {error.retry_after:.0f} с"
        else:
            original = getattr(error, 'original', error)
            name = interaction.command.name if interaction.command else '?'
            error_id = record_error(f"/{name} (guild={interaction.guild_id}, user={interaction.user.id})", original)
            text = f"❌ Внутренняя ошибка (код #{error_id}). Администратор уже может её посмотреть."
            await self._report_to_owner_channel(error_id, f"/{name}", original)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return  # обычные пользователи не должны видеть админ-команды
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            return await ctx.send(f"❌ Неверные аргументы: {error}")
        original = getattr(error, 'original', error)
        error_id = record_error(f"!{ctx.command}", original)
        await ctx.send(f"❌ Ошибка #{error_id}: `{type(original).__name__}: {str(original)[:300]}`")

    async def on_error(self, event_method, *args, **kwargs):
        import sys
        exc = sys.exc_info()[1]
        if exc:
            record_error(f"event {event_method}", exc)
        else:
            log.error("event %s\n%s", event_method, traceback.format_exc())


bot = MusicBot()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("❌ Токен не найден. Проверьте файл .env")
    bot.run(DISCORD_TOKEN, log_handler=None)
