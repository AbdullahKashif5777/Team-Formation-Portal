from __future__ import annotations

import uuid
from datetime import datetime, timedelta, time as dt_time, date as dt_date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app import models
from app.auth import get_current_user, require_admin
from app.database import get_db
from app import email_utils
from app.ws_manager import manager

router = APIRouter(prefix="/api/viva", tags=["viva"])

DURATION_CHOICES = {20, 30, 40, 50, 60, 90, 120}
SPRINT_NUMBERS = {1, 2, 3, 4, 5}


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


def _sprint_label_for_number(n: int) -> str:
    return f"Sprint {n}"


def _team_roster_people(team: models.Team) -> list[tuple[str, models.User]]:
    people: list[tuple[str, models.User]] = []
    if team.lead:
        people.append(("Lead", team.lead))
    for m in team.memberships or []:
        if m.status == "accepted" and m.member:
            people.append(("Member", m.member))
    return people


def _seed_member_scores_for_sprint(
    db: Session,
    sprint: models.VivaSprint,
    locked_slots: list[models.VivaSlot],
) -> None:
    """Ensure each booked team member has a score row (null until graded)."""
    seen: set[tuple[int, int]] = set()
    for slot in locked_slots:
        if not slot.team_id:
            continue
        team = _load_team_for_slot(db, slot.team_id)
        if not team:
            continue
        for role, person in _team_roster_people(team):
            key = (sprint.id, person.id)
            if key in seen:
                continue
            seen.add(key)
            exists = (
                db.query(models.VivaMemberScore)
                .filter(
                    models.VivaMemberScore.sprint_id == sprint.id,
                    models.VivaMemberScore.member_id == person.id,
                )
                .first()
            )
            if not exists:
                db.add(
                    models.VivaMemberScore(
                        sprint_id=sprint.id,
                        team_id=team.id,
                        member_id=person.id,
                        score=None,
                    )
                )
    db.flush()


def _members_with_scores(
    team: models.Team,
    saved: dict[int, models.VivaMemberScore],
) -> list[dict]:
    members: list[dict] = []
    for role, person in _team_roster_people(team):
        rec = saved.get(person.id)
        members.append(
            {
                "member_id": person.id,
                "role": role,
                "name": person.name,
                "student_id": person.student_id,
                "score": rec.score if rec else None,
                "notes": rec.notes if rec else None,
            }
        )
    return members


def _team_marks_sort_key(team: dict) -> tuple:
    return (
        team.get("slot_start") or "",
        team.get("course_name") or "",
        team.get("section_name") or "",
        team.get("team_name") or "",
    )


def _load_team_for_slot(db: Session, team_id: int) -> models.Team | None:
    return (
        db.query(models.Team)
        .options(
            joinedload(models.Team.lead),
            joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .filter(models.Team.id == team_id)
        .first()
    )


def _batch_keys_for_section(db: Session, section_id: int) -> list[str]:
    # Backwards-compatible: if table isn't created yet, behave like non-shared mode.
    try:
        rows = (
            db.query(models.VivaBatchSection.batch_key)
            .filter(models.VivaBatchSection.section_id == section_id)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]
    except Exception:
        db.rollback()
        return []


def _section_in_batch(db: Session, section_id: int, batch_key: str) -> bool:
    try:
        return (
            db.query(models.VivaBatchSection)
            .filter(
                models.VivaBatchSection.batch_key == batch_key,
                models.VivaBatchSection.section_id == section_id,
            )
            .first()
            is not None
        )
    except Exception:
        db.rollback()
        return False


def _register_batch_sections(db: Session, batch_key: str, section_ids: list[int]) -> None:
    # Backwards-compatible: if table isn't created yet, skip registration.
    try:
        existing = (
            db.query(models.VivaBatchSection.section_id)
            .filter(models.VivaBatchSection.batch_key == batch_key)
            .all()
        )
        existing_sids = {int(r[0]) for r in existing}
    except Exception:
        db.rollback()
        return
    new_sids = [sid for sid in section_ids if sid not in existing_sids]
    for sid in new_sids:
        db.add(models.VivaBatchSection(batch_key=batch_key, section_id=sid))


def _section_can_access_sprint(db: Session, section_id: int, sprint: models.VivaSprint) -> bool:
    if sprint.is_shared_pool and sprint.batch_key:
        return _section_in_batch(db, section_id, sprint.batch_key)
    return sprint.section_id == section_id


def _sprints_query_for_section(db: Session, section_id: int):
    batch_keys = _batch_keys_for_section(db, section_id)
    clauses = [models.VivaSprint.section_id == section_id]
    if batch_keys:
        clauses.append(
            (models.VivaSprint.batch_key.in_(batch_keys)) & (models.VivaSprint.is_shared_pool.is_(True))
        )
    return db.query(models.VivaSprint).filter(or_(*clauses))


def _resolve_sprint_for_section(
    db: Session, section_id: int, sprint_id: int | None = None, published_only: bool = False
) -> models.VivaSprint | None:
    if sprint_id:
        sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == sprint_id).first()
        if not sprint or not _section_can_access_sprint(db, section_id, sprint):
            return None
        if published_only and not sprint.published:
            return None
        return sprint
    q = _sprints_query_for_section(db, section_id)
    if published_only:
        q = q.filter(models.VivaSprint.published.is_(True))
    return q.order_by(models.VivaSprint.id.desc()).first()


def _lead_team_for_sprint(
    db: Session,
    user_id: int,
    sprint: models.VivaSprint,
    section_id: int | None = None,
) -> models.Team | None:
    """Find a team the user leads that can still claim this sprint (not yet booked)."""
    if section_id is not None:
        team = _lead_team(db, user_id, section_id)
        return team
    candidates: list[models.Team] = []
    if sprint.is_shared_pool and sprint.batch_key:
        try:
            rows = (
                db.query(models.VivaBatchSection.section_id)
                .filter(models.VivaBatchSection.batch_key == sprint.batch_key)
                .all()
            )
        except Exception:
            db.rollback()
            rows = []
        for row in rows:
            sid = row[0]
            team = _lead_team(db, user_id, int(sid))
            if team:
                candidates.append(team)
    else:
        team = _lead_team(db, user_id, sprint.section_id)
        if team:
            candidates.append(team)
    # Prefer a team that hasn't already claimed this sprint
    for team in candidates:
        already = (
            db.query(models.VivaSlot)
            .filter(
                models.VivaSlot.sprint_id == sprint.id,
                models.VivaSlot.team_id == team.id,
                models.VivaSlot.status == "locked",
            )
            .first()
        )
        if not already:
            # Also check sprint_number restriction
            if sprint.sprint_number:
                num_claim = _team_locked_for_sprint_number(db, team.id, sprint.sprint_number)
                if num_claim:
                    continue
            return team
    # All teams already booked; return first one so caller can show "already claimed"
    return candidates[0] if candidates else None


