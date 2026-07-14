"""ADR-016 — фикс параметров видео-генерации (style→prompt, generate_audio=false,
resolution/720p, duration-опция, ffmpeg re-encode caps).

Реальный fal НЕ дёргается. Два уровня проверки контракта fal-submit:

1. Client-payload (unit, реальный FalAiProvider с перехваченным ``_submit``): проверяет
   ИМЕННО ТЕЛО HTTP-запроса, которое клиент собрал бы для fal — включая инвариант
   «None-поля не отправляются» (``duration`` отсутствует при None). Имена полей сверены
   с реальной схемой seedance-2.0 {prompt,resolution,duration,aspect_ratio,generate_audio,
   bitrate_mode} (ADR-016 Context/D5). Перехват на ``_submit`` — до HTTP, сеть не трогается.
2. Pipeline-boundary (integration, стаб fal + spy на app.state.fal_provider): проверяет,
   что VideoPipeline прокидывает style→prompt, generate_audio=False, resolution из Settings,
   duration из env-капа и НЕ прокидывает kwarg ``style``. На этом уровне duration=None
   передаётся явным kwarg (ключ есть, значение None) — «ключа duration нет» относится к
   уровню client-payload (см. п.1).

ffmpeg в тест-среде отсутствует → команды ffmpeg проверяются через перехват
``video_mux._run`` (сборка cmd без реального прогона).
"""
from __future__ import annotations

import pytest

from app.domain.enums import VideoStyle
from app.domain.providers.fal.base import FalSubmitResult
from app.domain.providers.fal.client import FalAiProvider
from app.domain.providers.fal.stub import StubFalProvider
from app.domain.services.pipelines.video import (
    DEFAULT_LYRICS_BG_PROMPT,
    STYLE_PROMPT_SUFFIX,
    VideoPipeline,
    _apply_style,
)
from tests.helpers import auth_headers, grant_coins

GRANT = 100

# Реальная входная схема seedance-2.0 t2v/i2v (ADR-016 Context, source of truth для
# имён полей). i2v добавляет image_url. Тело нашего сабмита ОБЯЗАНО быть подмножеством.
_SEEDANCE_T2V_FIELDS = {
    "prompt",
    "resolution",
    "duration",
    "aspect_ratio",
    "generate_audio",
    "bitrate_mode",
}
_SEEDANCE_I2V_FIELDS = _SEEDANCE_T2V_FIELDS | {"image_url"}


# --------------------------------------------------------------------------
# _Spy — перехват kwargs на границе pipeline → fal-submit (стаб)
# --------------------------------------------------------------------------


class _Spy:
    def __init__(self, target, name):
        self.calls: list[dict] = []
        self._orig = getattr(target, name)
        self._target = target
        self._name = name

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return await self._orig(**kwargs)

    def install(self):
        setattr(self._target, self._name, self)
        return self


# --------------------------------------------------------------------------
# Client-payload contract: реальный FalAiProvider, _submit перехвачен
# --------------------------------------------------------------------------


def _make_fal_client() -> FalAiProvider:
    return FalAiProvider(
        api_key="test-key",
        base_url="https://queue.fal.run",
        song_model="m/song",
        refine_model="m/refine",
        speech_model="m/speech",
        voice_clone_model="m/clone",
        lyrics_llm="m/llm",
        demucs_model="m/demucs",
        voice_changer_model="m/vc",
        video_avatar_model="vendor/kling-lipsync",
        video_avatar_image_model="vendor/sync-lipsync-i2v",
        video_visual_model="bytedance/seedance-2.0/text-to-video",
        video_visual_image_model="bytedance/seedance-2.0/image-to-video",
        video_lyrics_bg_model="vendor/lyrics-bg",
        webhook_secret="s",
    )


