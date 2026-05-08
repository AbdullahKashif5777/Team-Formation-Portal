from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(tags=["roster"])


class RosterCellUpdate(BaseModel):
    column_name: str
    new_value: str | int | float | None
    section_id: int | None = None


class RosterExtraDataPatch(BaseModel):
    membership_id: int
    section_id: int | None = None
    patch: dict


@router.patch("/roster/update/{membership_id}")
@router.patch("/api/roster/update/{membership_id}")
def update_roster_cell(
    membership_id: int,
    payload: RosterCellUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = (
        db.query(models.TeamMembership)
        .options(
            joinedload(models.TeamMembership.member),
            joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
            joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
        )
        .filter(models.TeamMembership.id == membership_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    col = (payload.column_name or "").strip()
    if not col:
        raise HTTPException(status_code=422, detail="column_name is required")

    v = payload.new_value
    base_keys = {"student_name", "student_id", "team_name", "team_id"}

    # Resolve section context for authorization and validation.
    membership_section_id = membership.team.section_id if membership.team else None
    if not membership_section_id:
        raise HTTPException(status_code=400, detail="Invalid membership section")
    if payload.section_id is not None and int(payload.section_id) != int(membership_section_id):
        raise HTTPException(status_code=403, detail="Section mismatch")

    if user.role not in {"admin", "lead"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Lead authorization: must own this section and can only edit dynamic columns
    # that exist in section.column_config (no base identity fields).
    allowed_dynamic_keys: set[str] = set()
    if user.role == "lead":
        owns_section = (
            db.query(models.Team)
            .filter(models.Team.lead_id == user.id, models.Team.section_id == membership_section_id)
            .first()
            is not None
        )
        if not owns_section:
            raise HTTPException(status_code=403, detail="Not authorized for this section")

        section = db.query(models.Section).filter(models.Section.id == membership_section_id).first()
        if section and section.column_config:
            for c in section.column_config:
                k = (c or {}).get("key")
                if k:
                    allowed_dynamic_keys.add(str(k))

        if col in {"student_name", "student_id", "team_name", "team_id"}:
            raise HTTPException(status_code=403, detail="Not allowed to edit this base field")
        elif col not in allowed_dynamic_keys:
            raise HTTPException(status_code=403, detail="Not allowed to edit this column")

    try:
        refresh = False

        if user.role == "admin" and col in base_keys:
            if col == "student_name":
                membership.member.name = ("" if v is None else str(v)).strip()
            elif col == "student_id":
                membership.member.student_id = ("" if v is None else str(v)).strip().upper() or None
            elif col == "team_name":
                if not membership.team:
                    raise HTTPException(status_code=400, detail="Membership has no team")
                membership.team.name = ("" if v is None else str(v)).strip()
                refresh = True
            elif col == "team_id":
                # Optional: allow admin to reassign a membership to a different team within the same section.
                if v is None or (isinstance(v, str) and not str(v).strip()):
                    raise HTTPException(status_code=422, detail="team_id cannot be empty")
                new_team_id = int(v)
                new_team = db.query(models.Team).filter(models.Team.id == new_team_id).first()
                if not new_team or new_team.section_id != membership_section_id:
                    raise HTTPException(status_code=422, detail="Invalid team_id for this section")
                membership.team_id = new_team_id
                refresh = True
        else:
            # Dynamic/custom column: store into TeamMembership.extra_data
            if membership.extra_data is None:
                membership.extra_data = {}
            membership.extra_data[col] = v
            flag_modified(membership, "extra_data")

        membership.updated_at = datetime.utcnow()
        db.commit()
        team = membership.team
        return {
            "ok": True,
            "refresh": refresh,
            "updated": {
                "membership_id": membership.id,
                "team_id": team.id if team else None,
                "team_name": team.name if team else None,
                "lead_name": team.lead.name if team and team.lead else None,
            },
        }
    except ValueError:
        db.rollback()
        raise HTTPException(status_code=422, detail="Invalid value type")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/roster/patch")
@router.patch("/api/roster/patch")
def patch_roster_extra_data(
    payload: RosterExtraDataPatch,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Patch TeamMembership.extra_data (dynamic columns) without touching base fields.
    This is intentionally additive to preserve existing update endpoints.
    """
    membership = (
        db.query(models.TeamMembership)
        .options(
            joinedload(models.TeamMembership.member),
            joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
            joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
        )
        .filter(models.TeamMembership.id == payload.membership_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    membership_section_id = membership.team.section_id if membership.team else None
    if not membership_section_id:
        raise HTTPException(status_code=400, detail="Invalid membership section")
    if payload.section_id is not None and int(payload.section_id) != int(membership_section_id):
        raise HTTPException(status_code=403, detail="Section mismatch")

    if user.role not in {"admin", "lead"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    allowed_dynamic_keys: set[str] = set()
    if user.role == "lead":
        owns_section = (
            db.query(models.Team)
            .filter(models.Team.lead_id == user.id, models.Team.section_id == membership_section_id)
            .first()
            is not None
        )
        if not owns_section:
            raise HTTPException(status_code=403, detail="Not authorized for this section")

        section = db.query(models.Section).filter(models.Section.id == membership_section_id).first()
        if section and section.column_config:
            for c in section.column_config:
                k = (c or {}).get("key")
                if k:
                    allowed_dynamic_keys.add(str(k))

    if membership.extra_data is None:
        membership.extra_data = {}

    patch_obj = payload.patch or {}
    if not isinstance(patch_obj, dict):
        raise HTTPException(status_code=422, detail="patch must be an object")

    try:
        applied: dict[str, str] = {}
        for k, v in patch_obj.items():
            key = ("" if k is None else str(k)).strip()
            if not key:
                continue
            if user.role == "lead" and key not in allowed_dynamic_keys:
                raise HTTPException(status_code=403, detail=f"Not allowed to edit column '{key}'")
            membership.extra_data[key] = v
            applied[key] = key
        flag_modified(membership, "extra_data")

        membership.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "membership_id": membership.id, "applied_keys": list(applied.keys())}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
