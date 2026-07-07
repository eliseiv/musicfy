"""Детерминированный автозаголовок трека (без сетевых вызовов/генерации).

Заменяет пустой ``title`` осмысленным значением, выведенным из input_payload,
чтобы клиент не показывал «Untitled». См. ADR-008 (часть B).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import unquote, urlsplit

_TITLE_MAX_LEN = 40


def _truncate(text: str) -> str:
    """Усечение до ``_TITLE_MAX_LEN`` по границе слова + «…» при обрезке."""
    text = text.strip()
    if len(text) <= _TITLE_MAX_LEN:
        return text
    head = text[:_TITLE_MAX_LEN]
    cut = head.rsplit(" ", 1)[0].strip()
    if not cut:
        cut = head.strip()
    return f"{cut}…"


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _source_basename(url: str) -> str | None:
    """Имя файла источника без расширения (для суффикса cover-заголовка)."""
    path = urlsplit(url).path
    name = unquote(os.path.basename(path))
    if not name:
        return None
    stem = os.path.splitext(name)[0].strip()
    return stem or None


def derive_track_title(kind: str, input_payload: dict[str, Any] | None) -> str:
    """Возвращает детерминированный непустой заголовок трека.

    song: ``title`` → ``prompt`` → ``custom_lyrics`` → ``lyrics_prompt`` → «New Song».
    cover: ``title`` → «Cover • <basename(source_audio_url)>» → «Cover».
    """
    payload = input_payload or {}

    explicit = _clean(payload.get("title"))
    if explicit:
        return _truncate(explicit)

    if kind == "cover":
        source_url = _clean(payload.get("source_audio_url"))
        if source_url:
            basename = _source_basename(source_url)
            if basename:
                return f"Cover • {_truncate(basename)}"
        return "Cover"

    # song
    for key in ("prompt", "custom_lyrics", "lyrics_prompt"):
        value = _clean(payload.get(key))
        if value:
            return _truncate(value)
    return "New Song"