def _team_meta_from_team(db: Session, team: models.Team) -> tuple[str, str, int]:
    sec_name, course_name = _section_meta(db, team.section_id)
    return sec_name, course_name, team.section_id


def _lead_time_conflict(db: Session, user_id: int, slot: models.VivaSlot, sprint: models.VivaSprint) -> models.VivaSlot | None:
    """Same lead cannot claim two overlapping slots on the same date (global pool)."""
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


def _section_lead_ids(db: Session, section_id: int) -> list[int]:
    teams = (
        db.query(models.Team.lead_id)
        .filter(models.Team.section_id == section_id, models.Team.lead_id.isnot(None))
        .distinct()
        .all()
    )
    out: list[int] = []
    for row in teams:
        lid = row[0]
        if lid and lid not in out:
            out.append(int(lid))
    return out


def _notify_viva_published(db: Session, sprints: list[models.VivaSprint]) -> None:
    """Tell section leads that new viva slots are available (best-effort, non-blocking)."""
    try:
        seen: set[int] = set()
        for sprint in sprints:
            section_ids: list[int] = [sprint.section_id]
            if sprint.is_shared_pool and sprint.batch_key:
                rows = (
                    db.query(models.VivaBatchSection.section_id)
                    .filter(models.VivaBatchSection.batch_key == sprint.batch_key)
                    .all()
                )
                section_ids = [int(r[0]) for r in rows]
            for sid in section_ids:
                sec_name, course_name = _section_meta(db, sid)
                for lead_id in _section_lead_ids(db, sid):
                    if lead_id in seen:
                        continue
                    seen.add(lead_id)
                    try:
                        manager.fire(
                            manager.send_to(
                                lead_id,
                                "viva_published",
                                {
                                    "section_id": sid,
                                    "section_name": sec_name,
                                    "course_name": course_name,
                                    "sprint_label": sprint.sprint_label,
                                    "sprint_number": sprint.sprint_number,
                                    "slot_date": sprint.slot_date.isoformat(),
                                    "day": sprint.day,
                                    "sprint_id": sprint.id,
                                },
                            )
                        )
                    except RuntimeError:
                        pass
    except Exception:
        pass


def _team_locked_for_sprint_label(
    db: Session, team_id: int, section_id: int, sprint_label: str
) -> models.VivaSlot | None:
    """One team may claim only one slot per sprint label (across all dates) in a section."""
    return (
        db.query(models.VivaSlot)
        .join(models.VivaSprint, models.VivaSprint.id == models.VivaSlot.sprint_id)
        .filter(
            models.VivaSprint.section_id == section_id,
            models.VivaSprint.sprint_label == sprint_label,
            models.VivaSlot.team_id == team_id,
            models.VivaSlot.status == "locked",
        )
        .first()
    )


def _team_locked_for_sprint_number(db: Session, team_id: int, sprint_number: int) -> models.VivaSlot | None:
    """One team may claim only one slot per sprint number (1–5), on any date."""
    return (
        db.query(models.VivaSlot)
        .join(models.VivaSprint, models.VivaSprint.id == models.VivaSlot.sprint_id)
        .filter(
            models.VivaSprint.sprint_number == sprint_number,
            models.VivaSlot.team_id == team_id,
            models.VivaSlot.status == "locked",
        )
        .first()
    )


def _cleanup_batch_sections(db: Session, batch_key: str | None) -> None:
    if not batch_key:
        return
    remaining = db.query(models.VivaSprint).filter(models.VivaSprint.batch_key == batch_key).count()
    if remaining == 0:
        try:
            db.query(models.VivaBatchSection).filter(models.VivaBatchSection.batch_key == batch_key).delete(
                synchronize_session=False
            )
        except Exception:
            db.rollback()
            return


def _send_viva_booking_emails(
    db: Session,
    team: models.Team,
    sprint: models.VivaSprint,
    slot: models.VivaSlot,
    section_name: str,
    course_name: str,
) -> None:
    lead = db.query(models.User).filter(models.User.id == team.lead_id).first()
    time_label = f"{slot.start_at.strftime('%H:%M')} – {slot.end_at.strftime('%H:%M')}"
    date_label = sprint.slot_date.isoformat()
    seen_emails: set[str] = set()
    if lead and lead.email:
        seen_emails.add(lead.email.lower())
        email_utils.send_async(
            email_utils.send_viva_slot_booked,
            lead.email,
            lead.name,
            lead.student_id,
            team.name,
            course_name,
            section_name,
            sprint.sprint_label,
            date_label,
            time_label,
            sprint.day,
        )
    members = (
        db.query(models.TeamMembership)
        .options(joinedload(models.TeamMembership.member))
        .filter(models.TeamMembership.team_id == team.id, models.TeamMembership.status == "accepted")
        .all()
    )
    for m in members:
        if m.member and m.member.email:
            seen_emails.add(m.member.email.lower())
            email_utils.send_async(
                email_utils.send_viva_slot_booked,
                m.member.email,
                m.member.name,
                m.member.student_id,
                team.name,
                course_name,
                section_name,
                sprint.sprint_label,
                date_label,
                time_label,
                sprint.day,
            )

    # Only lead + their own team members are notified (not the whole section).


def _roster_row_from_slot(
    slot: models.VivaSlot, sprint: models.VivaSprint, sec_name: str, course_name: str, db: Session
) -> dict:
    section_id = sprint.section_id
    if slot.team_id:
        team = db.query(models.Team).filter(models.Team.id == slot.team_id).first()
        if team:
            sec_name, course_name = _section_meta(db, team.section_id)
            section_id = team.section_id
    p = _slot_payload(slot, db, section_name=sec_name, course_name=course_name, section_id=section_id)
    return {
        "course_name": course_name,
        "section_name": sec_name,
        "section_id": section_id,
        "sprint_id": sprint.id,
        "sprint_label": sprint.sprint_label,
        "sprint_number": sprint.sprint_number,
        "slot_date": sprint.slot_date.isoformat(),
        "day": sprint.day,
        "team_id": slot.team_id,
        "team_name": p["team_name"],
        "lead_name": p["lead_name"],
        "slot_start": p["start_at"],
        "slot_end": p["end_at"],
        "roster": p["roster"],
    }


