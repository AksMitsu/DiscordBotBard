import asyncio
import random
from dataclasses import dataclass, field
from typing import Optional


class QueueManager:
    def __init__(self):
        self.queues = {}

    def get_queue(self, guild_id):
        if guild_id not in self.queues:
            self.queues[guild_id] = []
        return self.queues[guild_id]

    def get_queue_len(self, guild_id):
        return len(self.get_queue(guild_id))

    def add(self, guild_id, item):
        self.get_queue(guild_id).append(item)

    def add_many(self, guild_id, items):
        self.get_queue(guild_id).extend(items)

    def pop(self, guild_id):
        queue = self.get_queue(guild_id)
        return queue.pop(0) if queue else None

    def remove(self, guild_id, index: int):
        """Удаляет трек по индексу (с 0). Возвращает трек или None."""
        queue = self.get_queue(guild_id)
        if 0 <= index < len(queue):
            return queue.pop(index)
        return None

    def move(self, guild_id, src: int, dst: int):
        queue = self.get_queue(guild_id)
        if not (0 <= src < len(queue)):
            return None
        dst = max(0, min(dst, len(queue) - 1))
        item = queue.pop(src)
        queue.insert(dst, item)
        return item

    def shuffle(self, guild_id):
        random.shuffle(self.get_queue(guild_id))

    def clear(self, guild_id):
        self.queues[guild_id] = []

    def is_empty(self, guild_id):
        return len(self.get_queue(guild_id)) == 0


@dataclass
class GuildState:
    """Состояние плеера на одном сервере."""
    current: Optional[dict] = None
    loop: str = 'off'              # off | track | queue
    volume: float = 1.0
    text_channel_id: Optional[int] = None
    voice_channel_id: Optional[int] = None
    force_next: bool = False       # пропуск трека при loop=track
    started_at: float = 0.0
    quick_fails: int = 0          # подряд оборвавшиеся сразу после старта треки
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_task: Optional[asyncio.Task] = None
    alone_task: Optional[asyncio.Task] = None
