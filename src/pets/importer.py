"""Zip → preview / import orchestration for pet asset packs.

Two entry points:

  scan_pack(zip_bytes, mode='preview')  → ScanResult
    Pure read; never writes DB or storage. Used by /pet-admin
    preview-pack and the previewing step of /pet-admin import-pack.

  perform_import(scan, mode, guild_id, importer_id, services)
    Applies the scan to storage + DB. Called after the user clicks
    Confirm on the preview embed.

The zip flow is in-process (no Celery/jobs); the user's interaction
holds the temp-extracted files in memory only long enough to scan or
import. Persistence between preview and confirm is via the
PetAssetImportJob row.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import PetAsset, PetAssetImportJob, PetSpecies
from .classifier import parse_asset_function
from .manifest import (
    ALLOWED_EXTS,
    ALLOWED_STAGES,
    REQUIRED_FOLDERS,
    StageManifest,
    is_safe_zip_path,
    parse_stage_manifest,
)
from .storage import PetStorage

log = logging.getLogger("akim-bot.pets.importer")


# Hard cap so a malicious zip can't OOM the bot. Per-guild cap is
# usually tighter via PetGuildConfig.max_zip_size_mb.
_HARD_MAX_BYTES = 200 * 1024 * 1024  # 200 MB


@dataclass
class _PlannedAsset:
    """One asset row planned for write. Filled by scan_pack, consumed
    by perform_import."""
    stage: str
    pack_id: str
    function_key: str
    asset_type: str
    state_key: str
    original_path: str
    filename: str
    raw_bytes: bytes
    mime_type: str
    checksum: str
    width: int | None = None
    height: int | None = None


@dataclass
class ScanResult:
    """Aggregate of one zip scan. Status is implicit: errors non-empty
    means invalid pack; otherwise importable."""
    species_id: str | None = None
    display_name: str | None = None
    stages_seen: list[str] = field(default_factory=list)
    planned: list[_PlannedAsset] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Set by the full-pack manifest path when present
    rarity: str | None = None
    price: int | None = None
    concept: str | None = None

    @property
    def is_valid(self) -> bool:
        return not self.errors and bool(self.planned)


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext_from_name(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _validate_zip_bytes(
    raw: bytes, max_size_bytes: int, max_files: int,
) -> tuple[zipfile.ZipFile | None, list[str]]:
    """Open the zip after format + size + path-safety checks."""
    errors: list[str] = []
    if len(raw) > max_size_bytes:
        errors.append(
            f"ZIP exceeds the {max_size_bytes // (1024 * 1024)} MB limit."
        )
        return None, errors
    if len(raw) > _HARD_MAX_BYTES:
        errors.append(f"ZIP exceeds the {_HARD_MAX_BYTES // (1024 * 1024)} MB hard cap.")
        return None, errors
    bio = io.BytesIO(raw)
    if not zipfile.is_zipfile(bio):
        errors.append("File is not a valid ZIP archive.")
        return None, errors
    bio.seek(0)
    try:
        zf = zipfile.ZipFile(bio)
    except zipfile.BadZipFile as e:
        errors.append(f"Corrupt ZIP: {e}")
        return None, errors
    names = zf.namelist()
    if not names:
        errors.append("ZIP is empty.")
        zf.close()
        return None, errors
    if len(names) > max_files:
        errors.append(
            f"ZIP has {len(names)} files, exceeding the {max_files} limit."
        )
        zf.close()
        return None, errors
    for name in names:
        if not is_safe_zip_path(name):
            errors.append(
                f"Unsafe path inside ZIP: `{name}` "
                "(path traversal / absolute path)."
            )
            zf.close()
            return None, errors
        if name.endswith("/"):  # directory entry, skip extension check
            continue
        ext = _ext_from_name(name)
        if ext and ext not in ALLOWED_EXTS:
            errors.append(
                f"File `{name}` has extension `{ext}` which is not allowed. "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTS))}"
            )
            zf.close()
            return None, errors
    return zf, errors


def _try_load_full_pack_manifest(zf: zipfile.ZipFile) -> dict | None:
    """`pet-pack.manifest.json` is optional per spec §6."""
    for name in zf.namelist():
        if name.rstrip("/").endswith("pet-pack.manifest.json"):
            with zf.open(name) as fh:
                try:
                    return json.loads(fh.read().decode("utf-8"))
                except Exception:
                    return None
    return None


def _list_stage_manifests(zf: zipfile.ZipFile) -> dict[str, str]:
    """Find each stage's manifest.json. Returns {stage: path_in_zip}."""
    found: dict[str, str] = {}
    for name in zf.namelist():
        parts = name.split("/")
        if (
            len(parts) >= 2
            and parts[0] in ALLOWED_STAGES
            and parts[-1] == "manifest.json"
        ):
            found[parts[0]] = name
    return found


