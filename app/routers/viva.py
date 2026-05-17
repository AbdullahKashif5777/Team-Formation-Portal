from __future__ import annotations

import uuid
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


def _section_meta(db: Session, section_id: int) -> tuple[str, str]:
    section = (
        db.query(models.Section)
        .options(joinedload(models.Section.course))
        .filter(models.Section.id == section_id)
        .first()
    )
    if not section:
        return "", ""
    return (
        section.name or "",
        section.course.name if section.course else "",
    )


def _times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _lead_time_conflict(db: Session, user_id: int, slot: models.VivaSlot, sprint: models.VivaSprint) -> models.VivaSlot | None:
    """Same lead cannot claim overlapping slots on the same date across any section."""
    others = (
        db.query(models.VivaSlot)
        .join(models.VivaSprint, models.VivaSprint.id == models.VivaSlot.sprint_id)
        .filter(
            models.VivaSlot.id != slot.id,
            models.VivaSlot.claimed_by_lead_id == user_id,
            models.VivaSlot.status == "locked",
            models.VivaSprint.slot_date == sprint.slot_date,
        )
        .all()
    )
    for other in others:
        if _times_overlap(slot.start_at, slot.end_at, other.start_at, other.end_at):
            return other
    return None


def _lead_team(db: Session, user_id: int, section_id: int) -> models.Team | None:
    return (
        db.query(models.Team)
        .filter(models.Team.lead_id == user_id, models.Team.section_id == section_id)
        .first()
    )


def _slot_payload(
    slot: models.VivaSlot,
    db: Session,
    viewer_id: int | None = None,
    section_name: str = "",
    course_name: str = "",
) -> dict:
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
        "section_name": section_name,
        "course_name": course_name,
        "roster": roster,
        "is_mine": bool(viewer_id and slot.claimed_by_lead_id == viewer_id),
    }


def _create_sprint_with_slots(
    db: Session,
    *,
    section_id: int,
    sprint_label: str,
    day: str,
    slot_date: dt_date,
    duration_minutes: int,
    start_time: str,
    end_time: str,
    admin_id: int,
    batch_key: str | None = None,
) -> tuple[models.VivaSprint, list[models.VivaSlot]]:
    times = _generate_slot_times(slot_date, start_time, end_time, duration_minutes)
    if not times:
        raise HTTPException(status_code=400, detail="No slots fit in the given window")
    sprint = models.VivaSprint(
        section_id=section_id,
        sprint_label=sprint_label.strip(),
        day=day.strip(),
        slot_date=slot_date,
        duration_minutes=duration_minutes,
        window_start=start_time.strip(),
        window_end=end_time.strip(),
        published=False,
        batch_key=batch_key,
        created_by_id=admin_id,
    )
    db.add(sprint)
    db.flush()
    slots: list[models.VivaSlot] = []
    for start_at, end_at in times:
        s = models.VivaSlot(sprint_id=sprint.id, start_at=start_at, end_at=end_at, status="open")
        db.add(s)
        slots.append(s)
    return sprint, slots


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


class VivaGenerateBulkRequest(BaseModel):
    section_ids: list[int] = Field(min_length=1)
    sprint_label: str = Field(min_length=1, max_length=80)
    day: str = Field(min_length=1, max_length=20)
    slot_date: dt_date
    duration_minutes: int
    start_time: str
    end_time: str


class VivaPublishBulkRequest(BaseModel):
    batch_key: str | None = None
    sprint_ids: list[int] | None = None


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

    sprint, slots = _create_sprint_with_slots(
        db,
        section_id=data.section_id,
        sprint_label=data.sprint_label,
        day=data.day,
        slot_date=data.slot_date,
        duration_minutes=data.duration_minutes,
        start_time=data.start_time,
        end_time=data.end_time,
        admin_id=admin.id,
    )
    db.commit()
    db.refresh(sprint)
    sec_name, course_name = _section_meta(db, data.section_id)
    return {
        "sprint_id": sprint.id,
        "batch_key": sprint.batch_key,
        "slot_count": len(slots),
        "slots": [_slot_payload(s, db, section_name=sec_name, course_name=course_name) for s in slots],
    }