@pytest.fixture
async def fal_client():
    provider = _make_fal_client()
    captured: dict = {}

    async def fake_submit(*, model, payload, webhook_url, idempotency_key):
        captured["model"] = model
        captured["payload"] = payload
        captured["webhook_url"] = webhook_url
        captured["idempotency_key"] = idempotency_key
        return FalSubmitResult(request_id="rid-stub", status="queued")

    provider._submit = fake_submit  # type: ignore[method-assign]
    try:
        yield provider, captured
    finally:
        await provider.aclose()


async def test_client_t2v_payload_contract(fal_client):
    """Case 1: t2v-тело = {prompt(со style), generate_audio=False, aspect_ratio, resolution};
    БЕЗ ключей style и duration (duration=None → не отправляется). Имена полей ⊆ seedance."""
    provider, captured = fal_client
    styled_prompt = "neon city, " + STYLE_PROMPT_SUFFIX[VideoStyle.cartoon.value]

    await provider.submit_text_to_video(
        prompt=styled_prompt,
        aspect_ratio="1:1",
        webhook_url="https://hook",
        idempotency_key="job:visual",
        resolution="720p",
        generate_audio=False,
        duration=None,
    )

    payload = captured["payload"]
    # Имена полей сверены с реальной схемой seedance-2.0.
    assert set(payload) <= _SEEDANCE_T2V_FIELDS
    assert payload.keys() == {"prompt", "generate_audio", "aspect_ratio", "resolution"}
    assert payload["generate_audio"] is False
    assert payload["resolution"] == "720p"
    assert payload["aspect_ratio"] == "1:1"
    assert payload["prompt"] == styled_prompt
    assert "style" not in payload  # у seedance нет поля style
    assert "duration" not in payload  # None-поле не отправляется
    assert captured["model"] == "bytedance/seedance-2.0/text-to-video"


async def test_client_t2v_payload_includes_duration_when_set(fal_client):
    """Case 5: при duration='5' поле duration присутствует в теле."""
    provider, captured = fal_client
    await provider.submit_text_to_video(
        prompt="p",
        aspect_ratio="9:16",
        webhook_url=None,
        idempotency_key="k",
        resolution="720p",
        generate_audio=False,
        duration="5",
    )
    payload = captured["payload"]
    assert payload["duration"] == "5"
    assert set(payload) <= _SEEDANCE_T2V_FIELDS


async def test_client_i2v_payload_contract(fal_client):
    """Case 4: i2v-тело содержит image_url + generate_audio=False + resolution; без duration
    при None. Имена полей ⊆ seedance i2v."""
    provider, captured = fal_client
    styled = "portrait, " + STYLE_PROMPT_SUFFIX[VideoStyle.anime.value]
    await provider.submit_image_to_video(
        prompt=styled,
        image_url="https://cdn.local/ref.png",
        aspect_ratio="9:16",
        webhook_url=None,
        idempotency_key="k",
        resolution="720p",
        generate_audio=False,
        duration=None,
    )
    payload = captured["payload"]
    assert set(payload) <= _SEEDANCE_I2V_FIELDS
    assert payload["image_url"] == "https://cdn.local/ref.png"
    assert payload["generate_audio"] is False
    assert payload["resolution"] == "720p"
    assert payload["prompt"] == styled
    assert "duration" not in payload
    assert captured["model"] == "bytedance/seedance-2.0/image-to-video"


async def test_client_lyrics_bg_payload_contract(fal_client):
    """Case 3/4: lyrics-bg-тело = t2v-схема, модель = FAL_VIDEO_LYRICS_BG_MODEL (не visual)."""
    provider, captured = fal_client
    await provider.submit_lyrics_background(
        prompt="bg prompt, cinematic",
        aspect_ratio="9:16",
        webhook_url=None,
        idempotency_key="k",
        resolution="720p",
        generate_audio=False,
        duration=None,
    )
    payload = captured["payload"]
    assert set(payload) <= _SEEDANCE_T2V_FIELDS
    assert payload["generate_audio"] is False
    assert payload["resolution"] == "720p"
    assert "duration" not in payload
    assert captured["model"] == "vendor/lyrics-bg"


