"""Микширование music + vocal через ffmpeg.

Скачивает оба трека, микширует через `ffmpeg amix`, загружает результат
в fal storage и возвращает URL. Если ffmpeg недоступен или микс упал —
возвращает (None, None) (caller продолжит с music без vocal).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile

import httpx

logger = logging.getLogger(__name__)

DEFAULT_VOCAL_GAIN_DB = 2.0
DEFAULT_MUSIC_GAIN_DB = -3.0
MAX_DOWNLOAD_MB = 50


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def mix_music_and_vocal(
    *,
    music_url: str,
    vocal_url: str,
    upload_fn,
) -> tuple[str | None, float | None]:
    if not ffmpeg_available():
        logger.warning("ffmpeg not in PATH — skipping mix_master")
        return None, None
    with tempfile.TemporaryDirectory(prefix="mix-") as tmp:
        music_path = os.path.join(tmp, "music.input")
        vocal_path = os.path.join(tmp, "vocal.input")
        output_path = os.path.join(tmp, "mix.wav")
        try:
            await _download(music_url, music_path)
            await _download(vocal_url, vocal_path)
        except Exception as e:
            logger.warning("mix download failed: %s", e)
            return None, None
        try:
            await _ffmpeg_mix(music_path, vocal_path, output_path)
        except Exception as e:
            logger.warning("ffmpeg mix failed: %s", e)
            return None, None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning("ffmpeg produced empty file")
            return None, None
        try:
            duration = await _probe_duration(output_path)
        except Exception:
            duration = None
        with open(output_path, "rb") as f:
            content = f.read()
        try:
            url = await upload_fn(
                content=content, filename="mix.wav", content_type="audio/wav"
            )
        except Exception as e:
            logger.warning("mix upload failed: %s", e)
            return None, None
        return url, duration


async def _download(url: str, dest_path: str) -> None:
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                        raise RuntimeError(f"file > {MAX_DOWNLOAD_MB}MB")
                    f.write(chunk)


async def _ffmpeg_mix(music_path: str, vocal_path: str, output_path: str) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", music_path,
        "-i", vocal_path,
        "-filter_complex",
        f"[0:a]volume={DEFAULT_MUSIC_GAIN_DB}dB[m];"
        f"[1:a]volume={DEFAULT_VOCAL_GAIN_DB}dB[v];"
        f"[m][v]amix=inputs=2:duration=longest:normalize=0[a]",
        "-map", "[a]",
        "-ac", "2",
        "-ar", "44100",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
        return float(out.decode().strip())
    except Exception:
        return None
