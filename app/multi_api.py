"""Administrator endpoints for the shared multi-account service."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from multi_service import BusyError


class PetInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    since: date | None = None
    until: date | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Name must not be blank")
        if self.since and self.until and self.since > self.until:
            raise ValueError("Start date must precede end date")
        return self


class ReferenceInput(BaseModel):
    account: str
    asset_id: UUID
    pet_id: UUID | None = None
    bbox: list[Annotated[float, Field(ge=0, le=1)]] | None = Field(
        default=None, min_length=4, max_length=4
    )

    @model_validator(mode="after")
    def validate_bbox(self):
        if self.bbox and (self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]):
            raise ValueError("Crop must have positive width and height")
        return self


class ScanInput(BaseModel):
    account: str | None = None
    since: date | None = None
    until: date | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.until and not self.since:
            raise ValueError("A start date is required with an end date")
        if self.since and self.until and self.since > self.until:
            raise ValueError("Start date must precede end date")
        return self


def create_router(service):
    router = APIRouter(prefix="/api/multi")

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "Pet or account not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            # requests exceptions may include URLs or headers from a remote server.
            raise HTTPException(
                502, "Immich request failed; check connectivity and account permissions"
            ) from exc

    @router.get("/state")
    def state():
        return service.public_state()

    @router.post("/pets")
    def create_pet(body: PetInput):
        return {"id": call(service.create_pet, **body.model_dump(mode="json"))}

    @router.put("/pets/{pet_id}")
    def update_pet(pet_id: UUID, body: PetInput):
        call(service.update_pet, str(pet_id), body.model_dump(mode="json"))
        return {"ok": True}

    @router.delete("/pets/{pet_id}")
    def delete_pet(pet_id: UUID, local_only: bool = False):
        call(service.delete_pet, str(pet_id), local_only=local_only)
        return {"ok": True}

    @router.post("/pets/{pet_id}/reset/{account_id}")
    def reset_pet(pet_id: UUID, account_id: str):
        call(service.reset_pet, str(pet_id), account_id)
        return {"ok": True}

    @router.post("/references")
    def add_reference(body: ReferenceInput):
        call(
            service.add_reference,
            body.account,
            str(body.asset_id),
            str(body.pet_id) if body.pet_id else None,
            body.bbox,
        )
        return {"ok": True}

    @router.delete("/references")
    def remove_reference(body: ReferenceInput):
        call(
            service.remove_reference,
            body.account,
            str(body.asset_id),
            str(body.pet_id) if body.pet_id else None,
        )
        return {"ok": True}

    @router.get("/thumb/{account_id}/{asset_id}")
    def thumbnail(account_id: str, asset_id: UUID):
        account = call(service.account, account_id)
        call(account.require_asset, str(asset_id))
        response = call(
            account.request,
            "GET",
            f"assets/{asset_id}/thumbnail",
            params={"size": "preview"},
        )
        return Response(
            response.content,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/scan")
    def scan(body: ScanInput):
        if body.account and body.account not in service.accounts:
            raise HTTPException(400, "Unknown account")
        since = body.since.isoformat() + "T00:00:00.000Z" if body.since else None
        until = body.until.isoformat() + "T23:59:59.999Z" if body.until else None
        call(service.start_scan, [body.account] if body.account else None, since, until)
        return {"ok": True}

    @router.post("/albums/sync")
    def sync_albums():
        if not service.albums.owner_id:
            raise HTTPException(400, "Shared albums are not enabled")
        call(service.start_scan, albums_only=True)
        return {"ok": True}

    @router.post("/scan/stop")
    def stop():
        service.cancel.set()
        return {"ok": True}

    return router
