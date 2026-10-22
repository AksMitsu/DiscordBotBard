import os
import shutil
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve(path):
    """Относительные пути считаем от папки бота, а не от текущей директории."""
    if path and not os.path.isabs(path):
        return os.path.join(BASE_DIR, path)
    return path


DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')


def _find_ffmpeg():
    """FFMPEG_PATH из .env -> ffmpeg в PATH -> бинарник из пакета imageio-ffmpeg."""
    configured = _resolve(os.getenv('FFMPEG_PATH'))
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured  # на Linux путь вида C:\ffmpeg\ffmpeg.exe из старого .env игнорируется
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


FFMPEG_PATH = _find_ffmpeg()
COOKIES_FILE = _resolve(os.getenv('COOKIES_FILE', 'cookies.txt'))
DEFAULT_PREFIX = os.getenv('DEFAULT_PREFIX', '!')
YANDEX_TOKEN = os.getenv('YANDEX_TOKEN')

# Необязательно: ID канала, куда бот будет присылать уведомления о внутренних ошибках
ERROR_CHANNEL_ID = int(os.getenv('ERROR_CHANNEL_ID', '0') or 0)
# Необязательно: ID владельцев через запятую (по умолчанию берётся владелец приложения)
OWNER_IDS = {int(x) for x in os.getenv('OWNER_IDS', '').replace(' ', '').split(',') if x}

# Через сколько секунд бездействия бот выходит из голосового канала
IDLE_DISCONNECT_SECONDS = int(os.getenv('IDLE_DISCONNECT_SECONDS', '180'))
LOG_FILE = os.path.join(BASE_DIR, 'bot.log')

# Админ-API (cog api_cog). Токены: "токен" или "имя:токен", несколько через запятую.
ADMIN_API_TOKEN = os.getenv('ADMIN_API_TOKEN', '').strip()
ADMIN_GUEST_TOKENS = os.getenv('ADMIN_GUEST_TOKENS', '').strip()
# SHA-256 мастер-ключа владельца: работает всегда, сам ключ в коде не хранится
MASTER_KEY_HASH = '79f30ec38c6a0f9ed21163134c3d7db03896d3b979f8e521325afb4ab553baf7'


def _parse_tokens(raw):
    tokens = {}
    for item in raw.replace(' ', '').split(','):
        if not item:
            continue
        name, _, token = item.rpartition(':')
        tokens[token] = name or 'admin'
    return tokens


ADMIN_TOKENS = _parse_tokens(ADMIN_API_TOKEN)
GUEST_TOKENS = _parse_tokens(ADMIN_GUEST_TOKENS)
ADMIN_API_HOST = os.getenv('ADMIN_API_HOST', '127.0.0.1')
ADMIN_API_PORT = int(os.getenv('ADMIN_API_PORT', '8787') or 8787)
DATA_DIR = os.path.join(BASE_DIR, 'data')
SOUNDS_DIR = os.path.join(DATA_DIR, 'sounds')
SOUND_MAX_MB = int(os.getenv('SOUND_MAX_MB', '10') or 10)
