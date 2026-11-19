import os
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
MINIAPP_URL: str = os.getenv("MINIAPP_URL", "")
SERVER_PORT: int = int(os.getenv("PORT", 8080))
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "464800908"))
ALLOW_INSECURE_TG_ID: bool = os.getenv("ALLOW_INSECURE_TG_ID", "false").lower() in ("1", "true", "yes")

if not BOT_TOKEN or not MINIAPP_URL or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Перевірте змінні середовища BOT_TOKEN, MINIAPP_URL, SUPABASE_URL та SUPABASE_KEY!")


def _build_allowed_origins() -> list[str]:
    origins: list[str] = []
    parsed = urlparse(MINIAPP_URL)
    if parsed.scheme and parsed.netloc:
        origins.append(f"{parsed.scheme}://{parsed.netloc}")
    extra = os.getenv("ALLOWED_ORIGINS", "")
    for item in extra.split(","):
        cleaned = item.strip().rstrip("/")
        if cleaned and cleaned not in origins:
            origins.append(cleaned)
    for dev_origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        if dev_origin not in origins:
            origins.append(dev_origin)
    return origins


ALLOWED_ORIGINS: list[str] = _build_allowed_origins()
PRIMARY_ORIGIN: str = ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else MINIAPP_URL.rstrip("/")