@router.post("/generate-bulk")
def generate_slots_bulk(
    data: VivaGenerateBulkRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if data.duration_minutes not in DURATION_CHOICES:
        raise HTTPException(status_code=400, detail=f"duration_minutes must be one of {sorted(DURATION_CHOICES)}")
    batch_key = uuid.uuid4().hex
    sections_out: list[dict] = []
    for sid in data.section_ids:
        section = db.query(models.Section).filter(models.Section.id == sid).first()
        if not section:
            raise HTTPException(status_code=404, detail=f"Section {sid} not found")
        sprint, slots = _create_sprint_with_slots(
            db,
            section_id=sid,
            sprint_label=data.sprint_label,
            day=data.day,
            slot_date=data.slot_date,
            duration_minutes=data.duration_minutes,
            start_time=data.start_time,
            end_time=data.end_time,
            admin_id=admin.id,
            batch_key=batch_key,
        )
        sec_name, course_name = _section_meta(db, sid)
        sections_out.append(
            {
                "section_id": sid,
                "section_name": sec_name,
                "course_name": course_name,
                "sprint_id": sprint.id,
                "slot_count": len(slots),
            }
        )
    db.commit()
    return {"batch_key": batch_key, "slot_date": data.slot_date.isoformat(), "sections": sections_out}


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
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == slot.sprint_id).first()
    sec_name, course_name = _section_meta(db, sprint.section_id if sprint else 0)
    return _slot_payload(slot, db, section_name=sec_name, course_name=course_name)


@router.post("/publish-bulk")
def publish_sprints_bulk(
    data: VivaPublishBulkRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.VivaSprint).filter(models.VivaSprint.published.is_(False))
    if data.batch_key:
        q = q.filter(models.VivaSprint.batch_key == data.batch_key)
    elif data.sprint_ids:
        q = q.filter(models.VivaSprint.id.in_(data.sprint_ids))
    else:
        raise HTTPException(status_code=400, detail="Provide batch_key or sprint_ids")
    sprints = q.all()
    if not sprints:
        raise HTTPException(status_code=404, detail="No unpublished sprints found")
    for sprint in sprints:
        sprint.published = True
    db.commit()
    return {"published_count": len(sprints), "sprint_ids": [s.id for s in sprints]}


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
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == slot.sprint_id).first()
    sec_name, course_name = _section_meta(db, sprint.section_id if sprint else 0)
    return _slot_payload(slot, db, section_name=sec_name, course_name=course_name)


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

    conflict = _lead_time_conflict(db, user.id, slot, sprint)
    if conflict:
        raise HTTPException(
            status_code=409,
            detail="Time conflict: you already have a viva slot at this time (another section/course).",
        )

    slot.status = "locked"
    slot.claimed_by_lead_id = user.id
    slot.team_id = team.id
    db.commit()
    db.refresh(slot)
    sec_name, course_name = _section_meta(db, sprint.section_id)
    return _slot_payload(slot, db, user.id, sec_name, course_name)


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
            "batch_key": s.batch_key,
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
    if user.role != "admin":
        slots = [s for s in slots if s.status != "off"]
    sec_name, course_name = _section_meta(db, section_id)
    return {
        "sprint": {
            "id": sprint.id,
            "sprint_label": sprint.sprint_label,
            "day": sprint.day,
            "slot_date": sprint.slot_date.isoformat(),
            "published": sprint.published,
            "section_name": sec_name,
            "course_name": course_name,
            "batch_key": sprint.batch_key,
        },
        "slots": [_slot_payload(s, db, user.id, sec_name, course_name) for s in slots],
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

    sec_name, course_name = _section_meta(db, section_id)
    slots = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
        .order_by(models.VivaSlot.start_at)
        .all()
    )
    rows = []
    for slot in slots:
        p = _slot_payload(slot, db, section_name=sec_name, course_name=course_name)
        rows.append(
            {
                "course_name": course_name,
                "section_name": sec_name,
                "team_name": p["team_name"],
                "lead_name": p["lead_name"],
                "slot_start": p["start_at"],
                "slot_end": p["end_at"],
                "roster": p["roster"],
            }
        )
    return {
        "sprint": {"id": sprint.id, "sprint_label": sprint.sprint_label, "published": sprint.published},
        "rows": rows,
    }