async def test_client_lipsync_payload_regression(fal_client):
    """Case 6: submit_lipsync_video НЕ тронут ADR-016 — тело только {video_url,audio_url}."""
    provider, captured = fal_client
    await provider.submit_lipsync_video(
        video_url="https://cdn.local/v.mp4",
        audio_url="https://cdn.local/a.mp3",
        webhook_url=None,
        idempotency_key="k",
    )
    payload = captured["payload"]
    assert payload == {"video_url": "https://cdn.local/v.mp4", "audio_url": "https://cdn.local/a.mp3"}
    assert "generate_audio" not in payload
    assert "resolution" not in payload
    assert "aspect_ratio" not in payload


async def test_client_avatar_image_payload_regression(fal_client):
    """Case 6: submit_avatar_image_video НЕ тронут — тело только {image_url,audio_url}."""
    provider, captured = fal_client
    await provider.submit_avatar_image_video(
        image_url="https://cdn.local/face.png",
        audio_url="https://cdn.local/a.mp3",
        webhook_url=None,
        idempotency_key="k",
    )
    payload = captured["payload"]
    assert payload == {"image_url": "https://cdn.local/face.png", "audio_url": "https://cdn.local/a.mp3"}
    assert "generate_audio" not in payload
    assert "resolution" not in payload
    assert "aspect_ratio" not in payload


# --------------------------------------------------------------------------
# _apply_style (case 2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("style", [s.value for s in VideoStyle])
def test_apply_style_appends_suffix_each_style(style):
    out = _apply_style("base prompt", style)
    assert out == f"base prompt, {STYLE_PROMPT_SUFFIX[style]}"
    assert out != "base prompt"


@pytest.mark.parametrize("style", [s.value for s in VideoStyle])
def test_apply_style_applies_with_explicit_prompt(style):
    """Стиль подмешивается ВСЕГДА, в т.ч. к явно заданному (surprise_me/prompt) промпту."""
    explicit = "user typed this cinematic dream sequence"
    assert _apply_style(explicit, style).endswith(STYLE_PROMPT_SUFFIX[style])


@pytest.mark.parametrize("style", [None, "", "   ", "unknown_style", "REALISTIC"])
def test_apply_style_noop_for_missing_or_unknown(style):
    """None / пусто / неизвестный / неверный регистр → prompt без изменений, без падения."""
    assert _apply_style("base prompt", style) == "base prompt"


# --------------------------------------------------------------------------
# _lyrics_bg_prompt (case 3) — метод не использует self, зовём напрямую
# --------------------------------------------------------------------------


def test_lyrics_bg_prompt_applies_style_with_explicit_prompt():
    out = VideoPipeline._lyrics_bg_prompt(None, {"prompt": "rainy window", "style": "cartoon"})
    assert out == f"rainy window, {STYLE_PROMPT_SUFFIX[VideoStyle.cartoon.value]}"


def test_lyrics_bg_prompt_applies_style_to_default_prompt():
    out = VideoPipeline._lyrics_bg_prompt(None, {"style": "anime"})
    assert out == f"{DEFAULT_LYRICS_BG_PROMPT}, {STYLE_PROMPT_SUFFIX[VideoStyle.anime.value]}"


def test_lyrics_bg_prompt_no_style_is_default():
    out = VideoPipeline._lyrics_bg_prompt(None, {})
    assert out == DEFAULT_LYRICS_BG_PROMPT


# --------------------------------------------------------------------------
# Pipeline-boundary: style→prompt + флаги прокидываются в fal-submit (case 1/4/5)
# --------------------------------------------------------------------------


