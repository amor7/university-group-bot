"""Configuration loaded from environment / .env (lazily, via load_config)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# The folder that contains bot/ (and .env): paths don't depend on the current directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    db_path: str
    group_id: int | None
    default_contact: str | None = None
    proxy_url: str | None = None


def parse_admin_ids(raw: str | None) -> frozenset[int]:
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise ConfigError(f"ADMIN_IDS contains a non-numeric value: {part!r}") from None
    return frozenset(ids)


def parse_group_id(raw: str | None) -> int | None:
    """The group's numeric id. Tolerates a copied label such as "ID: -100123"."""
    raw = (raw or "").strip()
    if not raw:
        return None
    match = re.fullmatch(r"(?:id\s*:?\s*)?(-?\d+)", raw, flags=re.IGNORECASE)
    if not match:
        raise ConfigError(f"GROUP_ID must be an integer like -1001234567890, got {raw!r}")
    return int(match.group(1))


def parse_proxy_url(raw: str | None) -> str | None:
    """Optional proxy for reaching api.telegram.org (http://, https://, socks5://)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.match(r"^(https?|socks5h?)://\S+$", raw, flags=re.IGNORECASE):
        raise ConfigError(
            f"PROXY_URL must look like http://host:port or socks5://host:port, got {raw!r}")
    return raw


def resolve_db_path(raw: str, root: Path | None = None) -> str:
    """A relative DB_PATH is relative to the project root, not to the current directory."""
    if raw == ":memory:" or Path(raw).is_absolute():
        return raw
    return str((root or PROJECT_ROOT) / raw)


def load_config(env_file: str | None = None) -> Config:
    """Read .env (default: the project root's .env) and the environment.
    Raises ConfigError on problems."""
    load_dotenv(env_file if env_file is not None else PROJECT_ROOT / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    group_id = parse_group_id(os.getenv("GROUP_ID", ""))
    return Config(
        bot_token=token,
        admin_ids=parse_admin_ids(os.getenv("ADMIN_IDS")),
        db_path=resolve_db_path(os.getenv("DB_PATH", "").strip() or "bot.db"),
        group_id=group_id,
        default_contact=os.getenv("DEFAULT_CONTACT", "").strip() or None,
        proxy_url=parse_proxy_url(os.getenv("PROXY_URL", "")),
    )
