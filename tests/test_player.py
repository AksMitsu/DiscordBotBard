"""Регрессионные проверки ядра плеера и микшера без Discord: python tests/test_player.py"""
import asyncio
import os
import struct
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

from cogs.music_cog import MusicCog, apply_volume, QUICK_FAIL_LIMIT  # noqa: E402
from utils.mixer import MixSource  # noqa: E402
from utils.queue_manager import QueueManager  # noqa: E402

FAILED = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILED.append(name)


class FakeVoice:
    def __init__(self, channel, playing=False):
        self.channel = channel
        self._playing = playing
        self.moved_to = None

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    async def move_to(self, channel):
        self.moved_to = channel
        self.channel = channel


class Pcm(discord.AudioSource):
    def __init__(self, frames, value):
        self.frames, self.value = frames, value

    def read(self):
        if self.frames <= 0:
            return b''
        self.frames -= 1
        return struct.pack('<h', self.value) * 1920


def make_cog(guild):
    bot = SimpleNamespace(guild_states={}, loop=asyncio.get_running_loop(), get_guild=lambda gid: guild,
                          get_channel=lambda cid: None, user=SimpleNamespace(id=99))
    cog = MusicCog(bot, QueueManager())

    async def no_notify(guild_id, content=None, embed=None):
        pass

    cog._send_notification = no_notify
    return cog


async def test_track_kept_when_connect_fails():
    guild = SimpleNamespace(id=1, voice_client=None, get_channel=lambda cid: None)
    cog = make_cog(guild)
    cog.queue_manager.add(1, {'type': 'track', 'title': 'A', 'webpage_url': 'x'})
    started = await cog._play_next(1)
    queue = [t['title'] for t in cog.queue_manager.get_queue(1)]
    check('трек не теряется при ошибке подключения', started is None and queue == ['A'])


async def test_idle_bot_follows_user():
    chan_a, chan_b = SimpleNamespace(id=10, name='A'), SimpleNamespace(id=20, name='B')
    guild = SimpleNamespace(id=1, voice_client=FakeVoice(chan_a), get_channel=lambda cid: {10: chan_a, 20: chan_b}.get(cid))
    cog = make_cog(guild)
    st = cog.state(1)
    st.voice_channel_id = 20
    voice = await cog._ensure_voice(guild, st)
    check('простаивающий бот переходит в канал пользователя', voice.moved_to is chan_b)
    guild.voice_client = FakeVoice(chan_a, playing=True)
    voice = await cog._ensure_voice(guild, st)
    check('играющий бот не перескакивает в другой канал', voice.moved_to is None)


async def test_quick_fail_guard():
    cog = make_cog(SimpleNamespace(id=1, voice_client=None))
    halted = []

    async def halt(guild_id):
        halted.append(guild_id)

    async def play_next(guild_id, notify=True):
        return None

    cog._halt_broken = halt
    cog._play_next = play_next
    st = cog.state(1)
    for _ in range(QUICK_FAIL_LIMIT):
        cog._after(1, None, time.monotonic())
    await asyncio.sleep(0.05)
    check(f'после {QUICK_FAIL_LIMIT} мгновенных обрывов воспроизведение останавливается', halted == [1])
    st.force_next = True
    for _ in range(QUICK_FAIL_LIMIT + 2):
        cog._after(1, None, time.monotonic())
    await asyncio.sleep(0.05)
    check('быстрые пропуски не считаются обрывами', halted == [1])


def test_volume():
    base = discord.PCMVolumeTransformer(Pcm(1, 0), volume=1.0)
    apply_volume(SimpleNamespace(source=MixSource(base)), 0.4)
    check('громкость применяется к треку под MixSource', abs(base.volume - 0.4) < 1e-9)
    apply_volume(SimpleNamespace(source=base), 0.7)
    check('громкость применяется к обычному треку', abs(base.volume - 0.7) < 1e-9)
    apply_volume(None, 0.5)


def test_mixer():
    idle_calls = []
    mixer = MixSource(Pcm(4, 30000), on_idle=lambda: idle_calls.append(1))
    mixer.add(Pcm(2, 10000))
    values = [struct.unpack('<h', f[:2])[0] if f else None for f in (mixer.read() for _ in range(5))]
    check('микшер ограничивает сумму и продолжает трек', values == [32767, 32767, 30000, 30000, None])
    check('после звука микшер ровно один раз просит вернуть трек', idle_calls == [1])
    sounds_only = MixSource()
    sounds_only.add(Pcm(1, 100))
    check('микшер без трека заканчивается вместе со звуком', sounds_only.read() and sounds_only.read() == b'')


async def main():
    await test_track_kept_when_connect_fails()
    await test_idle_bot_follows_user()
    await test_quick_fail_guard()
    test_volume()
    test_mixer()


if __name__ == '__main__':
    asyncio.run(main())
    print(f'\nПровалено: {len(FAILED)}' if FAILED else '\nВсе проверки пройдены')
    sys.exit(1 if FAILED else 0)