async def test_pipeline_visual_clip_passes_style_and_flags(client, app):
    """POST visual_clip style=cartoon aspect=1:1 → submit_text_to_video получает
    generate_audio=False, resolution=720p, aspect_ratio=1:1, prompt со style-суффиксом,
    БЕЗ kwarg style, duration=None (FAL_VIDEO_MAX_DURATION дефолт None)."""
    spy = _Spy(app.state.fal_provider, "submit_text_to_video").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "neon city flythrough",
            "style": "cartoon",
            "aspectRatio": "1:1",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text

    assert len(spy.calls) == 1
    kw = spy.calls[0]
    assert kw["generate_audio"] is False
    assert kw["resolution"] == "720p"
    assert kw["aspect_ratio"] == "1:1"
    assert kw["prompt"].endswith(STYLE_PROMPT_SUFFIX[VideoStyle.cartoon.value])
    assert "neon city flythrough" in kw["prompt"]
    assert "style" not in kw  # style уходит в prompt, не отдельным полем
    assert kw["duration"] is None  # env-кап не задан → None (client отбросит поле)


async def test_pipeline_i2v_passes_style_and_flags(client, app):
    """visual_clip + referenceImageUrl → submit_image_to_video с image_url,
    generate_audio=False, resolution, style-суффикс в prompt."""
    spy = _Spy(app.state.fal_provider, "submit_image_to_video").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "slow zoom portrait",
            "referenceImageUrl": "https://cdn.local/ref.png",
            "style": "realistic",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text

    assert len(spy.calls) == 1
    kw = spy.calls[0]
    assert kw["image_url"] == "https://cdn.local/ref.png"
    assert kw["generate_audio"] is False
    assert kw["resolution"] == "720p"
    assert kw["prompt"].endswith(STYLE_PROMPT_SUFFIX[VideoStyle.realistic.value])
    assert "style" not in kw


async def test_pipeline_lyrics_bg_passes_flags(client, app):
    """lyrics_video → submit_lyrics_background с generate_audio=False, resolution, duration=None."""
    spy = _Spy(app.state.fal_provider, "submit_lyrics_background").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "lyrics_video",
            "audioUrl": "https://cdn.local/song.mp3",
            "lyrics": "line one\nline two",
            "style": "cinematic",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text

    assert len(spy.calls) == 1
    kw = spy.calls[0]
    assert kw["generate_audio"] is False
    assert kw["resolution"] == "720p"
    assert kw["duration"] is None
    assert kw["prompt"].endswith(STYLE_PROMPT_SUFFIX[VideoStyle.cinematic.value])


