from __future__ import annotations

from datetime import datetime, timedelta, time as dt_time, date as dt_date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app import models
from app.auth import get_current_user, require_admin
from app.database import get_db

router = APIRouter(prefix="/api/viva", tags=["viva"])

DURATION_CHOICES = {20, 30, 40, 50, 60, 90, 120}


def _parse_hm(s: str) -> dt_time:
    parts = (s or "").strip().split(":")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Time must be HH:MM")
    return dt_time(int(parts[0]), int(parts[1]))


def _generate_slot_times(slot_date: dt_date, start_hm: str, end_hm: str, duration_min: int) -> list[tuple[datetime, datetime]]:
    cur = datetime.combine(slot_date, _parse_hm(start_hm))
    end = datetime.combine(slot_date, _parse_hm(end_hm))
    delta = timedelta(minutes=duration_min)
    out: list[tuple[datetime, datetime]] = []
    while cur + delta <= end:
        out.append((cur, cur + delta))
        cur += delta
    return out


def _lead_team(db: Session, user_id: int, section_id: int) -> models.Team | None:
    return (
        db.query(models.Team)
        .filter(models.Team.lead_id == user_id, models.Team.section_id == section_id)
        .first()
    )


def _slot_payload(slot: models.VivaSlot, db: Session, viewer_id: int | None = None) -> dict:
    lead_name = None
    team_name = None
    roster = []
    if slot.claimed_by_lead_id:
        lead = db.query(models.User).filter(models.User.id == slot.claimed_by_lead_id).first()
        lead_name = lead.name if lead else None
    if slot.team_id:
        team = (
            db.query(models.Team)
            .options(
                joinedload(models.Team.lead),
                joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
            )
            .filter(models.Team.id == slot.team_id)
            .first()
        )
        if team:
            team_name = team.name
            if team.lead:
                roster.append({"role": "Lead", "name": team.lead.name, "student_id": team.lead.student_id})
            for m in team.memberships:
                if m.status == "accepted" and m.member:
                    roster.append(
                        {"role": "Member", "name": m.member.name, "student_id": m.member.student_id}
                    )
    return {
        "id": slot.id,
        "start_at": slot.start_at.isoformat(),
        "end_at": slot.end_at.isoformat(),
        "status": slot.status,
        "claimed_by_lead_id": slot.claimed_by_lead_id,
        "lead_name": lead_name,
        "team_id": slot.team_id,
        "team_name": team_name,
        "roster": roster,
        "is_mine": bool(viewer_id and slot.claimed_by_lead_id == viewer_id),
    }


class VivaGenerateRequest(BaseModel):
    section_id: int
    sprint_label: str = Field(min_length=1, max_length=80)
    day: str = Field(min_length=1, max_length=20)
    slot_date: dt_date
    duration_minutes: int
    start_time: str
    end_time: str


class VivaToggleRequest(BaseModel):
    slot_id: int
    status: str  # open | off


class VivaClaimRequest(BaseModel):
    slot_id: int


class VivaPublishRequest(BaseModel):
    sprint_id: int


class VivaResetRequest(BaseModel):
    slot_id: int


