"""Настройки из окружения и .env рядом с бэкендом (systemd передаёт EnvironmentFile сам)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # каталог backend/


def _load_env(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE / ".env")
_load_env(BASE.parent / ".env")  # /opt/lyrics-tv/.env на ВМ


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


HOST = env("HOST", "0.0.0.0")
PORT = int(env("PORT", "8787"))
ACCESS_KEY = env("ACCESS_KEY")
CLIENT_ID = env("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = env("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
GEMINI_API_KEY = env("GEMINI_API_KEY")
# Можно списком через запятую: модели пробуются по очереди (перегрузка, снятая модель, квота)
GEMINI_MODELS = [m.strip() for m in env("GEMINI_MODEL", "gemini-flash-latest").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0] if GEMINI_MODELS else "gemini-flash-latest"
OFFSET_MS = int(env("LYRICS_OFFSET_MS", "250"))
# перевод неанглийских песен: язык (пусто — выключить), DeepL по желанию, иначе Google без ключа
TRANSLATE_TO = env("TRANSLATE_TO", "ru").strip().lower()
DEEPL_API_KEY = env("DEEPL_API_KEY")
IDLE_AFTER_S = int(env("IDLE_AFTER_S", "180"))
DATA_DIR = Path(env("DATA_DIR", str(BASE / "data")))
DEMO = env("DEMO", "0") == "1"
DEMO_START_MS = int(env("DEMO_START_MS", "0"))

# Адреса внешних сервисов — переопределяются в сквозных тестах.
SPOTIFY_ACCOUNTS = env("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com")
SPOTIFY_API = env("SPOTIFY_API_BASE", "https://api.spotify.com/v1")
LRCLIB_BASE = env("LRCLIB_BASE", "https://lrclib.net")
NETEASE_BASE = env("NETEASE_BASE", "https://music.163.com")
GEMINI_BASE = env("GEMINI_BASE", "https://generativelanguage.googleapis.com")
DEEPL_BASE = env("DEEPL_BASE", "https://api-free.deepl.com")
GOOGLE_TRANSLATE_BASE = env("GOOGLE_TRANSLATE_BASE", "https://translate.googleapis.com")
# Noto Sans CJK Bold (OFL): {FONT_BASE}/JP/NotoSansJP-Bold.otf и т.д.
FONT_BASE = env("FONT_BASE", "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF")

WEB_DIR = BASE / "web"
DEMO_DIR = BASE / "demo"
COVERS_DIR = DATA_DIR / "covers"
TOKEN_FILE = DATA_DIR / "spotify_token.json"
CACHE_DB = DATA_DIR / "cache.db"
FONTS_DIR = DATA_DIR / "fonts"

TOKEN_LIFETIME_DAYS = 182      # refresh-токен Spotify живёт ~6 месяцев с момента входа
NEGATIVE_RETRY_S = 3 * 86400   # «текст не найден» перепроверяем раз в три дня
COVERS_KEEP = 300

UA = "lyrics-tv/1.0 (personal now-playing display; +https://github.com/daaazzzeeed/now-lyrics)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
