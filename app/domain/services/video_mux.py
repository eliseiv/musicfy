"""ffmpeg-хелперы для видео-пайплайна (ADR-007).

Два синхронных (в рамках стадии) сценария поверх ffmpeg, образец — `audio_mixer.py`:

- `mux_audio_into_video` — вшить аудио-трек в сгенерированный клип. fal t2v/i2v выдаёт
  клип на секунды, трек — на минуты; поэтому **границей длительности берётся аудио**, а
  короткое видео **зацикливается** (`-stream_loop -1`) под длину трека (иначе `-shortest`
  на видео обрезал бы результат до пары секунд).
- `render_lyrics_video` — фон (статический градиент V1 или переданный background_url) +
  бёрн-ин строк лирики (ffmpeg subtitles), тайминги V1 — равномерно по длительности трека.

При отсутствии/сбое ffmpeg функции возвращают (None, None) — caller деградирует
(`quality_flag`), как `cover._mix`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile

import httpx

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_MB = 200
DEFAULT_DURATION_SECONDS = 30.0
MAX_LYRIC_LINES = 200

# Размеры фона lyrics-video по соотношению сторон (best-effort, [RISK-B3]).
_ASPECT_TO_SIZE = {
    "1:1": (1080, 1080),
    "3:4": (1080, 1440),
    "4:3": (1440, 1080),
    "9:16": (1080, 1920),
}
_DEFAULT_SIZE = (1080, 1920)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def mux_audio_into_video(
    *,
    video_url: str,
    audio_url: str,
    upload_fn,
) -> tuple[str | None, float | None]:
    """Зацикливает короткий клип под длину аудио и муксит трек. Возврат: (url, duration)."""
    if not ffmpeg_available():
        logger.warning("ffmpeg not in PATH — skipping mux_audio")
        return None, None
    with tempfile.TemporaryDirectory(prefix="vmux-") as tmp:
        video_path = os.path.join(tmp, "clip.input")
        audio_path = os.path.join(tmp, "audio.input")
        output_path = os.path.join(tmp, "out.mp4")
        try:
            await _download(video_url, video_path)
            await _download(audio_url, audio_path)
        except Exception as e:
            logger.warning("mux download failed: %s", e)
            return None, None

        audio_duration = await _probe_duration(audio_path)
        try:
            await _ffmpeg_mux_loop(video_path, audio_path, output_path, audio_duration)
        except Exception as e:
            logger.warning("ffmpeg mux failed: %s", e)
            return None, None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning("ffmpeg mux produced empty file")
            return None, None
        duration = await _probe_duration(output_path) or audio_duration
        try:
            with open(output_path, "rb") as f:
                content = f.read()
            url = await upload_fn(
                content=content, filename="video.mp4", content_type="video/mp4"
            )
        except Exception as e:
            logger.warning("mux upload failed: %s", e)
            return None, None
        return url, duration


async def render_lyrics_video(
    *,
    background_url: str | None,
    audio_url: str,
    lyrics_lines: list[str],
    aspect_ratio: str | None,
    upload_fn,
) -> tuple[str | None, float | None]:
    """Рендерит lyrics-video: фон + бёрн-ин строк + аудио. Возврат: (url, duration)."""
    if not ffmpeg_available():
        logger.warning("ffmpeg not in PATH — skipping lyrics_render")
        return None, None
    width, height = _ASPECT_TO_SIZE.get(aspect_ratio or "", _DEFAULT_SIZE)
    with tempfile.TemporaryDirectory(prefix="lyr-") as tmp:
        audio_path = os.path.join(tmp, "audio.input")
        bg_path = os.path.join(tmp, "bg.input") if background_url else None
        sub_path = os.path.join(tmp, "subs.srt")
        output_path = os.path.join(tmp, "out.mp4")
        try:
            await _download(audio_url, audio_path)
            if background_url and bg_path:
                await _download(background_url, bg_path)
        except Exception as e:
            logger.warning("lyrics download failed: %s", e)
            return None, None

        duration = await _probe_duration(audio_path) or DEFAULT_DURATION_SECONDS
        _write_srt(sub_path, lyrics_lines, duration)
        try:
            await _ffmpeg_render_lyrics(
                tmp=tmp,
                bg_path=bg_path,
                audio_path=audio_path,
                output_path=output_path,
                duration=duration,
                width=width,
                height=height,
                has_subs=bool(lyrics_lines),
            )
        except Exception as e:
            logger.warning("ffmpeg lyrics render failed: %s", e)
            return None, None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning("lyrics render produced empty file")
            return None, None
        out_duration = await _probe_duration(output_path) or duration
        try:
            with open(output_path, "rb") as f:
                content = f.read()
            url = await upload_fn(
                content=content, filename="lyrics.mp4", content_type="video/mp4"
            )
        except Exception as e:
            logger.warning("lyrics upload failed: %s", e)
            return None, None
        return url, out_duration


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------


async def _ffmpeg_mux_loop(
    video_path: str, audio_path: str, output_path: str, audio_duration: float | None
) -> None:
    # -stream_loop -1: бесконечно повторяем видеовход; аудио — конечное → -shortest
    # останавливает вывод по концу аудио. -t дублирует границу, если длительность известна.
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
    ]
    if audio_duration and audio_duration > 0:
        cmd += ["-t", f"{audio_duration:.3f}"]
    cmd.append(output_path)
    await _run(cmd)


async def _ffmpeg_render_lyrics(
    *,
    tmp: str,
    bg_path: str | None,
    audio_path: str,
    output_path: str,
    duration: float,
    width: int,
    height: int,
    has_subs: bool,
) -> None:
    cmd = ["ffmpeg", "-y"]
    if bg_path:
        # Фон-видео зацикливаем под длину трека; изображение так же (decoder отдаёт кадры).
        cmd += ["-stream_loop", "-1", "-i", bg_path]
        bg_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    else:
        # Статический градиент V1 (lavfi): тёмный вертикальный градиент.
        cmd += [
            "-f", "lavfi",
            "-i", f"gradients=s={width}x{height}:c0=0x0a1020:c1=0x102840:d={duration:.3f}",
        ]
        bg_filter = f"scale={width}:{height}"
    cmd += ["-i", audio_path]

    vf = bg_filter
    if has_subs:
        # cwd=tmp → путь субтитров относительный ('subs.srt'), без экранирования двоеточий.
        vf += (
            ",subtitles=subs.srt:force_style='Alignment=10,FontSize=28,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,Outline=2,Shadow=1'"
        )
    cmd += [
        "-map", "0:v", "-map", "1:a",
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
        "-shortest",
        output_path,
    ]
    await _run(cmd, cwd=tmp)


async def _run(cmd: list[str], *, cwd: str | None = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg exit={proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')[:500]}"
        )


async def _probe_duration(path: str) -> float | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        value = float(out.decode().strip())
        return value if value > 0 else None
    except Exception:
        return None


async def _download(url: str, dest_path: str) -> None:
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                        raise RuntimeError(f"file > {MAX_DOWNLOAD_MB}MB")
                    f.write(chunk)


# --------------------------------------------------------------------------
# SRT (равномерная V1-синхронизация)
# --------------------------------------------------------------------------


def split_lyric_lines(lyrics: str | None) -> list[str]:
    """Разбивает лирику на строки для бёрн-ина: без пустых и без секций [Verse]/[Chorus]."""
    if not lyrics:
        return []
    lines: list[str] = []
    for raw in lyrics.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        lines.append(line)
        if len(lines) >= MAX_LYRIC_LINES:
            break
    return lines


def _write_srt(path: str, lines: list[str], duration: float) -> None:
    if not lines:
        # Пустой файл — subtitles-фильтр не подключается (has_subs=False у caller).
        with open(path, "w", encoding="utf-8"):
            pass
        return
    per = max(0.5, duration / len(lines))
    with open(path, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines):
            start = i * per
            end = min(duration, (i + 1) * per)
            f.write(f"{i + 1}\n")
            f.write(f"{_ts(start)} --> {_ts(end)}\n")
            f.write(f"{_srt_escape(line)}\n\n")


def _ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000))
    hh, millis = divmod(millis, 3_600_000)
    mm, millis = divmod(millis, 60_000)
    ss, millis = divmod(millis, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{millis:03d}"


def _srt_escape(text: str) -> str:
    # SRT-текст plain; убираем управляющие фигурные скобки ASS-override на всякий случай.
    return text.replace("{", "(").replace("}", ")")