@router.post("/generate")
def generate_slots(
    data: VivaGenerateRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if data.duration_minutes not in DURATION_CHOICES:
        raise HTTPException(status_code=400, detail=f"duration_minutes must be one of {sorted(DURATION_CHOICES)}")
    section = db.query(models.Section).filter(models.Section.id == data.section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    times = _generate_slot_times(data.slot_date, data.start_time, data.end_time, data.duration_minutes)
    if not times:
        raise HTTPException(status_code=400, detail="No slots fit in the given window")

    sprint = models.VivaSprint(
        section_id=data.section_id,
        sprint_label=data.sprint_label.strip(),
        day=data.day.strip(),
        slot_date=data.slot_date,
        duration_minutes=data.duration_minutes,
        window_start=data.start_time.strip(),
        window_end=data.end_time.strip(),
        published=False,
        created_by_id=admin.id,
    )
    db.add(sprint)
    db.flush()

    for start_at, end_at in times:
        db.add(models.VivaSlot(sprint_id=sprint.id, start_at=start_at, end_at=end_at, status="open"))
    db.commit()
    db.refresh(sprint)
    slots = db.query(models.VivaSlot).filter(models.VivaSlot.sprint_id == sprint.id).order_by(models.VivaSlot.start_at).all()
    return {
        "sprint_id": sprint.id,
        "slot_count": len(slots),
        "slots": [_slot_payload(s, db) for s in slots],
    }


@router.put("/toggle-status")
def toggle_slot_status(
    data: VivaToggleRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    status = (data.status or "").strip().lower()
    if status not in ("open", "off"):
        raise HTTPException(status_code=400, detail="status must be open or off")
    slot = db.query(models.VivaSlot).filter(models.VivaSlot.id == data.slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    if slot.status == "locked":
        raise HTTPException(status_code=400, detail="Locked slots cannot be toggled; use reset")
    slot.status = status
    db.commit()
    return _slot_payload(slot, db)


@router.post("/publish")
def publish_sprint(
    data: VivaPublishRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == data.sprint_id).first()
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    sprint.published = True
    db.commit()
    return {"sprint_id": sprint.id, "published": True}


@router.put("/reset")
def reset_locked_slot(
    data: VivaResetRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    slot = db.query(models.VivaSlot).filter(models.VivaSlot.id == data.slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    if slot.status != "locked":
        raise HTTPException(status_code=400, detail="Only locked slots can be reset")
    slot.status = "open"
    slot.claimed_by_lead_id = None
    slot.team_id = None
    db.commit()
    return _slot_payload(slot, db)


@router.post("/claim")
def claim_slot(
    data: VivaClaimRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    slot = db.query(models.VivaSlot).filter(models.VivaSlot.id == data.slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == slot.sprint_id).first()
    if not sprint or not sprint.published:
        raise HTTPException(status_code=400, detail="Sprint is not published")
    if slot.status != "open":
        raise HTTPException(status_code=409, detail="Slot is not available")

    team = _lead_team(db, user.id, sprint.section_id)
    if not team:
        raise HTTPException(status_code=403, detail="You are not a team lead for this section")

    existing = (
        db.query(models.VivaSlot)
        .filter(
            models.VivaSlot.sprint_id == sprint.id,
            models.VivaSlot.claimed_by_lead_id == user.id,
            models.VivaSlot.status == "locked",
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You already claimed a slot for this sprint")

    slot.status = "locked"
    slot.claimed_by_lead_id = user.id
    slot.team_id = team.id
    db.commit()
    db.refresh(slot)
    return _slot_payload(slot, db)


@router.get("/sprints")
def list_sprints(
    section_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        if not _lead_team(db, user.id, section_id):
            raise HTTPException(status_code=403, detail="Not authorized for this section")
    q = db.query(models.VivaSprint).filter(models.VivaSprint.section_id == section_id).order_by(models.VivaSprint.id.desc())
    if user.role != "admin":
        q = q.filter(models.VivaSprint.published.is_(True))
    sprints = q.all()
    return [
        {
            "id": s.id,
            "sprint_label": s.sprint_label,
            "day": s.day,
            "slot_date": s.slot_date.isoformat(),
            "duration_minutes": s.duration_minutes,
            "published": s.published,
        }
        for s in sprints
    ]


@router.get("/slots")
def list_slots(
    section_id: int,
    sprint_id: int | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        if not _lead_team(db, user.id, section_id):
            raise HTTPException(status_code=403, detail="Not authorized for this section")

    sprint_q = db.query(models.VivaSprint).filter(models.VivaSprint.section_id == section_id)
    if sprint_id:
        sprint_q = sprint_q.filter(models.VivaSprint.id == sprint_id)
    elif user.role != "admin":
        sprint_q = sprint_q.filter(models.VivaSprint.published.is_(True))
    sprint = sprint_q.order_by(models.VivaSprint.id.desc()).first()
    if not sprint:
        return {"sprint": None, "slots": []}
    if user.role != "admin" and not sprint.published:
        raise HTTPException(status_code=403, detail="Sprint not published")

    slots = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id)
        .order_by(models.VivaSlot.start_at)
        .all()
    )
    section = db.query(models.Section).filter(models.Section.id == section_id).first()
    return {
        "sprint": {
            "id": sprint.id,
            "sprint_label": sprint.sprint_label,
            "day": sprint.day,
            "slot_date": sprint.slot_date.isoformat(),
            "published": sprint.published,
            "section_name": section.name if section else "",
        },
        "slots": [_slot_payload(s, db, user.id) for s in slots],
    }


@router.get("/schedule")
def viva_schedule(
    section_id: int,
    sprint_id: int | None = None,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    sprint_q = db.query(models.VivaSprint).filter(models.VivaSprint.section_id == section_id)
    if sprint_id:
        sprint_q = sprint_q.filter(models.VivaSprint.id == sprint_id)
    sprint = sprint_q.order_by(models.VivaSprint.id.desc()).first()
    if not sprint:
        return {"sprint": None, "rows": []}

    section = db.query(models.Section).options(joinedload(models.Section.course)).filter(models.Section.id == section_id).first()
    slots = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
        .order_by(models.VivaSlot.start_at)
        .all()
    )
    rows = []
    for slot in slots:
        p = _slot_payload(slot, db)
        rows.append(
            {
                "section_name": section.name if section else "",
                "course_name": section.course.name if section and section.course else "",
                "slot_start": p["start_at"],
                "slot_end": p["end_at"],
                "lead_name": p["lead_name"],
                "team_name": p["team_name"],
                "roster": p["roster"],
            }
        )
    return {
        "sprint": {"id": sprint.id, "sprint_label": sprint.sprint_label, "published": sprint.published},
        "rows": rows,
    }
