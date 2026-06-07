from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def slugify(text: str, fallback: str = "item") -> str:
    text = text.strip().lower()
    text = re.sub(r"\$\{([^}]+)\}", r"\1", text)
    ascii_part = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if ascii_part:
        return ascii_part[:64]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{fallback}_{digest}"


def stable_hash(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def semantic_id(prefix: str, category: str, name: str) -> str:
    return f"{prefix}.{category}.{slugify(name, category)}"


def timestamped_id(prefix: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{now}_{stable_hash(now, 5)}"


def turn_id(episode_id: str, turn_index: int) -> str:
    return f"{episode_id}.t{turn_index}"