def _slot_payload(
    slot: models.VivaSlot,
    db: Session,
    viewer_id: int | None = None,
    section_name: str = "",
    course_name: str = "",
    section_id: int | None = None,
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
            # Show the booking team's actual course/section (not the viewer's)
            team_sec_name, team_course_name = _section_meta(db, team.section_id)
            section_name = team_sec_name
            course_name = team_course_name
            section_id = team.section_id
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
        "note": getattr(slot, "note", None),
        "section_name": section_name,
        "course_name": course_name,
        "section_id": section_id,
        "roster": roster,
        "booking_status": "Booked" if slot.status == "locked" else ("Off" if slot.status == "off" else "Open"),
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
    sprint_number: int | None = None,
    is_shared_pool: bool = False,
) -> tuple[models.VivaSprint, list[models.VivaSlot]]:
    times = _generate_slot_times(slot_date, start_time, end_time, duration_minutes)
    if not times:
        raise HTTPException(status_code=400, detail="No slots fit in the given window")
    sprint = models.VivaSprint(
        section_id=section_id,
        sprint_label=sprint_label.strip(),
        sprint_number=sprint_number,
        day=day.strip(),
        slot_date=slot_date,
        duration_minutes=duration_minutes,
        window_start=start_time.strip(),
        window_end=end_time.strip(),
        published=False,
        batch_key=batch_key,
        is_shared_pool=is_shared_pool,
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
    sprint_number: int | None = Field(default=None, ge=1, le=5)
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
    section_id: int | None = None


class VivaPublishRequest(BaseModel):
    sprint_id: int


class VivaResetRequest(BaseModel):
    slot_id: int


class VivaGenerateBulkRequest(BaseModel):
    section_ids: list[int] = Field(min_length=1)
    sprint_number: int = Field(ge=1, le=5)
    sprint_label: str | None = Field(default=None, max_length=80)
    day: str = Field(min_length=1, max_length=20)
    slot_date: dt_date
    duration_minutes: int
    start_time: str
    end_time: str


class VivaPublishBulkRequest(BaseModel):
    batch_key: str | None = None
    sprint_ids: list[int] | None = None


class VivaSetAllStatusRequest(BaseModel):
    sprint_id: int
    status: str  # open | off


class VivaScoreUpdate(BaseModel):
    member_id: int
    team_id: int | None = None
    score: float | None = None
    notes: str | None = None


class VivaScoresSaveRequest(BaseModel):
    sprint_id: int
    scores: list[VivaScoreUpdate]


class VivaMarksSectionEntry(BaseModel):
    member_id: int
    team_id: int
    sprint_number: int
    score: float | None = None
    notes: str | None = None


class VivaMarksSectionSaveRequest(BaseModel):
    section_id: int
    batch_key: str | None = None
    slot_date: dt_date | None = None
    mode: str = "save"  # save: non-empty only; update: all entries including clears
    entries: list[VivaMarksSectionEntry]


class VivaClearPublishedRequest(BaseModel):
    batch_key: str | None = None
    slot_date: dt_date | None = None
    sprint_id: int | None = None
    section_id: int | None = None


class VivaSlotNoteRequest(BaseModel):
    slot_id: int
    note: str | None = None


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

    sn = data.sprint_number if data.sprint_number in SPRINT_NUMBERS else None
    label = data.sprint_label.strip() or (_sprint_label_for_number(sn) if sn else "")
    sprint, slots = _create_sprint_with_slots(
        db,
        section_id=data.section_id,
        sprint_label=label,
        day=data.day,
        slot_date=data.slot_date,
        duration_minutes=data.duration_minutes,
        start_time=data.start_time,
        end_time=data.end_time,
        admin_id=admin.id,
        sprint_number=sn,
    )
    db.commit()
    db.refresh(sprint)
    sec_name, course_name = _section_meta(db, data.section_id)
    return {
        "sprint_id": sprint.id,
        "batch_key": sprint.batch_key,
        "slot_count": len(slots),
        "slots": [_slot_payload(s, db, section_name=sec_name, course_name=course_name, section_id=data.section_id) for s in slots],
    }


@router.post("/generate-bulk")
def generate_slots_bulk(
    data: VivaGenerateBulkRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if data.duration_minutes not in DURATION_CHOICES:
        raise HTTPException(status_code=400, detail=f"duration_minutes must be one of {sorted(DURATION_CHOICES)}")
    if data.sprint_number not in SPRINT_NUMBERS:
        raise HTTPException(status_code=400, detail="sprint_number must be between 1 and 5")
    batch_key = uuid.uuid4().hex
    label = (data.sprint_label or "").strip() or _sprint_label_for_number(data.sprint_number)
    host_section_id = data.section_ids[0]
    for sid in data.section_ids:
        section = db.query(models.Section).filter(models.Section.id == sid).first()
        if not section:
            raise HTTPException(status_code=404, detail=f"Section {sid} not found")
    sprint, slots = _create_sprint_with_slots(
        db,
        section_id=host_section_id,
        sprint_label=label,
        day=data.day,
        slot_date=data.slot_date,
        duration_minutes=data.duration_minutes,
        start_time=data.start_time,
        end_time=data.end_time,
        admin_id=admin.id,
        batch_key=batch_key,
        sprint_number=data.sprint_number,
        is_shared_pool=True,
    )
    _register_batch_sections(db, batch_key, data.section_ids)
    sections_out: list[dict] = []
    for sid in data.section_ids:
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
    return {
        "batch_key": batch_key,
        "slot_date": data.slot_date.isoformat(),
        "sprint_id": sprint.id,
        "sprint_number": data.sprint_number,
        "sprint_label": label,
        "sections": sections_out,
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
    _notify_viva_published(db, sprints)
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
    _notify_viva_published(db, [sprint])
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
    slot = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.id == data.slot_id)
        .with_for_update()
        .first()
    )
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == slot.sprint_id).first()
    if not sprint or not sprint.published:
        raise HTTPException(status_code=400, detail="Sprint is not published")
    if slot.status != "open":
        raise HTTPException(status_code=409, detail="Slot is not available")

    if sprint.is_shared_pool and sprint.batch_key and not data.section_id:
        raise HTTPException(
            status_code=400,
            detail="section_id is required — book for the course/section you opened.",
        )
    team = _lead_team_for_sprint(db, user.id, sprint, section_id=data.section_id)
    if not team:
        raise HTTPException(status_code=403, detail="You are not a team lead for this section")
    if not _section_can_access_sprint(db, team.section_id, sprint):
        raise HTTPException(status_code=403, detail="Your section is not part of this viva batch")
    if data.section_id and team.section_id != data.section_id:
        raise HTTPException(status_code=403, detail="Booking must use your team in this section")

    existing = (
        db.query(models.VivaSlot)
        .filter(
            models.VivaSlot.sprint_id == sprint.id,
            models.VivaSlot.team_id == team.id,
            models.VivaSlot.status == "locked",
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="This team already claimed a slot for this sprint")

    if sprint.sprint_number:
        num_claim = _team_locked_for_sprint_number(db, team.id, sprint.sprint_number)
        if num_claim and num_claim.id != slot.id:
            other = db.query(models.VivaSprint).filter(models.VivaSprint.id == num_claim.sprint_id).first()
            other_date = other.slot_date.isoformat() if other else "another day"
            raise HTTPException(
                status_code=400,
                detail=f"Your team already booked Sprint {sprint.sprint_number} on {other_date}. "
                "One slot per sprint number for your team.",
            )
    else:
        label_claim = _team_locked_for_sprint_label(db, team.id, team.section_id, sprint.sprint_label)
        if label_claim and label_claim.sprint_id != sprint.id:
            other = db.query(models.VivaSprint).filter(models.VivaSprint.id == label_claim.sprint_id).first()
            other_date = other.slot_date.isoformat() if other else "another day"
            raise HTTPException(
                status_code=400,
                detail=f"Your team already has a slot for {sprint.sprint_label} ({other_date}). "
                "You cannot pick another day until a new sprint opens.",
            )

    conflict = _lead_time_conflict(db, user.id, slot, sprint)
    if conflict:
        raise HTTPException(
            status_code=409,
            detail="Time conflict: you already have a viva slot at this time on this day.",
        )

    slot.status = "locked"
    slot.claimed_by_lead_id = user.id
    slot.team_id = team.id
    db.commit()
    db.refresh(slot)
    sec_name, course_name, section_id = _team_meta_from_team(db, team)
    _send_viva_booking_emails(db, team, sprint, slot, sec_name, course_name)
    return _slot_payload(slot, db, user.id, sec_name, course_name, section_id)


@router.get("/sprints")
def list_sprints(
    section_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        team_any = db.query(models.Team).filter(models.Team.lead_id == user.id).first()
        if not team_any:
            raise HTTPException(status_code=403, detail="Not authorized")
    q = _sprints_query_for_section(db, section_id).order_by(models.VivaSprint.id.desc())
    sprints = q.all()
    if user.role != "admin":
        lead_team = _lead_team(db, user.id, section_id)
        teams = [lead_team] if lead_team else []
    else:
        teams = []
    team_ids = [t.id for t in teams]
    out = []
    seen_shared: set[tuple] = set()
    for s in sprints:
        if s.is_shared_pool and s.batch_key:
            key = (s.batch_key, s.sprint_number, s.slot_date.isoformat())
            if key in seen_shared:
                continue
            seen_shared.add(key)
        item = {
            "id": s.id,
            "sprint_label": s.sprint_label,
            "sprint_number": s.sprint_number,
            "day": s.day,
            "slot_date": s.slot_date.isoformat(),
            "duration_minutes": s.duration_minutes,
            "published": s.published,
            "batch_key": s.batch_key,
            "is_shared_pool": s.is_shared_pool,
        }
        if teams:
            any_claim = False
            all_blocked = True
            blocked_reason = None
            for team in teams:
                this_claim = (
                    db.query(models.VivaSlot)
                    .filter(
                        models.VivaSlot.sprint_id == s.id,
                        models.VivaSlot.team_id == team.id,
                        models.VivaSlot.status == "locked",
                    )
                    .first()
                )
                if this_claim:
                    any_claim = True
                blocked = None
                if s.sprint_number:
                    blocked = _team_locked_for_sprint_number(db, team.id, s.sprint_number)
                else:
                    blocked = _team_locked_for_sprint_label(db, team.id, section_id, s.sprint_label)
                if not this_claim and (not blocked or blocked.sprint_id == s.id):
                    all_blocked = False
                elif blocked and blocked.sprint_id != s.id and not blocked_reason:
                    other_sp = db.query(models.VivaSprint).filter(models.VivaSprint.id == blocked.sprint_id).first()
                    lbl = f"Sprint {s.sprint_number}" if s.sprint_number else s.sprint_label
                    blocked_reason = (
                        f"Team already booked {lbl} on "
                        f"{other_sp.slot_date.isoformat() if other_sp else 'another day'}"
                    )
            item["team_has_slot"] = any_claim
            item["can_claim"] = bool(s.published and not any_claim and not all_blocked)
            if all_blocked and blocked_reason:
                item["blocked_reason"] = blocked_reason
        out.append(item)
    return out


@router.get("/slots")
def list_slots(
    section_id: int,
    sprint_id: int | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        team_any = db.query(models.Team).filter(models.Team.lead_id == user.id).first()
        if not team_any:
            raise HTTPException(status_code=403, detail="Not authorized")

    sprint = _resolve_sprint_for_section(db, section_id, sprint_id, published_only=False)
    if not sprint:
        return {"sprint": None, "slots": []}

    slots = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id)
        .order_by(models.VivaSlot.start_at)
        .all()
    )
    # All slots shown to leads (off slots displayed as unavailable with notes)
    sec_name, course_name = _section_meta(db, section_id)
    return {
        "sprint": {
            "id": sprint.id,
            "sprint_label": sprint.sprint_label,
            "sprint_number": sprint.sprint_number,
            "day": sprint.day,
            "slot_date": sprint.slot_date.isoformat(),
            "published": sprint.published,
            "section_name": sec_name,
            "course_name": course_name,
            "batch_key": sprint.batch_key,
            "is_shared_pool": sprint.is_shared_pool,
        },
        "slots": [_slot_payload(s, db, user.id, sec_name, course_name, section_id) for s in slots],
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

    seen_shared: set[int] = set()
    rows: list[dict] = []
    for sprint in sprints:
        if sprint.is_shared_pool and sprint.id in seen_shared:
            continue
        if sprint.is_shared_pool:
            seen_shared.add(sprint.id)
        sec_name, course_name = _section_meta(db, sprint.section_id)
        slots = (
            db.query(models.VivaSlot)
            .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
            .order_by(models.VivaSlot.start_at)
            .all()
        )
        for slot in slots:
            row = _roster_row_from_slot(slot, sprint, sec_name, course_name, db)
            rows.append(row)
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
    sprint = _resolve_sprint_for_section(db, section_id, published_only=True)
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


@router.delete("/sprint/{sprint_id}")
def delete_sprint(
    sprint_id: int,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove a sprint and all its slots (draft or published) so admin can regenerate."""
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == sprint_id).first()
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    bk = sprint.batch_key
    db.query(models.VivaMemberScore).filter(models.VivaMemberScore.sprint_id == sprint_id).delete()
    db.delete(sprint)
    db.flush()
    _cleanup_batch_sections(db, bk)
    db.commit()
    return {"deleted": True, "sprint_id": sprint_id}


@router.put("/set-all-status")
def set_all_slot_status(
    data: VivaSetAllStatusRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    status = (data.status or "").strip().lower()
    if status not in ("open", "off"):
        raise HTTPException(status_code=400, detail="status must be open or off")
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == data.sprint_id).first()
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    slots = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status != "locked")
        .all()
    )
    for slot in slots:
        slot.status = status
    db.commit()
    return {"updated": len(slots), "status": status}


@router.put("/slot-note")
def set_slot_note(
    data: VivaSlotNoteRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    slot = db.query(models.VivaSlot).filter(models.VivaSlot.id == data.slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    note = (data.note or "").strip()
    slot.note = note or None
    db.commit()
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == slot.sprint_id).first()
    sec_name, course_name = _section_meta(db, sprint.section_id if sprint else 0)
    return _slot_payload(
        slot,
        db,
        section_name=sec_name,
        course_name=course_name,
        section_id=(sprint.section_id if sprint else None),
    )


@router.get("/roster/daily")
def roster_by_day(
    slot_date: dt_date,
    section_id: int | None = None,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Day sheet: all locked viva bookings on a date with full team rosters."""
    sprint_q = db.query(models.VivaSprint).filter(models.VivaSprint.slot_date == slot_date)
    if section_id:
        sprint_q = sprint_q.filter(models.VivaSprint.section_id == section_id)
    sprints = sprint_q.order_by(models.VivaSprint.section_id, models.VivaSprint.sprint_label).all()
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
            rows.append(_roster_row_from_slot(slot, sprint, sec_name, course_name, db))
    rows.sort(key=lambda r: (r["slot_start"], r["course_name"], r["section_name"]))
    return {"slot_date": slot_date.isoformat(), "rows": rows}


@router.get("/roster/daily-all")
def roster_daily_all(
    slot_date: dt_date,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Day sheet (admin): every slot (Open/Off/Booked) with team + note."""
    sprints = (
        db.query(models.VivaSprint)
        .filter(models.VivaSprint.slot_date == slot_date)
        .order_by(models.VivaSprint.sprint_number, models.VivaSprint.id)
        .all()
    )
    if not sprints:
        return {"slot_date": slot_date.isoformat(), "rows": []}
    seen_shared: set[int] = set()
    rows: list[dict] = []
    for sprint in sprints:
        if getattr(sprint, "is_shared_pool", False) and sprint.id in seen_shared:
            continue
        if getattr(sprint, "is_shared_pool", False):
            seen_shared.add(sprint.id)
        slots = (
            db.query(models.VivaSlot)
            .filter(models.VivaSlot.sprint_id == sprint.id)
            .order_by(models.VivaSlot.start_at)
            .all()
        )
        for slot in slots:
            sec_name, course_name = _section_meta(db, sprint.section_id)
            sid = sprint.section_id
            if slot.team_id:
                team = db.query(models.Team).filter(models.Team.id == slot.team_id).first()
                if team:
                    sec_name, course_name = _section_meta(db, team.section_id)
                    sid = team.section_id
            p = _slot_payload(slot, db, section_name=sec_name, course_name=course_name, section_id=sid)
            rows.append(
                {
                    "slot_date": slot_date.isoformat(),
                    "sprint_id": sprint.id,
                    "sprint_label": sprint.sprint_label,
                    "sprint_number": sprint.sprint_number,
                    "course_name": course_name,
                    "section_name": sec_name,
                    "section_id": sid,
                    "slot_start": p["start_at"],
                    "slot_end": p["end_at"],
                    "booking_status": p["booking_status"],
                    "team_name": p["team_name"] or "—",
                    "lead_name": p["lead_name"] or "—",
                    "note": p.get("note"),
                }
            )
    rows.sort(key=lambda r: (r["sprint_number"] or 0, r["slot_start"], r["course_name"], r["section_name"]))
    return {"slot_date": slot_date.isoformat(), "rows": rows}


@router.get("/roster/sprint-label")
def roster_by_sprint_label(
    section_id: int,
    sprint_label: str,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Sprint label sheet: all teams who booked any day for this sprint name."""
    label = sprint_label.strip()
    sprints = (
        db.query(models.VivaSprint)
        .filter(models.VivaSprint.section_id == section_id, models.VivaSprint.sprint_label == label)
        .order_by(models.VivaSprint.slot_date, models.VivaSprint.id)
        .all()
    )
    sec_name, course_name = _section_meta(db, section_id)
    rows: list[dict] = []
    for sprint in sprints:
        slots = (
            db.query(models.VivaSlot)
            .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
            .order_by(models.VivaSlot.start_at)
            .all()
        )
        for slot in slots:
            rows.append(_roster_row_from_slot(slot, sprint, sec_name, course_name, db))
    return {"section_id": section_id, "sprint_label": label, "rows": rows}


@router.get("/scores")
def get_viva_scores(
    sprint_id: int,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grading sheet: members from locked teams + saved scores for this sprint."""
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == sprint_id).first()
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    sec_name, course_name = _section_meta(db, sprint.section_id)
    locked = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
        .order_by(models.VivaSlot.start_at)
        .all()
    )
    _seed_member_scores_for_sprint(db, sprint, locked)
    db.commit()
    saved = {
        s.member_id: s
        for s in db.query(models.VivaMemberScore).filter(models.VivaMemberScore.sprint_id == sprint_id).all()
    }
    teams_map: dict[int, dict] = {}
    for slot in locked:
        if not slot.team_id:
            continue
        if slot.team_id in teams_map:
            continue
        team = _load_team_for_slot(db, slot.team_id)
        if not team:
            continue
        sec_name, course_name = _section_meta(db, team.section_id)
        teams_map[slot.team_id] = {
            "team_id": team.id,
            "team_name": team.name,
            "lead_name": team.lead.name if team.lead else None,
            "course_name": course_name,
            "section_name": sec_name,
            "slot_start": slot.start_at.isoformat(),
            "slot_end": slot.end_at.isoformat(),
            "members": _members_with_scores(team, saved),
        }
    teams = sorted(teams_map.values(), key=_team_marks_sort_key)
    rows: list[dict] = []
    for team_block in teams:
        for member in team_block["members"]:
            rows.append(
                {
                    **member,
                    "team_id": team_block["team_id"],
                    "team_name": team_block["team_name"],
                    "course_name": team_block["course_name"],
                    "section_name": team_block["section_name"],
                    "slot_start": team_block["slot_start"],
                    "slot_end": team_block["slot_end"],
                }
            )
    return {
        "sprint": {
            "id": sprint.id,
            "sprint_label": sprint.sprint_label,
            "slot_date": sprint.slot_date.isoformat(),
            "section_name": sec_name,
            "course_name": course_name,
        },
        "teams": teams,
        "rows": rows,
    }


@router.put("/scores")
def save_viva_scores(
    data: VivaScoresSaveRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    sprint = db.query(models.VivaSprint).filter(models.VivaSprint.id == data.sprint_id).first()
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    locked_slots = (
        db.query(models.VivaSlot)
        .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
        .all()
    )
    updated = 0
    for item in data.scores:
        team_id = item.team_id
        if not team_id:
            for sl in locked_slots:
                if not sl.team_id:
                    continue
                team = db.query(models.Team).filter(models.Team.id == sl.team_id).first()
                if not team:
                    continue
                if team.lead_id == item.member_id:
                    team_id = team.id
                    break
                mem = (
                    db.query(models.TeamMembership)
                    .filter(
                        models.TeamMembership.team_id == team.id,
                        models.TeamMembership.member_id == item.member_id,
                        models.TeamMembership.status == "accepted",
                    )
                    .first()
                )
                if mem:
                    team_id = team.id
                    break
        if not team_id:
            continue
        rec = (
            db.query(models.VivaMemberScore)
            .filter(
                models.VivaMemberScore.sprint_id == sprint.id,
                models.VivaMemberScore.member_id == item.member_id,
            )
            .first()
        )
        if not rec:
            rec = models.VivaMemberScore(
                sprint_id=sprint.id,
                team_id=team_id,
                member_id=item.member_id,
            )
            db.add(rec)
        else:
            rec.team_id = team_id
        rec.score = item.score
        if item.notes is not None:
            rec.notes = (item.notes or "").strip() or None
        rec.updated_by_id = admin.id
        updated += 1
    db.commit()
    return {"saved": updated}


@router.post("/clear-published")
def clear_published_slots(
    data: VivaClearPublishedRequest,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Unpublish sprint(s) for a day/batch/section — sets published=False, does NOT delete."""
    q = db.query(models.VivaSprint).filter(models.VivaSprint.published.is_(True))
    if data.sprint_id:
        q = q.filter(models.VivaSprint.id == data.sprint_id)
    elif data.batch_key:
        q = q.filter(models.VivaSprint.batch_key == data.batch_key)
    elif data.slot_date:
        q = q.filter(models.VivaSprint.slot_date == data.slot_date)
        if data.section_id:
            q = q.filter(models.VivaSprint.section_id == data.section_id)
    else:
        raise HTTPException(status_code=400, detail="Provide sprint_id, batch_key, or slot_date")
    sprints = q.all()
    if not sprints:
        raise HTTPException(status_code=404, detail="No published sprints found to unpublish")
    for sprint in sprints:
        sprint.published = False
    db.commit()
    return {"unpublished_count": len(sprints), "sprint_ids": [s.id for s in sprints]}


@router.get("/roster/matrix")
def roster_matrix(
    batch_key: str | None = None,
    slot_date: dt_date | None = None,
    section_id: int | None = None,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Master matrix: shared pool slots once per time; booker section/course when locked."""
    sprint_q = db.query(models.VivaSprint)
    if batch_key:
        sprint_q = sprint_q.filter(models.VivaSprint.batch_key == batch_key)
    elif slot_date:
        sprint_q = sprint_q.filter(models.VivaSprint.slot_date == slot_date)
    else:
        raise HTTPException(status_code=400, detail="Provide batch_key or slot_date")
    if section_id:
        sprint_q = sprint_q.filter(
            or_(
                models.VivaSprint.section_id == section_id,
                models.VivaSprint.batch_key.in_(_batch_keys_for_section(db, section_id)),
            )
        )
    sprints = sprint_q.order_by(
        models.VivaSprint.slot_date, models.VivaSprint.sprint_number, models.VivaSprint.id
    ).all()
    seen_shared: set[int] = set()
    rows: list[dict] = []
    for sprint in sprints:
        if sprint.is_shared_pool and sprint.id in seen_shared:
            continue
        if sprint.is_shared_pool:
            seen_shared.add(sprint.id)
        slots = (
            db.query(models.VivaSlot)
            .filter(models.VivaSlot.sprint_id == sprint.id)
            .order_by(models.VivaSlot.start_at)
            .all()
        )
        for slot in slots:
            sec_name, course_name = _section_meta(db, sprint.section_id)
            sid = sprint.section_id
            if slot.team_id:
                team = db.query(models.Team).filter(models.Team.id == slot.team_id).first()
                if team:
                    sec_name, course_name = _section_meta(db, team.section_id)
                    sid = team.section_id
            p = _slot_payload(slot, db, section_name=sec_name, course_name=course_name, section_id=sid)
            rows.append(
                {
                    "slot_date": sprint.slot_date.isoformat(),
                    "slot_start": p["start_at"],
                    "slot_end": p["end_at"],
                    "section_id": sid,
                    "section_name": sec_name,
                    "course_name": course_name,
                    "sprint_label": sprint.sprint_label,
                    "sprint_number": sprint.sprint_number,
                    "lead_name": p["lead_name"] or "—",
                    "team_name": p["team_name"] or "—",
                    "booking_status": p["booking_status"],
                }
            )
    return {"rows": rows, "batch_key": batch_key, "slot_date": slot_date.isoformat() if slot_date else None}


@router.get("/roster/team-sheets")
def roster_team_sheets(
    batch_key: str | None = None,
    slot_date: dt_date | None = None,
    sprint_number: int | None = None,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """One card per booked team with course, section, members, and sprint bookings."""
    sprint_q = db.query(models.VivaSprint)
    if batch_key:
        sprint_q = sprint_q.filter(models.VivaSprint.batch_key == batch_key)
    elif slot_date:
        sprint_q = sprint_q.filter(models.VivaSprint.slot_date == slot_date)
    else:
        raise HTTPException(status_code=400, detail="Provide batch_key or slot_date")
    if sprint_number is not None:
        if sprint_number not in SPRINT_NUMBERS:
            raise HTTPException(status_code=400, detail="sprint_number must be 1–5")
        sprint_q = sprint_q.filter(models.VivaSprint.sprint_number == sprint_number)
    sprints = sprint_q.order_by(models.VivaSprint.sprint_number, models.VivaSprint.id).all()
    seen_shared: set[int] = set()
    teams_map: dict[int, dict] = {}
    for sprint in sprints:
        if sprint.is_shared_pool and sprint.id in seen_shared:
            continue
        if sprint.is_shared_pool:
            seen_shared.add(sprint.id)
        slots = (
            db.query(models.VivaSlot)
            .filter(
                models.VivaSlot.sprint_id == sprint.id,
                models.VivaSlot.status == "locked",
                models.VivaSlot.team_id.isnot(None),
            )
            .all()
        )
        for slot in slots:
            if not slot.team_id:
                continue
            if slot.team_id not in teams_map:
                team = (
                    db.query(models.Team)
                    .options(
                        joinedload(models.Team.lead),
                        joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
                    )
                    .filter(models.Team.id == slot.team_id)
                    .first()
                )
                if not team:
                    continue
                sec_name, course_name = _section_meta(db, team.section_id)
                roster = []
                if team.lead:
                    roster.append({"role": "Lead", "name": team.lead.name, "student_id": team.lead.student_id})
                for m in team.memberships:
                    if m.status == "accepted" and m.member:
                        roster.append(
                            {"role": "Member", "name": m.member.name, "student_id": m.member.student_id}
                        )
                teams_map[slot.team_id] = {
                    "team_id": team.id,
                    "team_name": team.name,
                    "lead_name": team.lead.name if team.lead else None,
                    "course_name": course_name,
                    "section_name": sec_name,
                    "section_id": team.section_id,
                    "roster": roster,
                    "bookings": [],
                }
            teams_map[slot.team_id]["bookings"].append(
                {
                    "sprint_label": sprint.sprint_label,
                    "sprint_number": sprint.sprint_number,
                    "slot_date": sprint.slot_date.isoformat(),
                    "day": sprint.day,
                    "slot_start": slot.start_at.isoformat(),
                    "slot_end": slot.end_at.isoformat(),
                }
            )
    sheets = sorted(teams_map.values(), key=lambda t: (t["course_name"], t["section_name"], t["team_name"] or ""))
    sprint_numbers = sorted({s.sprint_number for s in sprints if s.sprint_number})
    return {"sheets": sheets, "sprint_numbers": sprint_numbers}


def _team_roster_payload(team: models.Team) -> list[dict]:
    roster: list[dict] = []
    if team.lead:
        roster.append({"role": "Lead", "name": team.lead.name, "student_id": team.lead.student_id})
    for m in team.memberships or []:
        if m.status == "accepted" and m.member:
            roster.append(
                {"role": "Member", "name": m.member.name, "student_id": m.member.student_id}
            )
    return roster


def _empty_sprint_scores(sprint_numbers: list[int]) -> tuple[dict, dict]:
    scores = {str(sn): None for sn in sprint_numbers}
    notes = {str(sn): None for sn in sprint_numbers}
    return scores, notes


def _sprint_ids_by_number(sprints: list[models.VivaSprint]) -> dict[str, int]:
    """Map sprint_number -> sprint_id for the loaded batch/day (shared pool: one row per number)."""
    out: dict[str, int] = {}
    seen_shared: set[int] = set()
    for sprint in sorted(sprints, key=lambda s: (s.sprint_number or 0, s.id)):
        if sprint.is_shared_pool and sprint.id in seen_shared:
            continue
        if sprint.is_shared_pool:
            seen_shared.add(sprint.id)
        if sprint.sprint_number:
            out[str(sprint.sprint_number)] = sprint.id
    return out


def _sprints_for_marks_scope(
    db: Session,
    *,
    batch_key: str | None = None,
    slot_date: dt_date | None = None,
    sprint_number: int | None = None,
) -> list[models.VivaSprint]:
    sprint_q = db.query(models.VivaSprint)
    if batch_key:
        sprint_q = sprint_q.filter(models.VivaSprint.batch_key == batch_key)
    elif slot_date:
        sprint_q = sprint_q.filter(models.VivaSprint.slot_date == slot_date)
    else:
        return []
    if sprint_number is not None:
        sprint_q = sprint_q.filter(models.VivaSprint.sprint_number == sprint_number)
    return sprint_q.order_by(
        models.VivaSprint.sprint_number, models.VivaSprint.slot_date.desc(), models.VivaSprint.id.desc()
    ).all()


def _section_ids_for_sprints(db: Session, sprints: list[models.VivaSprint]) -> set[int]:
    section_ids: set[int] = set()
    seen_shared: set[int] = set()
    for sprint in sprints:
        if sprint.is_shared_pool and sprint.batch_key:
            if sprint.id in seen_shared:
                continue
            seen_shared.add(sprint.id)
            try:
                rows = (
                    db.query(models.VivaBatchSection.section_id)
                    .filter(models.VivaBatchSection.batch_key == sprint.batch_key)
                    .all()
                )
                section_ids.update(int(r[0]) for r in rows)
            except Exception:
                db.rollback()
        else:
            section_ids.add(sprint.section_id)
    return section_ids


def _build_marks_grid(db: Session, sprints: list[models.VivaSprint]) -> tuple[list[int], list[dict]]:
    """Roster/team-sheets layout: course → section sheets, all teams, sprint mark columns."""
    sprint_numbers = sorted({s.sprint_number for s in sprints if s.sprint_number})
    if not sprint_numbers:
        sprint_numbers = sorted(SPRINT_NUMBERS)

    section_ids = _section_ids_for_sprints(db, sprints)
    teams_by_id: dict[int, dict] = {}

    for sid in sorted(section_ids):
        sec_name, course_name = _section_meta(db, sid)
        section = (
            db.query(models.Section)
            .options(joinedload(models.Section.course))
            .filter(models.Section.id == sid)
            .first()
        )
        course_id = section.course_id if section else None
        teams = (
            db.query(models.Team)
            .options(
                joinedload(models.Team.lead),
                joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
            )
            .filter(models.Team.section_id == sid)
            .order_by(models.Team.name)
            .all()
        )
        for team in teams:
            sprint_scores, sprint_notes = _empty_sprint_scores(sprint_numbers)
            members: list[dict] = []
            for role, person in _team_roster_people(team):
                members.append(
                    {
                        "member_id": person.id,
                        "role": role,
                        "name": person.name,
                        "student_id": person.student_id,
                        "sprint_scores": dict(sprint_scores),
                        "sprint_notes": dict(sprint_notes),
                    }
                )
            teams_by_id[team.id] = {
                "team_id": team.id,
                "team_name": team.name,
                "lead_name": team.lead.name if team.lead else None,
                "course_id": course_id,
                "course_name": course_name,
                "section_id": sid,
                "section_name": sec_name,
                "roster": _team_roster_payload(team),
                "bookings": [],
                "members": members,
            }

    seen_shared: set[int] = set()
    for sprint in sprints:
        if sprint.is_shared_pool and sprint.id in seen_shared:
            continue
        if sprint.is_shared_pool:
            seen_shared.add(sprint.id)
        sn = sprint.sprint_number
        if not sn:
            continue
        locked = (
            db.query(models.VivaSlot)
            .filter(models.VivaSlot.sprint_id == sprint.id, models.VivaSlot.status == "locked")
            .order_by(models.VivaSlot.start_at)
            .all()
        )
        _seed_member_scores_for_sprint(db, sprint, locked)
        saved = {
            s.member_id: s
            for s in db.query(models.VivaMemberScore).filter(models.VivaMemberScore.sprint_id == sprint.id).all()
        }
        for slot in locked:
            if not slot.team_id:
                continue
            team = _load_team_for_slot(db, slot.team_id)
            if not team:
                continue
            if slot.team_id not in teams_by_id:
                sec_name, course_name = _section_meta(db, team.section_id)
                section = (
                    db.query(models.Section)
                    .options(joinedload(models.Section.course))
                    .filter(models.Section.id == team.section_id)
                    .first()
                )
                sprint_scores, sprint_notes = _empty_sprint_scores(sprint_numbers)
                members = []
                for role, person in _team_roster_people(team):
                    members.append(
                        {
                            "member_id": person.id,
                            "role": role,
                            "name": person.name,
                            "student_id": person.student_id,
                            "sprint_scores": dict(sprint_scores),
                            "sprint_notes": dict(sprint_notes),
                        }
                    )
                teams_by_id[slot.team_id] = {
                    "team_id": team.id,
                    "team_name": team.name,
                    "lead_name": team.lead.name if team.lead else None,
                    "course_id": section.course_id if section else None,
                    "course_name": course_name,
                    "section_id": team.section_id,
                    "section_name": sec_name,
                    "roster": _team_roster_payload(team),
                    "bookings": [],
                    "members": members,
                }
            entry = teams_by_id[slot.team_id]
            entry["bookings"].append(
                {
                    "sprint_label": sprint.sprint_label,
                    "sprint_number": sn,
                    "slot_date": sprint.slot_date.isoformat(),
                    "day": sprint.day,
                    "slot_start": slot.start_at.isoformat(),
                    "slot_end": slot.end_at.isoformat(),
                }
            )
            members_by_id = {m["member_id"]: m for m in entry["members"]}
            for role, person in _team_roster_people(team):
                if person.id not in members_by_id:
                    sc, nt = _empty_sprint_scores(sprint_numbers)
                    members_by_id[person.id] = {
                        "member_id": person.id,
                        "role": role,
                        "name": person.name,
                        "student_id": person.student_id,
                        "sprint_scores": sc,
                        "sprint_notes": nt,
                    }
                rec = saved.get(person.id)
                members_by_id[person.id]["sprint_scores"][str(sn)] = rec.score if rec else None
                members_by_id[person.id]["sprint_notes"][str(sn)] = rec.notes if rec else None
            entry["members"] = sorted(
                members_by_id.values(),
                key=lambda m: (0 if m["role"] == "Lead" else 1, m["name"] or ""),
            )

    courses_map: dict[int, dict] = {}
    for team_data in sorted(
        teams_by_id.values(),
        key=lambda t: (t.get("course_name") or "", t.get("section_name") or "", t.get("team_name") or ""),
    ):
        cid = team_data.get("course_id") or 0
        sid = team_data.get("section_id") or 0
        if cid not in courses_map:
            courses_map[cid] = {
                "course_id": cid,
                "course_name": team_data.get("course_name") or "",
                "section_sheets": {},
            }
        courses_map[cid]["section_sheets"].setdefault(
            sid,
            {
                "section_id": sid,
                "section_name": team_data.get("section_name") or "",
                "course_id": cid,
                "course_name": team_data.get("course_name") or "",
                "teams": [],
            },
        )
        courses_map[cid]["section_sheets"][sid]["teams"].append(
            {
                "team_id": team_data["team_id"],
                "team_name": team_data["team_name"],
                "lead_name": team_data["lead_name"],
                "course_name": team_data["course_name"],
                "section_name": team_data["section_name"],
                "roster": team_data["roster"],
                "bookings": sorted(
                    team_data["bookings"],
                    key=lambda b: (b.get("sprint_number") or 0, b.get("slot_start") or ""),
                ),
                "members": team_data["members"],
            }
        )

    course_sheets: list[dict] = []
    for cid in sorted(courses_map.keys(), key=lambda i: courses_map[i]["course_name"]):
        c = courses_map[cid]
        section_sheets = []
        for sid in sorted(c["section_sheets"].keys(), key=lambda i: c["section_sheets"][i]["section_name"]):
            section_sheets.append(c["section_sheets"][sid])
        course_sheets.append(
            {
                "course_id": c["course_id"],
                "course_name": c["course_name"],
                "section_sheets": section_sheets,
            }
        )
    db.commit()
    return sprint_numbers, course_sheets


@router.get("/marks/overview")
def marks_overview(
    batch_key: str | None = None,
    slot_date: dt_date | None = None,
    sprint_number: int | None = None,
    section_id: int | None = None,
    course_id: int | None = None,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Course/section roster sheets with sprint mark columns (same layout as Team sheets)."""
    if sprint_number is not None and sprint_number not in SPRINT_NUMBERS:
        raise HTTPException(status_code=400, detail="sprint_number must be 1–5")
    sprints = _sprints_for_marks_scope(db, batch_key=batch_key, slot_date=slot_date, sprint_number=sprint_number)
    if not sprints and not (batch_key or slot_date):
        sprints = db.query(models.VivaSprint).order_by(models.VivaSprint.sprint_number).all()
    sprint_numbers, course_sheets = _build_marks_grid(db, sprints)
    if section_id is not None:
        course_sheets = [
            {
                **c,
                "section_sheets": [s for s in c.get("section_sheets", []) if s.get("section_id") == section_id],
            }
            for c in course_sheets
            if any(s.get("section_id") == section_id for s in c.get("section_sheets", []))
        ]
    if course_id is not None:
        course_sheets = [c for c in course_sheets if c.get("course_id") == course_id]
    return {
        "sprint_numbers": sprint_numbers,
        "sprint_ids": _sprint_ids_by_number(sprints),
        "course_sheets": course_sheets,
        "batch_key": batch_key,
        "slot_date": slot_date.isoformat() if slot_date else None,
    }


@router.put("/marks/section")
def save_section_marks(
    data: VivaMarksSectionSaveRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Save or update marks for all teams in one section (all sprint columns)."""
    if data.mode not in ("save", "update"):
        raise HTTPException(status_code=400, detail="mode must be save or update")
    if not data.batch_key and not data.slot_date:
        raise HTTPException(status_code=400, detail="Provide batch_key or slot_date")
    sprints = _sprints_for_marks_scope(db, batch_key=data.batch_key, slot_date=data.slot_date)
    sprint_by_number = {s.sprint_number: s for s in sprints if s.sprint_number}
    if not sprint_by_number:
        raise HTTPException(status_code=404, detail="No sprints found for this batch or date")

    section = db.query(models.Section).filter(models.Section.id == data.section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    updated = 0
    for entry in data.entries:
        if entry.sprint_number not in sprint_by_number:
            continue
        if data.mode == "save" and entry.score is None:
            continue
        sprint = sprint_by_number[entry.sprint_number]
        team = (
            db.query(models.Team)
            .filter(models.Team.id == entry.team_id, models.Team.section_id == data.section_id)
            .first()
        )
        if not team:
            continue
        on_team = team.lead_id == entry.member_id or (
            db.query(models.TeamMembership)
            .filter(
                models.TeamMembership.team_id == team.id,
                models.TeamMembership.member_id == entry.member_id,
                models.TeamMembership.status == "accepted",
            )
            .first()
            is not None
        )
        if not on_team:
            continue
        rec = (
            db.query(models.VivaMemberScore)
            .filter(
                models.VivaMemberScore.sprint_id == sprint.id,
                models.VivaMemberScore.member_id == entry.member_id,
            )
            .first()
        )
        if not rec:
            rec = models.VivaMemberScore(
                sprint_id=sprint.id,
                team_id=team.id,
                member_id=entry.member_id,
            )
            db.add(rec)
        else:
            rec.team_id = team.id
        rec.score = entry.score
        if entry.notes is not None:
            rec.notes = (entry.notes or "").strip() or None
        rec.updated_by_id = admin.id
        updated += 1
    db.commit()
    return {"saved": updated, "section_id": data.section_id, "mode": data.mode}