@router.get("/schedule-all")
def viva_schedule_all(
    batch_key: str | None = None,
    slot_date: dt_date | None = None,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Master viva sheet: all sections in a batch or on a given date."""
    sprint_q = db.query(models.VivaSprint)
    if batch_key:
        sprint_q = sprint_q.filter(models.VivaSprint.batch_key == batch_key)
    elif slot_date:
        sprint_q = sprint_q.filter(models.VivaSprint.slot_date == slot_date)
    else:
        raise HTTPException(status_code=400, detail="Provide batch_key or slot_date")
    sprints = sprint_q.order_by(models.VivaSprint.section_id, models.VivaSprint.id).all()
    if not sprints:
        return {"rows": []}

    rows: list[dict] = []
    for sprint in sprints:
        sec_name, course_name = _section_meta(db, sprint.section_id)
        slots = (
            db.query(models.VivaSlot)
            .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
            .order_by(models.VivaSlot.start_at)
            .all()
        )
        for slot in slots:
            p = _slot_payload(slot, db, section_name=sec_name, course_name=course_name)
            rows.append(
                {
                    "course_name": course_name,
                    "section_name": sec_name,
                    "team_name": p["team_name"],
                    "lead_name": p["lead_name"],
                    "slot_start": p["start_at"],
                    "slot_end": p["end_at"],
                    "roster": p["roster"],
                    "sprint_label": sprint.sprint_label,
                    "slot_date": sprint.slot_date.isoformat(),
                }
            )
        # Include open/unclaimed rows for admin overview? User asked for sheet with teams - locked only is fine
    rows.sort(key=lambda r: (r["slot_date"], r["slot_start"], r["course_name"], r["section_name"]))
    return {"rows": rows, "batch_key": batch_key, "slot_date": slot_date.isoformat() if slot_date else None}


@router.get("/member-team-slot")
def member_team_viva_slot(
    section_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read-only: accepted member sees their team's claimed viva slot (if any)."""
    membership = (
        db.query(models.TeamMembership)
        .join(models.Team, models.Team.id == models.TeamMembership.team_id)
        .options(joinedload(models.TeamMembership.team).joinedload(models.Team.lead))
        .filter(
            models.TeamMembership.member_id == user.id,
            models.TeamMembership.status == "accepted",
            models.Team.section_id == section_id,
        )
        .first()
    )
    sec_name, course_name = _section_meta(db, section_id)
    if not membership or not membership.team:
        return {"slot": None, "course_name": course_name, "section_name": sec_name}

    team = membership.team
    sprint = (
        db.query(models.VivaSprint)
        .filter(models.VivaSprint.section_id == section_id, models.VivaSprint.published.is_(True))
        .order_by(models.VivaSprint.id.desc())
        .first()
    )
    if not sprint:
        return {
            "slot": None,
            "course_name": course_name,
            "section_name": sec_name,
            "team_name": team.name,
            "lead_name": team.lead.name if team.lead else None,
        }

    slot = (
        db.query(models.VivaSlot)
        .filter(
            models.VivaSlot.sprint_id == sprint.id,
            models.VivaSlot.status == "locked",
            models.VivaSlot.team_id == team.id,
        )
        .first()
    )
    if not slot and team.lead_id:
        slot = (
            db.query(models.VivaSlot)
            .filter(
                models.VivaSlot.sprint_id == sprint.id,
                models.VivaSlot.status == "locked",
                models.VivaSlot.claimed_by_lead_id == team.lead_id,
            )
            .first()
        )

    payload = {
        "course_name": course_name,
        "section_name": sec_name,
        "team_name": team.name,
        "lead_name": team.lead.name if team.lead else None,
        "sprint_label": sprint.sprint_label,
        "slot_date": sprint.slot_date.isoformat(),
        "day": sprint.day,
        "slot": None,
    }
    if slot:
        payload["slot"] = _slot_payload(slot, db, section_name=sec_name, course_name=course_name)
    return payload
