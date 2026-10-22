import random

import discord
from discord import app_commands
from discord.ext import commands

EIGHT_BALL = (
    'Бесспорно', 'Предрешено', 'Никаких сомнений', 'Определённо да', 'Можешь быть уверен',
    'Мне кажется — да', 'Вероятнее всего', 'Хорошие перспективы', 'Знаки говорят — да',
    'Пока не ясно, попробуй снова', 'Спроси позже', 'Лучше не рассказывать', 'Сейчас нельзя предсказать',
    'Сконцентрируйся и спроси опять', 'Даже не думай', 'Мой ответ — нет', 'По моим данным — нет',
    'Перспективы не очень хорошие', 'Весьма сомнительно',
)

COMPLIMENTS = (
    'у тебя талант делать любой чат живее', 'твой плейлист — лучшее, что случалось с этим каналом',
    'с тобой даже тишина в голосовом звучит как хит', 'ты как идеальный бит: всё сразу встаёт на места',
    'твоё чувство юмора заслуживает отдельного сервера', 'без тебя здесь было бы заметно скучнее',
)

JOKES = (
    'Почему программисты путают Хэллоуин и Рождество? Потому что Oct 31 == Dec 25.',
    'Заходит байт в бар, а там уже все биты.',
    'Звукорежиссёр — это человек, который слышит всё, но говорит только «поправим на сведении».',
    'Бот — это как друг, только он всегда онлайн и никогда не обижается на «/skip».',
    '— Алло, это техподдержка? — Да. — Мой бот молчит. — А вы пробовали включить звук?',
)


class FunCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='8ball', description='Магический шар отвечает на вопрос')
    @app_commands.describe(question='Ваш вопрос')
    async def eight_ball(self, interaction: discord.Interaction, question: str):
        embed = discord.Embed(color=discord.Color.purple())
        embed.add_field(name='❓ Вопрос', value=question[:1000], inline=False)
        embed.add_field(name='🎱 Ответ', value=random.choice(EIGHT_BALL), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='roll', description='Бросить кубик (например 2d6, d20)')
    @app_commands.describe(dice='Формат NdM, по умолчанию d6')
    async def roll(self, interaction: discord.Interaction, dice: str = 'd6'):
        try:
            count, sides = dice.lower().split('d')
            count, sides = int(count or 1), int(sides)
        except ValueError:
            return await interaction.response.send_message('❌ Формат: `2d6`, `d20`', ephemeral=True)
        if not (1 <= count <= 20 and 2 <= sides <= 1000):
            return await interaction.response.send_message('❌ От 1 до 20 кубиков, граней от 2 до 1000', ephemeral=True)
        rolls = [random.randint(1, sides) for _ in range(count)]
        detail = f" ({' + '.join(map(str, rolls))})" if count > 1 else ''
        await interaction.response.send_message(f'🎲 `{count}d{sides}`: **{sum(rolls)}**{detail}')

    @app_commands.command(name='coin', description='Подбросить монетку')
    async def coin(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🪙 {random.choice(('Орёл', 'Решка'))}!")

    @app_commands.command(name='choose', description='Выбрать вариант из списка (через запятую)')
    @app_commands.describe(options='Например: пицца, суши, шаурма')
    async def choose(self, interaction: discord.Interaction, options: str):
        items = [o.strip() for o in options.split(',') if o.strip()]
        if len(items) < 2:
            return await interaction.response.send_message('❌ Нужно минимум два варианта через запятую', ephemeral=True)
        await interaction.response.send_message(f'🎯 Выбираю: **{random.choice(items)[:200]}**')

    @app_commands.command(name='rate', description='Оценить что угодно по шкале от 0 до 10')
    @app_commands.describe(thing='Что оценить')
    async def rate(self, interaction: discord.Interaction, thing: str):
        rng = random.Random(thing.strip().lower())
        await interaction.response.send_message(f'📊 **{thing[:100]}** — {rng.randint(0, 10)}/10',
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='compliment', description='Получить комплимент')
    @app_commands.describe(member='Кому (по умолчанию вам)')
    async def compliment(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        await interaction.response.send_message(f'💐 {target.mention}, {random.choice(COMPLIMENTS)}.',
                                                allowed_mentions=discord.AllowedMentions(users=[target]))

    @app_commands.command(name='joke', description='Случайная шутка')
    async def joke(self, interaction: discord.Interaction):
        await interaction.response.send_message(f'😄 {random.choice(JOKES)}')


async def setup(bot):
    await bot.add_cog(FunCog(bot))
