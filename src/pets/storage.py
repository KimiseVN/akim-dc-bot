"""File storage layer for Phase 2 (Railway volume).

The bot is expected to run with a persistent volume mounted; the path
is taken from the PET_STORAGE_PATH env var (default `/data/pets` which
matches Railway's typical volume mount). For local dev when no volume
exists, set PET_STORAGE_PATH=./data/pets.

Per spec §14, the layout under the base path is:

  guilds/{guild_id}/pets/{species_id}/{stage}/{pack_id}/{asset_type}_{state_key}.{ext}

Files are written atomically (tmp + rename) so a crash mid-write never
leaves a half-written image on disk.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("akim-bot.pets.storage")


_DEFAULT_BASE = "/data/pets"


def _resolve_base() -> Path:
    raw = os.getenv("PET_STORAGE_PATH", _DEFAULT_BASE)
    p = Path(raw).expanduser().resolve()
    return p


class PetStorage:
    """Filesystem layer for pet assets. Single base directory + a
    deterministic path-builder helper. No mutation hidden behind the
    Path object — callers pass in bytes, we write them."""

    def __init__(self, base: Path | None = None) -> None:
        self.base = (base or _resolve_base())
        # Don't crash on missing base — Railway mounts the volume only
        # at deploy. Create-on-write means first import will mkdir.
        log.info("pet storage base: %s", self.base)

    def asset_path(
        self,
        guild_id: int, species_id: str, stage: str,
        pack_id: str, asset_type: str, state_key: str, ext: str,
    ) -> Path:
        """Build the canonical filesystem path. ext should be lowercase
        with leading dot (e.g. '.png')."""
        ext = ext if ext.startswith(".") else f".{ext}"
        return (
            self.base
            / "guilds" / str(guild_id)
            / "pets" / species_id / stage / pack_id
            / f"{asset_type}_{state_key}{ext}"
        )

    def write_bytes(self, dest: Path, data: bytes) -> int:
        """Atomic write: tmp file + rename. Returns bytes written."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return len(data)

    def delete(self, dest: Path) -> bool:
        """Idempotent delete. Returns True if file existed."""
        try:
            dest.unlink()
            return True
        except FileNotFoundError:
            return False

    def disable_species(self, guild_id: int, species_id: str) -> int:
        """For mode='replace': move the entire species folder to a
        .disabled-<ts> sibling so the bytes aren't lost but the live
        path is freed for the new import.

        Returns number of files moved (or 0 if folder didn't exist).
        """
        import time
        src = self.base / "guilds" / str(guild_id) / "pets" / species_id
        if not src.exists():
            return 0
        ts = int(time.time())
        dst = src.with_name(f"{species_id}.disabled-{ts}")
        try:
            shutil.move(str(src), str(dst))
        except OSError as e:
            log.warning("disable_species: move failed: %s", e)
            return 0
        # Count files in the moved tree for the audit log
        count = sum(1 for _ in dst.rglob("*") if _.is_file())
        log.info(
            "disable_species %s/%s — moved %d files to %s",
            guild_id, species_id, count, dst,
        )
        return count
