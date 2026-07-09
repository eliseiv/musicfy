"""Детерминированный заголовок видео (без сетевых вызовов/генерации).

Симметрично ``derive_track_title`` (ADR-008), но **без усечения**: явный
пользовательский ``title`` сохраняется целиком (только trim, лимит ≤255 гарантирован
схемой ``CreateVideoRequest.title`` / ``RenameVideoRequest.title``). Дефолт при
отсутствии ``title`` — фиксированная человекочитаемая метка режима. См. ADR-012 §4.
"""

from __future__ import annotations

from typing import Any

_MODE_TITLES = {
    "avatar_performance": "Avatar Video",
    "visual_clip": "Visual Clip",
    "lyrics_video": "Lyrics Video",
}
_FALLBACK_TITLE = "Music Video"


def derive_video_title(payload: dict[str, Any] | None) -> str:
    """Возвращает детерминированный непустой заголовок видео.

    1. Явный ``payload['title']`` (задан и непуст) → **только strip**, без усечения.
    2. Иначе дефолт по ``payload['mode']`` (метка режима).
    3. Fallback (неизвестный/пустой mode) → «Music Video».
    """
    data = payload or {}

    explicit = data.get("title")
    if isinstance(explicit, str):
        stripped = explicit.strip()
        if stripped:
            return stripped

    mode = data.get("mode")
    if isinstance(mode, str):
        label = _MODE_TITLES.get(mode)
        if label:
            return label
    return _FALLBACK_TITLE
