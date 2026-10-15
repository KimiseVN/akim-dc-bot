"""Pydantic v2 request/response models for the desktop API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LinkCreateResponse(BaseModel):
    code: str
    expires_at: datetime = Field(serialization_alias="expiresAt")

    model_config = {"populate_by_name": True}


class LinkConfirmRequest(BaseModel):
    code: str
    discord_user_id: str = Field(validation_alias="discordUserId")
    guild_id: str = Field(validation_alias="guildId")

    model_config = {"populate_by_name": True}


class LinkConfirmResponse(BaseModel):
    code: str
    discord_user_id: str = Field(serialization_alias="discordUserId")
    guild_id: str = Field(serialization_alias="guildId")

    model_config = {"populate_by_name": True}


class LinkStatusResponse(BaseModel):
    status: str  # pending | linked | expired | consumed
    access_token: str | None = Field(
        default=None, serialization_alias="accessToken",
    )
    linked_at: datetime | None = Field(
        default=None, serialization_alias="linkedAt",
    )

    model_config = {"populate_by_name": True}


class ApiError(BaseModel):
    error: bool = True
    code: str
    message: str


# === Pet endpoints ==========================================================


class PetAssetInfo(BaseModel):
    asset_id: str | None = Field(serialization_alias="assetId")
    display_name: str = Field(serialization_alias="displayName")
    local_path: str | None = Field(default=None, serialization_alias="localPath")
    manifest_url: str | None = Field(default=None, serialization_alias="manifestUrl")
    spritesheet_url: str | None = Field(default=None, serialization_alias="spritesheetUrl")
    version: str | None = None

    model_config = {"populate_by_name": True}


class UserPetSummary(BaseModel):
    """Trimmed pet view used in /owned listings."""
    id: int
    name: str
    pet_type_id: str = Field(serialization_alias="petTypeId")
    asset_id: str | None = Field(serialization_alias="assetId")
    display_name: str = Field(serialization_alias="displayName")
    level: int
    stage: str
    is_active: bool = Field(serialization_alias="isActive")

    model_config = {"populate_by_name": True}


class OwnedPetsResponse(BaseModel):
    pets: list[UserPetSummary]


class UserPetInstance(BaseModel):
    """Full pet view used in /active + /select responses + action
    responses. Mirrors SPEC §9 with bot stat-semantics preserved
    (hunger/thirst = fullness, high = good)."""
    id: int
    owner_discord_id: str = Field(serialization_alias="ownerDiscordId")
    name: str
    pet_type_id: str = Field(serialization_alias="petTypeId")
    species: str
    asset_id: str | None = Field(serialization_alias="assetId")
    is_active: bool = Field(serialization_alias="isActive")
    stage: str
    level: int
    exp: int
    hunger: int
    thirst: int
    energy: int
    mood: int
    cleanliness: int
    bond: int
    health: int
    current_mood: str = Field(serialization_alias="currentMood")
    last_fed_at: datetime | None = Field(default=None, serialization_alias="lastFedAt")
    last_drink_at: datetime | None = Field(default=None, serialization_alias="lastDrinkAt")
    last_played_at: datetime | None = Field(default=None, serialization_alias="lastPlayedAt")
    last_slept_at: datetime | None = Field(default=None, serialization_alias="lastSleptAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")

    model_config = {"populate_by_name": True}


class DesktopProfile(BaseModel):
    pet_instance_id: int = Field(serialization_alias="petInstanceId")
    discord_user_id: str = Field(serialization_alias="discordUserId")
    position_x: int = Field(serialization_alias="positionX")
    position_y: int = Field(serialization_alias="positionY")
    scale: float
    selected_skin: str = Field(serialization_alias="selectedSkin")
    selected_accessory: str | None = Field(serialization_alias="selectedAccessory")
    bubble_enabled: bool = Field(serialization_alias="bubbleEnabled")
    sound_enabled: bool = Field(serialization_alias="soundEnabled")
    random_walk_enabled: bool = Field(serialization_alias="randomWalkEnabled")
    always_on_top: bool = Field(serialization_alias="alwaysOnTop")
    last_sync_at: datetime = Field(serialization_alias="lastSyncAt")

    model_config = {"populate_by_name": True}


class ActivePetResponse(BaseModel):
    active_pet: UserPetInstance = Field(serialization_alias="activePet")
    asset: PetAssetInfo
    desktop: DesktopProfile

    model_config = {"populate_by_name": True}


class SelectActivePetRequest(BaseModel):
    pet_instance_id: int = Field(validation_alias="petInstanceId")

    model_config = {"populate_by_name": True}


class DesktopSyncRequest(BaseModel):
    position_x: int | None = Field(default=None, validation_alias="positionX")
    position_y: int | None = Field(default=None, validation_alias="positionY")
    scale: float | None = None
    selected_skin: str | None = Field(default=None, validation_alias="selectedSkin")
    selected_accessory: str | None = Field(
        default=None, validation_alias="selectedAccessory",
    )
    bubble_enabled: bool | None = Field(default=None, validation_alias="bubbleEnabled")
    sound_enabled: bool | None = Field(default=None, validation_alias="soundEnabled")
    random_walk_enabled: bool | None = Field(
        default=None, validation_alias="randomWalkEnabled",
    )
    always_on_top: bool | None = Field(default=None, validation_alias="alwaysOnTop")

    model_config = {"populate_by_name": True}


class ActionRequest(BaseModel):
    source: str | None = "desktop"


class ActionResponse(BaseModel):
    active_pet: UserPetInstance = Field(serialization_alias="activePet")
    asset: PetAssetInfo
    desktop: DesktopProfile
    action: str
    delta: dict[str, int]
    exp_gained: int = Field(serialization_alias="expGained")
    level_up: bool = Field(serialization_alias="levelUp")
    new_stage: str | None = Field(serialization_alias="newStage")
    message: str | None = None

    model_config = {"populate_by_name": True}
