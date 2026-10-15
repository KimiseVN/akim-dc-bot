"""Manifest parser + validator (Phase 2).

Implements spec §4 (required fields), §10.2 (manifest validation),
§10.3 (speciesId regex), §10.4 (stage allowed list), §10.5 (folder
keys), §10.6 (image file validation).

The schema is the post-migration `folders{}` shape — the legacy
`assets[]` schema is rejected outright per spec §10.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Required folders per spec §10.5
REQUIRED_FOLDERS: tuple[str, ...] = (
    "01_base_moods_effects",
    "02_interaction_pack",
    "03_care_state_pack",
    "04_reward_reaction_pack",
)

ALLOWED_STAGES: tuple[str, ...] = ("baby", "teen", "adult", "mystic")

# spec §10.3
SPECIES_ID_RE = re.compile(r"^[a-z0-9_]{2,64}$")

# Allowed extensions per spec §10.1
ALLOWED_EXTS: frozenset[str] = frozenset({".png", ".webp", ".jpg", ".jpeg", ".json"})


@dataclass
class ManifestItem:
    file: str            # path inside the zip
    function: str        # function key (e.g. "moon_cat_baby_base")


@dataclass
class StageManifest:
    species_id: str
    display_name: str
    stage: str
    total_images: int
    folders: dict[str, list[ManifestItem]]


@dataclass
class ParseResult:
    """Aggregate result so callers can short-circuit on errors."""
    manifest: StageManifest | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_stage_manifest(raw: object) -> ParseResult:
    """Validate the raw manifest dict against the spec. Returns the
    parsed manifest + any errors/warnings; manifest is None when fatal."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(raw, dict):
        return ParseResult(None, ["Manifest must be a JSON object."])

    # Reject legacy schema explicitly (spec §10.2)
    if "assets" in raw and "folders" not in raw:
        return ParseResult(
            None,
            [
                "❌ Legacy manifest schema `assets[]` is no longer supported. "
                "Please use `folders{}`."
            ],
        )

    # Required top-level fields
    required = ("speciesId", "displayName", "stage", "total_images", "folders")
    missing = [k for k in required if k not in raw]
    if missing:
        errors.append(
            f"Manifest missing field: {', '.join('`' + k + '`' for k in missing)}"
        )
        return ParseResult(None, errors, warnings)

    species_id = str(raw["speciesId"]).strip()
    display_name = str(raw["displayName"]).strip()
    stage = str(raw["stage"]).strip()
    total_images_raw = raw["total_images"]
    folders_raw = raw["folders"]

    # species_id
    if not SPECIES_ID_RE.match(species_id):
        errors.append(
            f"Invalid `speciesId`: `{species_id}`. "
            "Requires lowercase snake_case [a-z0-9_], 2-64 chars."
        )

    # display_name
    if not display_name or len(display_name) > 128:
        errors.append("`displayName` must be 1-128 characters.")

    # stage
    if stage not in ALLOWED_STAGES:
        errors.append(
            f"Invalid `stage`: `{stage}`. "
            f"Allowed: {', '.join(ALLOWED_STAGES)}"
        )

    # total_images
    try:
        total_images = int(total_images_raw)
    except (TypeError, ValueError):
        errors.append("`total_images` must be an integer.")
        total_images = 0

    # folders
    if not isinstance(folders_raw, dict):
        errors.append("`folders` must be a key→list object.")
        return ParseResult(None, errors, warnings)

    folders: dict[str, list[ManifestItem]] = {}
    for folder_id, entries in folders_raw.items():
        if folder_id not in REQUIRED_FOLDERS:
            warnings.append(
                f"Folder `{folder_id}` is not one of the 4 canonical folders — "
                f"skipped. Allowed: {', '.join(REQUIRED_FOLDERS)}"
            )
            continue
        if not isinstance(entries, list):
            errors.append(f"Folder `{folder_id}` must be an array.")
            continue
        items: list[ManifestItem] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(
                    f"Folder `{folder_id}` item #{i}: must be an object"
                )
                continue
            file_path = str(entry.get("file", "")).strip()
            fn = str(entry.get("function", "")).strip()
            if not file_path or not fn:
                errors.append(
                    f"Folder `{folder_id}` item #{i}: missing `file` "
                    f"or `function`"
                )
                continue
            # spec §10.6 — file path must start with folder key
            if not file_path.startswith(f"{folder_id}/"):
                errors.append(
                    f"Folder `{folder_id}` item #{i}: `file` "
                    f"`{file_path}` doesn't start with `{folder_id}/`"
                )
                continue
            ext = "." + file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
            if ext not in ALLOWED_EXTS:
                errors.append(
                    f"Folder `{folder_id}` item #{i}: extension `{ext}` "
                    f"is not allowed. Allowed: {', '.join(ALLOWED_EXTS)}"
                )
                continue
            items.append(ManifestItem(file=file_path, function=fn))
        folders[folder_id] = items

    # Missing folders
    for needed in REQUIRED_FOLDERS:
        if needed not in folders:
            errors.append(f"Missing required folder `{needed}`")

    if errors:
        return ParseResult(None, errors, warnings)

    manifest = StageManifest(
        species_id=species_id,
        display_name=display_name,
        stage=stage,
        total_images=total_images,
        folders=folders,
    )
    return ParseResult(manifest, errors, warnings)


def is_safe_zip_path(path: str) -> bool:
    """spec §10.1 — reject path traversal + absolute paths inside zip."""
    p = path.replace("\\", "/")
    parts = p.split("/")
    if any(part == ".." for part in parts):
        return False
    if p.startswith("/"):
        return False
    if len(p) > 1 and p[1] == ":":  # windows drive letter
        return False
    return True
