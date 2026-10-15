"""Pet routes — active pet, owned listing, select, sync, actions.

All routes require a valid desktop JWT (issued by the link flow). The
guild_id is baked into the token at link time, so the caller's guild
context is implicit on every call.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import DesktopIdentity
from ..config import DeskApiSettings
from ..pet_service import (
    PetError,
    apply_action,
    fetch_active_pet,
    list_owned_pets,
    select_active_pet,
    update_desktop_profile,
    visual_state,
)
from ..schemas import (
    ActionRequest,
    ActionResponse,
    ActivePetResponse,
    ApiError,
    DesktopProfile,
    DesktopSyncRequest,
    OwnedPetsResponse,
    PetAssetInfo,
    SelectActivePetRequest,
    UserPetInstance,
    UserPetSummary,
)

log = logging.getLogger("akim-bot.desk-api.routers.pet")


# Map PetError.code → HTTP status. Default to 400 for anything unmapped
# rather than 500 — these are validation/business errors, not bugs.
_PET_ERROR_HTTP_STATUS: dict[str, int] = {
    "PET_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "ACTIVE_PET_REQUIRED": status.HTTP_409_CONFLICT,
    "PET_NOT_OWNED": status.HTTP_403_FORBIDDEN,
    "COOLDOWN_ACTIVE": status.HTTP_429_TOO_MANY_REQUESTS,
    "INSUFFICIENT_ENERGY": status.HTTP_409_CONFLICT,
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
}


def _raise_pet_error(e: PetError) -> None:
    raise HTTPException(
        status_code=_PET_ERROR_HTTP_STATUS.get(e.code, status.HTTP_400_BAD_REQUEST),
        detail={"error": True, "code": e.code, "message": e.message},
    )


def _build_pet_instance(view) -> UserPetInstance:
    """Serialize an ActivePetView's pet to the SPEC-shape UserPetInstance.
    Bot doesn't track per-action last-fed-at / last-drank-at, so those
    SPEC fields stay null until a future migration adds them."""
    pet = view.pet
    species = view.species
    species_name = (
        species.display_name if species else pet.species_id.replace("_", " ").title()
    )
    asset_id = species.desktop_asset_id if species else None
    return UserPetInstance(
        id=pet.id,
        owner_discord_id=str(pet.user_id),
        name=pet.nickname or species_name,
        pet_type_id=pet.species_id,
        species=pet.species_id,
        asset_id=asset_id,
        is_active=pet.is_active,
        stage=pet.stage,
        level=pet.level,
        exp=pet.exp,
        hunger=pet.hunger,
        thirst=pet.thirst,
        energy=pet.energy,
        mood=pet.mood,
        cleanliness=pet.cleanliness,
        bond=pet.bond,
        health=pet.health,
        current_mood=visual_state(pet),
        last_fed_at=None,
        last_drink_at=None,
        last_played_at=pet.last_play_at,
        last_slept_at=pet.last_rest_at,
        updated_at=pet.updated_at,
    )


def _build_asset(view) -> PetAssetInfo:
    species = view.species
    pet = view.pet
    display_name = (
        species.display_name if species else pet.species_id.replace("_", " ").title()
    )
    asset_id = species.desktop_asset_id if species else None
    return PetAssetInfo(
        asset_id=asset_id,
        display_name=display_name,
        # MVP: desktop bundles assets locally — server has no URLs to
        # hand back. Phase 3 will fill these once we wire an asset CDN
        # endpoint, but desktop already falls back to bundled assets
        # when these are null so the flow keeps working.
        local_path=(f"assets/pets/{asset_id}/pet.json" if asset_id else None),
        manifest_url=None,
        spritesheet_url=None,
        version=None,
    )


def _build_profile(view) -> DesktopProfile:
    p = view.profile
    return DesktopProfile(
        pet_instance_id=p.pet_instance_id,
        discord_user_id=str(p.discord_user_id),
        position_x=p.position_x,
        position_y=p.position_y,
        scale=p.scale,
        selected_skin=p.selected_skin,
        selected_accessory=p.selected_accessory,
        bubble_enabled=p.bubble_enabled,
        sound_enabled=p.sound_enabled,
        random_walk_enabled=p.random_walk_enabled,
        always_on_top=p.always_on_top,
        last_sync_at=p.last_sync_at,
    )


def _build_active_response(view) -> ActivePetResponse:
    return ActivePetResponse(
        active_pet=_build_pet_instance(view),
        asset=_build_asset(view),
        desktop=_build_profile(view),
    )


def build_pet_router(
    settings: DeskApiSettings,
    db_session_factory,
    desktop_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/pet", tags=["desktop-pet"])

    @router.get(
        "/active",
        response_model=ActivePetResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
        },
    )
    async def get_active(
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActivePetResponse:
        async with db_session_factory() as s:
            try:
                view = await fetch_active_pet(
                    s,
                    discord_user_id=identity.discord_user_id,
                    guild_id=identity.guild_id,
                )
            except PetError as e:
                _raise_pet_error(e)
        return _build_active_response(view)

    @router.get(
        "/owned",
        response_model=OwnedPetsResponse,
        responses={401: {"model": ApiError}},
    )
    async def get_owned(
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> OwnedPetsResponse:
        async with db_session_factory() as s:
            pairs = await list_owned_pets(
                s,
                discord_user_id=identity.discord_user_id,
                guild_id=identity.guild_id,
            )
        summaries: list[UserPetSummary] = []
        for pet, species in pairs:
            species_name = (
                species.display_name if species else
                pet.species_id.replace("_", " ").title()
            )
            summaries.append(UserPetSummary(
                id=pet.id,
                name=pet.nickname or species_name,
                pet_type_id=pet.species_id,
                asset_id=species.desktop_asset_id if species else None,
                display_name=species_name,
                level=pet.level,
                stage=pet.stage,
                is_active=pet.is_active,
            ))
        return OwnedPetsResponse(pets=summaries)

    @router.post(
        "/active/select",
        response_model=ActivePetResponse,
        responses={
            401: {"model": ApiError},
            403: {"model": ApiError},
            404: {"model": ApiError},
        },
    )
    async def post_select(
        payload: SelectActivePetRequest,
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActivePetResponse:
        async with db_session_factory() as s:
            try:
                view = await select_active_pet(
                    s,
                    discord_user_id=identity.discord_user_id,
                    guild_id=identity.guild_id,
                    pet_instance_id=payload.pet_instance_id,
                )
            except PetError as e:
                _raise_pet_error(e)
        return _build_active_response(view)

    @router.post(
        "/desktop/sync",
        response_model=ActivePetResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
        },
    )
    async def post_sync(
        payload: DesktopSyncRequest,
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActivePetResponse:
        # Pydantic to dict — exclude_none so the service layer's "only
        # update what the caller specified" semantics work cleanly.
        fields = payload.model_dump(exclude_none=True)
        async with db_session_factory() as s:
            try:
                view = await update_desktop_profile(
                    s,
                    discord_user_id=identity.discord_user_id,
                    guild_id=identity.guild_id,
                    fields=fields,
                )
            except PetError as e:
                _raise_pet_error(e)
        return _build_active_response(view)

    # --- actions --------------------------------------------------------

    async def _do_action(
        identity: DesktopIdentity, action: str,
    ) -> ActionResponse:
        async with db_session_factory() as s:
            try:
                applied = await apply_action(
                    s,
                    discord_user_id=identity.discord_user_id,
                    guild_id=identity.guild_id,
                    action=action,
                )
            except PetError as e:
                _raise_pet_error(e)
        view = applied.view
        result = applied.result
        return ActionResponse(
            active_pet=_build_pet_instance(view),
            asset=_build_asset(view),
            desktop=_build_profile(view),
            action=action,
            delta=result.delta,
            exp_gained=result.exp_gained,
            level_up=result.level_up,
            new_stage=result.new_stage,
            message=None,
        )

    @router.post(
        "/action/feed",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_feed(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        return await _do_action(identity, "feed")

    @router.post(
        "/action/drink",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_drink(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        # SPEC: "drink". Bot: "water". Map the URL verb to the bot's
        # internal action name here so the rest of the service speaks
        # one vocabulary.
        return await _do_action(identity, "water")

    @router.post(
        "/action/play",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_play(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        return await _do_action(identity, "play")

    @router.post(
        "/action/train",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_train(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        return await _do_action(identity, "train")

    @router.post(
        "/action/clean",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_clean(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        return await _do_action(identity, "clean")

    @router.post(
        "/action/sleep",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_sleep(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        # SPEC: "sleep". Bot: "rest". Same intent, different vocab.
        return await _do_action(identity, "rest")

    @router.post(
        "/action/pet",
        response_model=ActionResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def post_pet(
        _payload: ActionRequest = ActionRequest(),
        identity: DesktopIdentity = Depends(desktop_auth_dependency),
    ) -> ActionResponse:
        return await _do_action(identity, "pet")

    return router
