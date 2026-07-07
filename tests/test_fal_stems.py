"""Unit-тесты ``extract_stems`` (ADR-008, часть A).

demucs отдаёт стемы верхнеуровневыми ключами payload (vocals/drums/bass/other),
а не в обёртке ``result["stems"]``. ``extract_stems`` разбирает оба формата:
приоритет — явная обёртка (обратная совместимость wrapped-моделей), иначе demucs-путь
(top-level ключи из STEM_NAMES, порог >=2 против ложного срабатывания на омониме).

Чистая функция без I/O — тестовая БД не требуется (autouse clean_db не задействуем).
"""
from __future__ import annotations

from app.domain.providers.fal.parsing import STEM_NAMES, extract_stems


def test_demucs_top_level_object_urls_collected():
    """Реальный demucs: >=2 верхнеуровневых стема с {url} → dict со всеми ключами."""
    result = {
        "vocals": {"url": "https://v/vocals.wav"},
        "drums": {"url": "https://v/drums.wav"},
        "bass": {"url": "https://v/bass.wav"},
        "other": {"url": "https://v/other.wav"},
    }
    stems = extract_stems(result)
    assert stems is not None
    assert set(stems) == {"vocals", "drums", "bass", "other"}
    assert stems["vocals"] == {"url": "https://v/vocals.wav"}


def test_demucs_top_level_string_urls_collected():
    """Стемы строкой-url (иная форма) тоже собираются (порог >=2)."""
    result = {
        "vocals": "https://v/vocals.wav",
        "accompaniment": "https://v/inst.wav",
    }
    stems = extract_stems(result)
    assert stems == {
        "vocals": "https://v/vocals.wav",
        "accompaniment": "https://v/inst.wav",
    }


def test_explicit_stems_wrapper_takes_priority():
    """Явная обёртка result['stems'] имеет приоритет над top-level ключами."""
    explicit = {"vocal": "https://v/v.mp3", "music": "https://v/m.mp3"}
    result = {
        "stems": explicit,
        # top-level шум, который НЕ должен победить обёртку
        "vocals": {"url": "https://v/other-vocals.wav"},
        "drums": {"url": "https://v/other-drums.wav"},
    }
    assert extract_stems(result) is explicit


def test_empty_explicit_wrapper_falls_through_to_demucs():
    """Пустая обёртка result['stems']={} не блокирует demucs-путь."""
    result = {
        "stems": {},
        "vocals": {"url": "https://v/vocals.wav"},
        "drums": {"url": "https://v/drums.wav"},
    }
    stems = extract_stems(result)
    assert stems == {
        "vocals": {"url": "https://v/vocals.wav"},
        "drums": {"url": "https://v/drums.wav"},
    }


def test_single_stem_key_below_threshold_is_none():
    """Один stem-именованный ключ (омоним у не-сепаратора) < порога 2 → None."""
    result = {"vocals": {"url": "https://v/vocals.wav"}}
    assert extract_stems(result) is None


def test_song_audio_result_not_mistaken_for_stems():
    """Результат песни {'audio': {'url': ..}} → None (audio не входит в STEM_NAMES)."""
    result = {"audio": {"url": "https://v/song.mp3", "duration": 42.0}}
    assert extract_stems(result) is None


def test_empty_result_is_none():
    assert extract_stems({}) is None


def test_non_url_stem_values_ignored_below_threshold():
    """Значения без url (число/None/пустая строка) не считаются стемами → None."""
    result = {"vocals": 123, "drums": None, "bass": ""}
    assert extract_stems(result) is None


def test_mixed_valid_and_invalid_only_valid_counted():
    """Валидные стемы считаются, мусорные — нет; при >=2 валидных → dict только из них."""
    result = {
        "vocals": {"url": "https://v/vocals.wav"},
        "drums": "https://v/drums.wav",
        "bass": 0,            # не url → игнор
        "unknown_key": {"url": "https://v/x.wav"},  # не в STEM_NAMES → игнор
    }
    stems = extract_stems(result)
    assert stems == {
        "vocals": {"url": "https://v/vocals.wav"},
        "drums": "https://v/drums.wav",
    }


def test_stem_names_dictionary_covers_demucs_outputs():
    """Гарантия словаря: базовые demucs-выходы присутствуют в STEM_NAMES."""
    assert {"vocals", "drums", "bass", "other"} <= STEM_NAMES