def _read_single_stage_manifest(zf: zipfile.ZipFile) -> str | None:
    """For single-stage zip: top-level manifest.json (no stage folder)."""
    for name in zf.namelist():
        if name == "manifest.json":
            return name
    return None


def _ingest_stage(
    zf: zipfile.ZipFile, stage_manifest: StageManifest,
    manifest_dir: str, scan: ScanResult,
) -> None:
    """Pull every asset declared in stage_manifest from the zip and
    append to scan.planned."""
    for folder_id, items in stage_manifest.folders.items():
        for item in items:
            zip_path = (
                f"{manifest_dir}{item.file}" if manifest_dir
                else item.file
            )
            try:
                with zf.open(zip_path) as fh:
                    data = fh.read()
            except KeyError:
                scan.errors.append(
                    f"[{stage_manifest.stage}/{folder_id}] "
                    f"Image file listed in manifest is not in the ZIP: "
                    f"`{zip_path}`"
                )
                continue
            ext = _ext_from_name(zip_path)
            if ext not in ALLOWED_EXTS:
                scan.errors.append(
                    f"[{stage_manifest.stage}/{folder_id}] "
                    f"`{zip_path}` extension is not allowed."
                )
                continue
            try:
                asset_type, state_key = parse_asset_function(
                    item.function,
                    stage_manifest.species_id,
                    stage_manifest.stage,
                )
            except ValueError as e:
                scan.errors.append(
                    f"[{stage_manifest.stage}/{folder_id}] {e}"
                )
                continue
            if asset_type == "unknown":
                scan.warnings.append(
                    f"[{stage_manifest.stage}/{folder_id}] "
                    f"`{item.function}` is not in the canonical map — "
                    "importing anyway with asset_type='unknown'."
                )
            mime, _ = mimetypes.guess_type(item.file)
            mime = mime or "application/octet-stream"
            scan.planned.append(
                _PlannedAsset(
                    stage=stage_manifest.stage,
                    pack_id=folder_id,
                    function_key=item.function,
                    asset_type=asset_type,
                    state_key=state_key,
                    original_path=zip_path,
                    filename=zip_path.rsplit("/", 1)[-1],
                    raw_bytes=data,
                    mime_type=mime,
                    checksum=_checksum(data),
                )
            )


