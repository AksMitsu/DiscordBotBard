"""Источник звука, подмешивающий короткие звуки поверх текущего трека без его остановки."""
import threading
from array import array

import discord


def _mix(a: bytes, b: bytes) -> bytes:
    if len(a) < len(b):
        a, b = b, a
    x = array('h')
    x.frombytes(a[:len(a) // 2 * 2])
    y = array('h')
    y.frombytes(b[:len(b) // 2 * 2])
    for i in range(len(y)):
        v = x[i] + y[i]
        x[i] = 32767 if v > 32767 else -32768 if v < -32768 else v
    return x.tobytes()


class MixSource(discord.AudioSource):
    """base=None — режим «только звуки»; иначе оборачивает играющий трек и возвращает его через on_idle."""

    def __init__(self, base=None, on_idle=None):
        self.base = base
        self.on_idle = on_idle
        self.overlays = []
        self.base_done = base is None
        self.detached = False
        self._lock = threading.Lock()
        self._notified = False

    def add(self, source):
        with self._lock:
            self.overlays.append(source)
            self._notified = False

    def stop_overlays(self):
        with self._lock:
            for src in self.overlays:
                src.cleanup()
            self.overlays.clear()

    def read(self) -> bytes:
        frame = b''
        if not self.base_done:
            frame = self.base.read()
            if not frame:
                self.base_done = True
        with self._lock:
            for src in list(self.overlays):
                data = src.read()
                if not data:
                    self.overlays.remove(src)
                    src.cleanup()
                    continue
                frame = _mix(frame, data) if frame else data
            idle = not self.overlays
        if idle and not self.base_done and self.on_idle and not self._notified:
            self._notified = True
            self.on_idle()
        return frame

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self.stop_overlays()
        if self.base is not None and not self.detached:
            self.base.cleanup()
