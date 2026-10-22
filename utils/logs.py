import logging
import logging.handlers
import time
import traceback
from collections import deque

from config import LOG_FILE

# Кольцевой буфер последних ошибок — для команд !errors / !trace
ERRORS = deque(maxlen=50)
_counter = 0


def setup_logging():
    root = logging.getLogger()
    if any(getattr(h, '_newbot', False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    stream_handler = logging.StreamHandler()
    for h in (file_handler, stream_handler):
        h.setFormatter(fmt)
        h._newbot = True
        root.addHandler(h)
    logging.getLogger('discord.http').setLevel(logging.WARNING)


def record_error(context: str, error: BaseException) -> int:
    """Сохраняет ошибку в буфер и лог, возвращает её номер."""
    global _counter
    _counter += 1
    tb = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    ERRORS.append({'id': _counter, 'time': time.time(), 'context': context, 'trace': tb})
    logging.getLogger('newbot').error('#%s %s\n%s', _counter, context, tb)
    return _counter