def scan_pack(
    zip_bytes: bytes,
    *,
    is_single_stage: bool,
    max_size_bytes: int,
    max_files: int,
) -> ScanResult:
    """Read-only zip scan. Validates structure + classifies assets but
    does not touch DB or storage."""
    scan = ScanResult()
    zf, zip_errors = _validate_zip_bytes(zip_bytes, max_size_bytes, max_files)
    if zip_errors:
        scan.errors.extend(zip_errors)
        return scan
    assert zf is not None
    try:
        if is_single_stage:
            # Single-stage zip: manifest.json at root + 4 folders.
            mpath = _read_single_stage_manifest(zf)
            if mpath is None:
                scan.errors.append("ZIP has no `manifest.json` at root.")
                return scan
            with zf.open(mpath) as fh:
                try:
                    raw = json.loads(fh.read().decode("utf-8"))
                except Exception as e:
                    scan.errors.append(f"manifest.json is not valid JSON: {e}")
                    return scan
            parse = parse_stage_manifest(raw)
            scan.warnings.extend(parse.warnings)
            if parse.manifest is None:
                scan.errors.extend(parse.errors)
                return scan
            sm = parse.manifest
            scan.species_id = sm.species_id
            scan.display_name = sm.display_name
            scan.stages_seen.append(sm.stage)
            _ingest_stage(zf, sm, manifest_dir="", scan=scan)
            return scan

        # Full pack: optional pet-pack.manifest.json + 4 stage folders
        full = _try_load_full_pack_manifest(zf)
        if isinstance(full, dict):
            scan.species_id = str(full.get("speciesId") or "").strip() or None
            scan.display_name = (
                str(full.get("displayName") or "").strip() or None
            )
            scan.rarity = (str(full.get("rarity") or "").strip() or None)
            scan.price = (
                int(full["price"]) if isinstance(full.get("price"), (int, float))
                else None
            )
            scan.concept = (str(full.get("concept") or "").strip() or None)

        stage_manifests = _list_stage_manifests(zf)
        if not stage_manifests:
            scan.errors.append(
                "No `<stage>/manifest.json` found in the ZIP."
            )
            return scan
        for stage in ALLOWED_STAGES:
            if stage not in stage_manifests:
                scan.warnings.append(f"Missing stage `{stage}`.")
        for stage, mpath in stage_manifests.items():
            with zf.open(mpath) as fh:
                try:
                    raw = json.loads(fh.read().decode("utf-8"))
                except Exception as e:
                    scan.errors.append(
                        f"`{mpath}` is not valid JSON: {e}"
                    )
                    continue
            parse = parse_stage_manifest(raw)
            scan.warnings.extend(parse.warnings)
            if parse.manifest is None:
                scan.errors.extend(parse.errors)
                continue
            sm = parse.manifest
            # Cross-check species_id consistency
            if scan.species_id is None:
                scan.species_id = sm.species_id
            elif scan.species_id != sm.species_id:
                scan.errors.append(
                    f"speciesId mismatch across stages: "
                    f"`{scan.species_id}` vs `{sm.species_id}` "
                    f"({mpath})"
                )
                continue
            if scan.display_name is None:
                scan.display_name = sm.display_name
            scan.stages_seen.append(sm.stage)
            manifest_dir = mpath.rsplit("/", 1)[0] + "/"  # e.g. "baby/"
            _ingest_stage(zf, sm, manifest_dir=manifest_dir, scan=scan)
        return scan
    finally:
        zf.close()


