"""Unit-тесты ``derive_track_title`` (ADR-008, часть B).

Детерминированный автозаголовок трека без сетевых вызовов. Заменяет пустой title,
чтобы клиент не показывал «Untitled».

Чистая функция — БД не требуется (autouse clean_db всё равно отработает).
"""
from __future__ import annotations

from app.domain.services.track_title import derive_track_title

# --------------------------------------------------------------------------
# song: title > prompt > custom_lyrics > lyrics_prompt > "New Song"
# --------------------------------------------------------------------------


def test_song_title_wins():
    assert derive_track_title("song", {"title": "My Hit", "prompt": "ignored"}) == "My Hit"


def test_song_prompt_when_no_title():
    assert derive_track_title("song", {"prompt": "an upbeat indie pop song"}) == (
        "an upbeat indie pop song"
    )


def test_song_custom_lyrics_when_no_title_prompt():
    assert derive_track_title("song", {"custom_lyrics": "hello world lyric"}) == (
        "hello world lyric"
    )


def test_song_lyrics_prompt_lowest_before_default():
    assert derive_track_title("song", {"lyrics_prompt": "song about the sea"}) == (
        "song about the sea"
    )


def test_song_priority_prompt_over_lyrics_sources():
    payload = {
        "prompt": "prompt wins",
        "custom_lyrics": "not this",
        "lyrics_prompt": "nor this",
    }
    assert derive_track_title("song", payload) == "prompt wins"


def test_song_default_when_empty():
    assert derive_track_title("song", {}) == "New Song"
    assert derive_track_title("song", None) == "New Song"


def test_song_blank_strings_skipped_to_default():
    """Пустые/пробельные значения не считаются заданными."""
    payload = {"title": "  ", "prompt": "", "custom_lyrics": "   "}
    assert derive_track_title("song", payload) == "New Song"


def test_song_prompt_truncated_at_word_boundary():
    """Длинный prompt усекается по границе слова с добавлением «…» (<=40 симв.)."""
    prompt = "The quick brown fox jumps over the lazy dog again"
    result = derive_track_title("song", {"prompt": prompt})
    assert result == "The quick brown fox jumps over the lazy…"
    assert result.endswith("…")
    # усечка не режет слово посередине: тело — префикс исходника по границе.
    body = result[:-1]
    assert prompt.startswith(body)
    assert prompt[len(body)] == " "


# --------------------------------------------------------------------------
# cover: title > "Cover • <basename(source_audio_url)>" > "Cover"
# --------------------------------------------------------------------------


def test_cover_title_wins():
    assert derive_track_title("cover", {"title": "My Cover"}) == "My Cover"


def test_cover_basename_from_source_url():
    payload = {"source_audio_url": "https://cdn.local/path/my_song.mp3"}
    assert derive_track_title("cover", payload) == "Cover • my_song"


def test_cover_basename_url_encoded_spaces():
    payload = {"source_audio_url": "https://cdn.local/dir/my%20great%20song.wav"}
    assert derive_track_title("cover", payload) == "Cover • my great song"


def test_cover_fallback_when_no_source():
    assert derive_track_title("cover", {}) == "Cover"


def test_cover_fallback_when_source_has_no_basename():
    """URL без имени файла (заканчивается на /) → просто «Cover»."""
    assert derive_track_title("cover", {"source_audio_url": "https://cdn.local/dir/"}) == "Cover"


def test_cover_does_not_use_prompt():
    """cover не использует song-ключи (prompt/custom_lyrics) — только title/source."""
    payload = {"prompt": "should be ignored", "source_audio_url": "https://cdn.local/x.mp3"}
    assert derive_track_title("cover", payload) == "Cover • x"
