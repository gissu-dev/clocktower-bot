import os
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _require_str(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _require_int(name: str) -> int:
    raw = _require_str(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer. Got: {raw!r}") from exc


def _int_with_default(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer. Got: {raw!r}") from exc


TOKEN = _require_str("DISCORD_TOKEN")
GUILD_ID = _require_int("GUILD_ID")
VOICE_CHANNEL_ID = _require_int("VOICE_CHANNEL_ID")
OWNER_ID = _require_int("OWNER_ID")
CLOCK_ADMIN_ROLE_ID = _int_with_default("CLOCK_ADMIN_ROLE_ID", 0)
BELL_ALLOWED_ROLE_ID = _int_with_default("BELL_ALLOWED_ROLE_ID", 0)
BELL_PUBLIC_ENABLED_DEFAULT = os.getenv("BELL_PUBLIC_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}