async def test_pipeline_duration_from_settings(client, app, monkeypatch):
    """Case 5: FAL_VIDEO_MAX_DURATION='5' → pipeline прокидывает duration='5' в submit."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "FAL_VIDEO_MAX_DURATION", "5")
    spy = _Spy(app.state.fal_provider, "submit_text_to_video").install()
    headers = await auth_headers(client)
    await grant_coins(client, headers, coins=GRANT)

    resp = await client.post(
        "/v1/videos",
        json={
            "mode": "visual_clip",
            "audioUrl": "https://cdn.local/song.mp3",
            "prompt": "p",
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    assert len(spy.calls) == 1
    assert spy.calls[0]["duration"] == "5"


# --------------------------------------------------------------------------
# ffmpeg re-encode args (case 7) — перехват _run, без реального ffmpeg
# --------------------------------------------------------------------------


def _assert_contiguous(seq: list, sub: list) -> None:
    """Утверждает, что sub встречается как непрерывная подпоследовательность seq."""
    n, m = len(seq), len(sub)
    assert any(seq[i : i + m] == sub for i in range(n - m + 1)), (
        f"{sub!r} не найдена как непрерывная подпоследовательность в {seq!r}"
    )


def test_video_encode_args_constant():
    """Case 7: обязательные параметры re-encode (ADR-016 D3) присутствуют в константе."""
    from app.domain.services.video_mux import _VIDEO_ENCODE_ARGS as args

    _assert_contiguous(args, ["-c:v", "libx264"])
    _assert_contiguous(args, ["-profile:v", "high"])
    _assert_contiguous(args, ["-level", "4.0"])
    _assert_contiguous(args, ["-pix_fmt", "yuv420p"])
    _assert_contiguous(args, ["-crf", "26"])
    _assert_contiguous(args, ["-maxrate", "2500k"])
    _assert_contiguous(args, ["-bufsize", "5000k"])
    _assert_contiguous(args, ["-c:a", "aac"])
    _assert_contiguous(args, ["-b:a", "128k"])
    _assert_contiguous(args, ["-movflags", "+faststart"])


async def test_ffmpeg_mux_loop_uses_encode_args(monkeypatch):
    """Case 7: _ffmpeg_mux_loop собирает cmd с _VIDEO_ENCODE_ARGS, scale-капом и -t."""
    from app.domain.services import video_mux

    captured: dict = {}

    async def fake_run(cmd, *, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd

    monkeypatch.setattr(video_mux, "_run", fake_run)
    await video_mux._ffmpeg_mux_loop("v.in", "a.in", "out.mp4", 42.5)

    cmd = captured["cmd"]
    _assert_contiguous(cmd, list(video_mux._VIDEO_ENCODE_ARGS))
    _assert_contiguous(cmd, ["-movflags", "+faststart"])
    _assert_contiguous(cmd, ["-maxrate", "2500k"])
    _assert_contiguous(cmd, ["-vf", video_mux._MUX_SCALE_720P])
    _assert_contiguous(cmd, ["-t", "42.500"])
    assert cmd[-1] == "out.mp4"
    assert "libx264" in cmd


@pytest.mark.parametrize("has_subs", [True, False])
async def test_ffmpeg_render_lyrics_uses_encode_args(monkeypatch, has_subs):
    """Case 7: _ffmpeg_render_lyrics (с субтитрами и без) вшивает _VIDEO_ENCODE_ARGS + faststart."""
    from app.domain.services import video_mux

    captured: dict = {}

    async def fake_run(cmd, *, cwd=None):
        captured["cmd"] = cmd

    monkeypatch.setattr(video_mux, "_run", fake_run)
    await video_mux._ffmpeg_render_lyrics(
        tmp="/tmp/lyr",
        bg_path=None,  # градиентный фон lavfi (без реального файла)
        audio_path="a.in",
        output_path="out.mp4",
        duration=30.0,
        width=720,
        height=1280,
        has_subs=has_subs,
    )
    cmd = captured["cmd"]
    _assert_contiguous(cmd, list(video_mux._VIDEO_ENCODE_ARGS))
    _assert_contiguous(cmd, ["-movflags", "+faststart"])
    _assert_contiguous(cmd, ["-crf", "26"])
    assert cmd[-1] == "out.mp4"


# --------------------------------------------------------------------------
# Stub реализует новую сигнатуру video-сабмитов (case 8)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["submit_text_to_video", "submit_lyrics_background"])
async def test_stub_video_methods_accept_new_kwargs(method):
    """DI-совместимость: стаб принимает resolution/generate_audio/duration и возвращает
    FalSubmitResult (существующие video-тесты на стабе не падают)."""
    stub = StubFalProvider(webhook_secret="s")
    res = await getattr(stub, method)(
        prompt="p",
        aspect_ratio="1:1",
        webhook_url=None,
        idempotency_key="k",
        resolution="720p",
        generate_audio=False,
        duration="5",
    )
    assert isinstance(res, FalSubmitResult)
    assert res.request_id


async def test_stub_i2v_accepts_new_kwargs():
    stub = StubFalProvider(webhook_secret="s")
    res = await stub.submit_image_to_video(
        prompt="p",
        image_url="https://cdn.local/ref.png",
        aspect_ratio="9:16",
        webhook_url=None,
        idempotency_key="k",
        resolution="720p",
        generate_audio=False,
        duration=None,
    )
    assert isinstance(res, FalSubmitResult)
    assert res.request_id