async def perform_import(
    session: AsyncSession,
    storage: PetStorage,
    scan: ScanResult,
    *,
    guild_id: int,
    uploaded_by: int,
    mode: str,  # create | update | replace
    job: PetAssetImportJob,
) -> tuple[int, int]:
    """Apply the scan to storage + DB. Updates the supplied
    PetAssetImportJob row in place. Returns (imported, failed).

    Caller is responsible for committing the session at the end."""
    if not scan.is_valid or scan.species_id is None:
        job.status = "FAILED"
        job.errors_json = json.dumps(scan.errors, ensure_ascii=False)
        return 0, len(scan.planned)

    species_id = scan.species_id
    species = await session.scalar(
        select(PetSpecies).where(
            PetSpecies.guild_id == guild_id,
            PetSpecies.species_id == species_id,
        )
    )

    if mode == "create" and species is not None:
        job.status = "FAILED"
        job.errors_json = json.dumps(
            [f"Species `{species_id}` already exists — mode=create won't overwrite."],
            ensure_ascii=False,
        )
        return 0, len(scan.planned)

    if mode == "replace" and species is not None:
        # Disable old asset rows + move old files aside
        moved = storage.disable_species(guild_id, species_id)
        log.info("replace mode: moved %d old files for %s", moved, species_id)
        old_assets = await session.execute(
            select(PetAsset).where(
                PetAsset.guild_id == guild_id,
                PetAsset.species_id == species_id,
            )
        )
        for asset in old_assets.scalars().all():
            asset.is_active = False

    # Create / fetch species
    if species is None:
        species = PetSpecies(
            guild_id=guild_id,
            species_id=species_id,
            display_name=scan.display_name or species_id,
            rarity=scan.rarity or "common",
            price=scan.price or 500,
            concept=scan.concept,
            is_shop_enabled=False,
            is_active=True,
            imported_by=uploaded_by,
            imported_at=datetime.now(timezone.utc),
        )
        session.add(species)
        await session.flush()
    else:
        # update metadata from the pack manifest if it had any
        if scan.display_name:
            species.display_name = scan.display_name
        if scan.rarity:
            species.rarity = scan.rarity
        if scan.price is not None:
            species.price = scan.price
        if scan.concept:
            species.concept = scan.concept
        species.imported_by = uploaded_by
        species.imported_at = datetime.now(timezone.utc)

    imported = 0
    failed = 0
    for planned in scan.planned:
        try:
            ext = _ext_from_name(planned.original_path)
            dest = storage.asset_path(
                guild_id=guild_id,
                species_id=species_id,
                stage=planned.stage,
                pack_id=planned.pack_id,
                asset_type=planned.asset_type,
                state_key=planned.state_key,
                ext=ext,
            )
            size = storage.write_bytes(dest, planned.raw_bytes)
        except Exception as e:
            log.exception("import: write failed for %s", planned.original_path)
            failed += 1
            scan.warnings.append(
                f"Save fail `{planned.original_path}`: {e}"
            )
            continue

        # Upsert PetAsset row by composite unique key
        existing = await session.scalar(
            select(PetAsset).where(
                PetAsset.guild_id == guild_id,
                PetAsset.species_id == species_id,
                PetAsset.stage == planned.stage,
                PetAsset.asset_type == planned.asset_type,
                PetAsset.state_key == planned.state_key,
            )
        )
        if existing is None:
            session.add(PetAsset(
                guild_id=guild_id,
                species_id=species_id,
                stage=planned.stage,
                pack_id=planned.pack_id,
                asset_type=planned.asset_type,
                state_key=planned.state_key,
                function_key=planned.function_key,
                filename=planned.filename,
                original_path=planned.original_path,
                storage_path=str(dest),
                mime_type=planned.mime_type,
                file_size=size,
                checksum=planned.checksum,
                is_active=True,
            ))
        else:
            existing.pack_id = planned.pack_id
            existing.function_key = planned.function_key
            existing.filename = planned.filename
            existing.original_path = planned.original_path
            existing.storage_path = str(dest)
            existing.mime_type = planned.mime_type
            existing.file_size = size
            existing.checksum = planned.checksum
            existing.is_active = True
        imported += 1

    # Update species counters from ALL currently active assets — NOT
    # from scan.planned, which only contains the current import's
    # stages. A single-stage import would otherwise overwrite the
    # stage_count back to 1 even when prior imports already populated
    # other stages.
    from sqlalchemy import func as _func
    stages_q = await session.execute(
        select(PetAsset.stage).where(
            PetAsset.guild_id == guild_id,
            PetAsset.species_id == species_id,
            PetAsset.is_active.is_(True),
        ).distinct()
    )
    species.stage_count = len({row[0] for row in stages_q.all()})
    active_rows = await session.scalar(
        select(_func.count(PetAsset.id)).where(
            PetAsset.guild_id == guild_id,
            PetAsset.species_id == species_id,
            PetAsset.is_active.is_(True),
        )
    )
    species.asset_count = int(active_rows or 0)

    job.status = "SUCCESS"
    job.species_id = species_id
    job.display_name = species.display_name
    job.total_assets = len(scan.planned)
    job.imported_assets = imported
    job.failed_assets = failed
    job.completed_at = datetime.now(timezone.utc)
    if scan.warnings:
        job.warnings_json = json.dumps(scan.warnings, ensure_ascii=False)

    return imported, failed


def write_zip_to_tempfile(zip_bytes: bytes) -> Path:
    """Persist the uploaded zip to a temp file so the Confirm button
    can re-read it later (the original attachment URL is short-lived)."""
    tmp_dir = Path(tempfile.gettempdir()) / "akim-pet-imports"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    handle, path = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    import os as _os
    _os.close(handle)
    Path(path).write_bytes(zip_bytes)
    return Path(path)
