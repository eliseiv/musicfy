"""seed prompt presets (genres / moods / prompt kits)

Revision ID: 0003_seed_presets
Revises: 0002_song_media_presets
Create Date: 2026-06-18

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_seed_presets"
down_revision: str | None = "0002_song_media_presets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


GENRES = [
    ("pop", "Pop", "Catchy, radio-ready"),
    ("hip_hop", "Hip-Hop", "Beats & bars"),
    ("electronic", "Electronic", "EDM, house, synth"),
    ("rock", "Rock", "Guitars & energy"),
    ("lofi", "Lo-Fi", "Chill, mellow beats"),
    ("rnb", "R&B", "Smooth & soulful"),
    ("acoustic", "Acoustic", "Unplugged & warm"),
    ("cinematic", "Cinematic", "Epic & orchestral"),
]

MOODS = [
    ("happy", "Happy", "Upbeat & bright"),
    ("sad", "Sad", "Melancholic"),
    ("energetic", "Energetic", "High tempo"),
    ("chill", "Chill", "Relaxed"),
    ("romantic", "Romantic", "Tender & warm"),
    ("dark", "Dark", "Moody & intense"),
    ("dreamy", "Dreamy", "Ethereal"),
]

PROMPTS = [
    ("summer_anthem", "Summer Anthem", "Feel-good festival energy",
     "An upbeat summer anthem with bright synths and a catchy chorus"),
    ("late_night_drive", "Late Night Drive", "Synthwave cruising vibe",
     "A moody synthwave track for a late night city drive"),
    ("heartbreak_ballad", "Heartbreak Ballad", "Emotional and slow",
     "A slow emotional piano ballad about heartbreak"),
    ("workout_pump", "Workout Pump", "Gym motivation",
     "A high-energy trap beat to power through a workout"),
    ("focus_lofi", "Focus Lo-Fi", "Study & concentrate",
     "A calm lo-fi hip-hop instrumental for focus and studying"),
]


def _rows() -> list[dict]:
    rows = []
    for i, (key, title, subtitle) in enumerate(GENRES):
        rows.append({"kind": "genre", "key": key, "title": title, "subtitle": subtitle,
                     "prompt_text": None, "sort_order": i})
    for i, (key, title, subtitle) in enumerate(MOODS):
        rows.append({"kind": "mood", "key": key, "title": title, "subtitle": subtitle,
                     "prompt_text": None, "sort_order": i})
    for i, (key, title, subtitle, prompt_text) in enumerate(PROMPTS):
        rows.append({"kind": "prompt", "key": key, "title": title, "subtitle": subtitle,
                     "prompt_text": prompt_text, "sort_order": i})
    return rows


def upgrade() -> None:
    stmt = sa.text(
        "INSERT INTO prompt_presets (kind, key, title, subtitle, prompt_text, sort_order) "
        "VALUES (CAST(:kind AS preset_kind), :key, :title, :subtitle, :prompt_text, :sort_order)"
    )
    bind = op.get_bind()
    for row in _rows():
        bind.execute(stmt, row)


def downgrade() -> None:
    op.execute("DELETE FROM prompt_presets")
