"""Ручной реальный E2E-прогон через настоящий fal.ai (без stub).

Запуск:
  DATABASE_URL="postgresql+asyncpg://musicfy:musicfy@localhost:5544/musicfy" \
    .venv/Scripts/python.exe scripts/real_e2e.py

Требует в .env: FAL_USE_STUB=false, FAL_API_KEY=<ключ>. Результаты приходят через
встроенный poller (PUBLIC_BASE_URL можно не задавать).
"""
from __future__ import annotations

import asyncio
import os
import time

# Скрипт грантит подписку синтетическим StoreKit-токеном — отключаем проверку подписи.
os.environ.setdefault("APPLE_STOREKIT_VERIFY_SIGNATURE", "false")

import jwt
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app


def weekly_sub_token() -> str:
    expires_ms = int((time.time() + 7 * 86400) * 1000)
    return jwt.encode(
        {
            "transactionId": f"real-e2e-{int(time.time())}",
            "originalTransactionId": f"real-e2e-{int(time.time())}",
            "productId": "com.musicfy.sub.weekly",
            "type": "Auto-Renewable Subscription",
            "expiresDate": expires_ms,
        },
        "test-key",
        algorithm="HS256",
    )


async def poll(client, headers, job_id, *, timeout=300, interval=6):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        j = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
        st = j["status"]
        if st != last:
            print(f"  [{int(time.monotonic())%10000:>4}s] status={st} stage={j.get('currentStage')}")
            last = st
        if st in ("completed", "failed", "canceled"):
            return j
        await asyncio.sleep(interval)
    return {"status": "TIMEOUT"}


async def main():
    settings = get_settings()
    print(f"FAL_USE_STUB={settings.FAL_USE_STUB}  song_model={settings.FAL_SONG_MODEL}")
    app = create_app(settings)
    async with LifespanManager(app, startup_timeout=60):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:
            token = (await c.post("/v1/auth/guest", json={})).json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            print("guest:", headers["Authorization"][:24], "...")

            sub = await c.post(
                "/v1/billing/purchases/verify",
                json={"signedTransaction": weekly_sub_token()},
                headers=headers,
            )
            print("grant sub:", sub.status_code, sub.json())

            # 1) Реальный lyrics (LLM)
            print("\n=== LYRICS (real LLM) ===")
            t0 = time.monotonic()
            ly = await c.post(
                "/v1/lyrics",
                json={"prompt": "a hopeful song about a summer road trip", "language": "en"},
                headers=headers,
            )
            print("lyrics status:", ly.status_code, f"({time.monotonic()-t0:.1f}s)")
            if ly.status_code == 200:
                content = ly.json()["content"]
                print("--- lyrics ---\n" + content[:400] + ("..." if len(content) > 400 else ""))

            # 2) Реальная песня (minimax-music/v2.6) через polling
            print("\n=== SONG (real minimax-music/v2.6) ===")
            t0 = time.monotonic()
            song = await c.post(
                "/v1/songs",
                json={
                    "prompt": "upbeat indie pop, summer road trip, bright synths, catchy chorus",
                    "genre": "pop",
                    "mood": "happy",
                    "lyricsPrompt": "a hopeful song about a summer road trip",
                },
                headers=headers,
            )
            print("create song:", song.status_code, song.json())
            if song.status_code == 202:
                job_id = song.json()["jobId"]
                result = await poll(c, headers, job_id, timeout=300)
                print(f"final status: {result['status']}  ({time.monotonic()-t0:.1f}s total)")
                if result["status"] == "completed":
                    track = (await c.get(f"/v1/tracks/{result['trackId']}", headers=headers)).json()
                    v = track["variants"][0]
                    print("TRACK audioUrl:", v["audioUrl"])
                    print("duration:", v["durationSeconds"])
                elif result["status"] == "failed":
                    print("error:", result.get("errorCode"), result.get("errorMessage"))
                    print("pipeline:", [(s["stage"], s["status"]) for s in result.get("pipeline", [])])


if __name__ == "__main__":
    asyncio.run(main())
