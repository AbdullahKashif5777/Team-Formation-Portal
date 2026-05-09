import asyncio
import threading
import csv
from io import StringIO
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app import models
from app.auth import get_current_user
from app.formation import is_formation_open, formation_window_debug
from app.ws_manager import manager
from app import email_utils
from sqlalchemy.orm import joinedload
from app.config import settings
from app.core.lookups import course_name_by_id

router = APIRouter(prefix="/api/teams", tags=["teams"])
member_router = APIRouter(prefix="/api/member", tags=["member"])


@router.get("/sections/{section_id}/formation-status")
def formation_status(
    section_id: int,
    _: models.User = Depends(get_current_user),
):
    return formation_window_debug(section_id)


class RosterCellUpdate(BaseModel):
    row_id: int
    column_name: str
    new_value: str | int | float | None = None
    section_id: int | None = None


# ─── helpers ────────────────────────────────────────────────────────────────

def _dt_utc_z(dt: datetime | None) -> str | None:
    """Serialize stored-naive-UTC datetimes as ISO 8601 with Z."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _membership_kind(m: models.TeamMembership) -> str | None:
    extra = getattr(m, "extra_data", None) or {}
    if isinstance(extra, dict):
        k = extra.get("kind")
        return str(k) if k is not None else None
    return None


def _is_lead_invite(m: models.TeamMembership) -> bool:
    return _membership_kind(m) == "lead_invite"


def cleanup_expired_requests(
    db: Session,
    *,
    section_id: int | None = None,
    team_id: int | None = None,
    member_id: int | None = None,
) -> int:
    """
    Delete any pending join-requests or invites older than 1 hour.

    Scoping is optional but recommended to keep the query tight:
    - section_id: expires items for a specific section (via Team.section_id)
    - team_id: expires items for a specific team
    - member_id: expires items for a specific student/member
    """
    cutoff = datetime.utcnow() - timedelta(hours=1)

    q = (
        db.query(models.TeamMembership)
        .join(models.Team, models.Team.id == models.TeamMembership.team_id)
        .filter(
            models.TeamMembership.status == "pending",
            models.TeamMembership.created_at < cutoff,
        )
    )
    if section_id is not None:
        q = q.filter(models.Team.section_id == int(section_id))
    if team_id is not None:
        q = q.filter(models.TeamMembership.team_id == int(team_id))
    if member_id is not None:
        q = q.filter(models.TeamMembership.member_id == int(member_id))

    ids = [row[0] for row in q.with_entities(models.TeamMembership.id).all()]
    if not ids:
        return 0
    count = (
        db.query(models.TeamMembership)
        .filter(models.TeamMembership.id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(count or 0)


def team_dict(team: models.Team) -> dict:
    accepted = [m for m in team.memberships if m.status == "accepted"]
    pending = [m for m in team.memberships if m.status == "pending"]
    pending_requests = [m for m in pending if not _is_lead_invite(m)]
    pending_invites = [m for m in pending if _is_lead_invite(m)]
    section_limit = (team.section.team_size_limit if team.section and team.section.team_size_limit else 4)
    member_slots_total = max(0, int(section_limit) - 1)  # exclude lead slot
    member_slots_filled = len(accepted)
    member_slots_available = max(0, member_slots_total - member_slots_filled)
    team_people_count = 1 + member_slots_filled  # lead + accepted members
    return {
        "id": team.id,
        "name": team.name,
        "lead_name": team.lead.name,
        "lead_email": team.lead.email,
        "lead_id": team.lead_id,
        "section_id": team.section_id,
        "section_name": team.section.name if team.section else "",
        "course_name": team.section.course.name if team.section and team.section.course else "",
        # Back-compat fields (member slots, not counting lead)
        "max_members": member_slots_total,
        "accepted_count": member_slots_filled,
        "slots_available": member_slots_available,
        # New section-based limit fields (counts lead + members)
        "team_size_limit": int(section_limit),
        "team_people_count": team_people_count,
        "member_slots_total": member_slots_total,
        "member_slots_filled": member_slots_filled,
        "member_slots_available": member_slots_available,
        "accepted_members": [
            {"id": m.member.id, "name": m.member.name,
             "student_id": m.member.student_id, "email": m.member.email}
            for m in accepted
        ],
        "pending_requests": [
            {"id": m.id, "member_name": m.member.name,
             "member_id": m.member.student_id, "member_email": m.member.email,
             "message": m.message, "created_at": m.created_at.isoformat()}
            for m in pending_requests
        ],
        "pending_invites": [
            {"id": m.id, "member_name": m.member.name,
             "member_id": m.member.student_id, "member_email": m.member.email,
             "message": m.message, "created_at": m.created_at.isoformat()}
            for m in pending_invites
        ],
    }


def _sync_sheet(team):
    """Refreshes the entire section's Google Sheet when a team changes."""
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON and team.section.google_sheet_url:
        threading.Thread(
            target=sheets_utils.sync_section_data,
            args=(team.section_id,),
            daemon=True,
        ).start()


def _notify_admins(db: Session, event: str, data: dict):
    """Broadcast team activity to all admins for live monitoring."""
    admin_ids = [u.id for u in db.query(models.User).filter(models.User.role == "admin").all()]
    if admin_ids:
        manager.fire(manager.broadcast_to_many(admin_ids, event, data))


def _broadcast_team_updated(db: Session, team: models.Team):
    """Broadcast updated team payload to all teammates (lead + accepted members)."""
    accepted_ids = [
        m.member_id for m in team.memberships
        if m.status == "accepted"
    ]
    user_ids = [team.lead_id] + accepted_ids
    payload = team_dict(team)
    manager.fire(manager.broadcast_to_many(user_ids, "team_updated", payload))


# ─── routes ─────────────────────────────────────────────────────────────────

@router.get("")
def list_teams(section_id: int | None = None, db: Session = Depends(get_db)):
    """Public: list all teams, optionally filtered by section."""
    query = (
        db.query(models.Team)
        .options(
            joinedload(models.Team.lead),
            joinedload(models.Team.section).joinedload(models.Section.course),
            joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .join(models.User)
    )
    if section_id:
        query = query.filter(models.Team.section_id == section_id)
    teams = query.all()
    return [team_dict(t) for t in teams]


@router.get("/my-team")
def my_team_info(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Section-aware dashboard payload for all user enrollments."""
    user_sections = (
        db.query(models.UserSection)
        .options(
            joinedload(models.UserSection.section).joinedload(models.Section.course),
        )
        .filter(models.UserSection.user_id == user.id)
        .all()
    )
    
    result = {"sections": []}
    
    for us in user_sections:
        sec = us.section
        # Clean up expired pending requests/invites so the dashboard never shows stale items.
        cleanup_expired_requests(db, section_id=sec.id, member_id=user.id)

        lead_team = (
            db.query(models.Team)
            .options(
                joinedload(models.Team.lead),
                joinedload(models.Team.section).joinedload(models.Section.course),
                joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
            )
            .filter(
                models.Team.lead_id == user.id,
                models.Team.section_id == sec.id,
            )
            .first()
        )

        section_role = "lead" if lead_team else "member"
        sec_info = {
            "section_id": sec.id,
            "section_name": sec.name,
            "course_name": course_name_by_id(int(sec.course.id)) if sec.course else "",
            "course_id": sec.course.id if sec.course else None,
            "role": section_role,
            "formation_start": _dt_utc_z(getattr(sec, "formation_start", None)),
            "formation_end": _dt_utc_z(getattr(sec, "formation_end", None)),
        }
        
        if lead_team:
            # Also expire stale pending items in the lead's team view.
            cleanup_expired_requests(db, section_id=sec.id, team_id=lead_team.id)
            sec_info["team"] = team_dict(lead_team)
        else:
            # Member logic
            accepted = (
                db.query(models.TeamMembership)
                .join(models.Team)
                .options(
                    joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
                    joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
                    joinedload(models.TeamMembership.team).selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
                )
                .filter(
                    models.TeamMembership.member_id == user.id,
                    models.TeamMembership.status == "accepted",
                    models.Team.section_id == sec.id,
                )
                .first()
            )
            
            if accepted:
                sec_info["status"] = "accepted"
                sec_info["request_id"] = accepted.id
                sec_info["team_name"] = accepted.team.name
                sec_info["lead_name"] = accepted.team.lead.name
                sec_info["lead_email"] = accepted.team.lead.email
                # Full member details via shared team payload (includes email and ids)
                sec_info["accepted_members"] = team_dict(accepted.team).get("accepted_members", [])
            else:
                pending = (
                    db.query(models.TeamMembership)
                    .join(models.Team)
                    .options(
                        joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
                        joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
                    )
                    .filter(
                        models.TeamMembership.member_id == user.id,
                        models.TeamMembership.status == "pending",
                        models.Team.section_id == sec.id,
                    )
                    .order_by(models.TeamMembership.created_at.desc())
                    .all()
                )

                pending_requests = [p for p in pending if not _is_lead_invite(p)]
                pending_invites = [p for p in pending if _is_lead_invite(p)]

                # Keep existing "pending" status semantics for student-initiated join requests only.
                if pending_requests:
                    sec_info["status"] = "pending"
                    sec_info["pending_requests"] = [
                        {"request_id": p.id, "team_id": p.team_id, "team_name": p.team.name, "lead_name": p.team.lead.name, "lead_email": p.team.lead.email}
                        for p in pending_requests
                    ]
                else:
                    sec_info["status"] = "none"

                # Invitations are additive and do not affect the legacy status field.
                if pending_invites:
                    sec_info["pending_invites"] = [
                        {"invite_id": p.id, "team_id": p.team_id, "team_name": p.team.name, "lead_name": p.team.lead.name, "lead_email": p.team.lead.email, "message": p.message, "created_at": p.created_at.isoformat()}
                        for p in pending_invites
                    ]

                # Preserve the historical "rejected" state only when there is no active join-request pending
                # and no active invitation pending.
                if (not pending_requests) and (not pending_invites):
                    rejected = db.query(models.TeamMembership).join(models.Team).filter(
                        models.TeamMembership.member_id == user.id,
                        models.TeamMembership.status == "rejected",
                        models.Team.section_id == sec.id
                    ).order_by(models.TeamMembership.created_at.desc()).first()

                    if rejected:
                        sec_info["status"] = "rejected"
                        sec_info["team_name"] = rejected.team.name
                        sec_info["lead_name"] = rejected.team.lead.name
                        
            # Include all available teams for this section so the frontend can list them
            teams_in_sec = (
                db.query(models.Team)
                .options(
                    joinedload(models.Team.lead),
                    joinedload(models.Team.section).joinedload(models.Section.course),
                    joinedload(models.Team.memberships).joinedload(models.TeamMembership.member),
                )
                .filter(models.Team.section_id == sec.id)
                .all()
            )
            sec_info["available_teams"] = [team_dict(t) for t in teams_in_sec]
            
        result["sections"].append(sec_info)

    return result


@router.get("/my-sections")
def my_sections(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return all enrolled sections with course names and section-specific role."""
    user_sections = (
        db.query(models.UserSection)
        .options(joinedload(models.UserSection.section).joinedload(models.Section.course))
        .filter(models.UserSection.user_id == user.id)
        .all()
    )
    sections = []

    for us in user_sections:
        sec = us.section
        is_lead_here = db.query(models.Team).filter(
            models.Team.lead_id == user.id,
            models.Team.section_id == sec.id
        ).first() is not None
        sections.append({
            "section_id": sec.id,
            "section_name": sec.name,
            "course_name": course_name_by_id(int(sec.course.id)) if sec.course else "",
            "role": "lead" if is_lead_here else "member",
        })

    return {"sections": sections}


class JoinRequest(BaseModel):
    team_id: int
    message: str | None = None


class LeadInviteRequest(BaseModel):
    user_id: int
    message: str | None = None


class MemberUpdate(BaseModel):
    name: str | None = None
    student_id: str | None = None


class TransferSectionRequest(BaseModel):
    current_section_id: int
    target_section_id: int


class ExitTeamRequest(BaseModel):
    section_id: int


@member_router.post("/transfer-section")
def transfer_section(
    payload: TransferSectionRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_section_id = int(payload.current_section_id or 0)
    target_section_id = int(payload.target_section_id or 0)
    if not current_section_id or not target_section_id:
        raise HTTPException(status_code=422, detail="current_section_id and target_section_id are required")
    if current_section_id == target_section_id:
        return {"ok": True}

    current_section = db.query(models.Section).filter(models.Section.id == current_section_id).first()
    target_section = db.query(models.Section).filter(models.Section.id == target_section_id).first()
    if not current_section or not target_section:
        raise HTTPException(status_code=404, detail="Section not found")
    if current_section.course_id != target_section.course_id:
        raise HTTPException(status_code=400, detail="Target section must be in the same course")

    # Must be enrolled in the current section.
    enrollment = (
        db.query(models.UserSection)
        .filter(models.UserSection.user_id == user.id, models.UserSection.section_id == current_section_id)
        .first()
    )
    if not enrollment:
        raise HTTPException(status_code=403, detail="You are not enrolled in this section")

    # Strict requirement: allow only if membership_status is 'Pending' or 'None' for the current section.
    accepted = (
        db.query(models.TeamMembership)
        .join(models.Team)
        .filter(
            models.TeamMembership.member_id == user.id,
            models.TeamMembership.status == "accepted",
            models.Team.section_id == current_section_id,
        )
        .first()
    )
    if accepted:
        raise HTTPException(status_code=400, detail="Section transfer blocked: already accepted in a team.")

    pending = (
        db.query(models.TeamMembership)
        .join(models.Team)
        .filter(
            models.TeamMembership.member_id == user.id,
            models.TeamMembership.status == "pending",
            models.Team.section_id == current_section_id,
        )
        .all()
    )
    # If user is in a rejected state only, block per strict requirement.
    if not pending:
        rejected = (
            db.query(models.TeamMembership)
            .join(models.Team)
            .filter(
                models.TeamMembership.member_id == user.id,
                models.TeamMembership.status == "rejected",
                models.Team.section_id == current_section_id,
            )
            .first()
        )
        if rejected:
            raise HTTPException(status_code=400, detail="Section transfer blocked: membership_status must be Pending or None.")

    # Ensure not already enrolled in target section.
    already_target = (
        db.query(models.UserSection)
        .filter(models.UserSection.user_id == user.id, models.UserSection.section_id == target_section_id)
        .first()
    )
    if already_target:
        raise HTTPException(status_code=400, detail="Already enrolled in target section")

    # Clean up any pending requests/invites in the old section (so leads don't see stale items).
    for m in pending:
        db.delete(m)

    # Update enrollment to target section (keeps one-section-per-course invariant).
    enrollment.section_id = target_section_id
    db.commit()
    return {"ok": True, "target_section_id": target_section_id}


@member_router.post("/exit-team")
async def exit_team(
    payload: ExitTeamRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Member-only: exit an accepted team for the given section.
    Restricted to the formation window, same as join/request operations.
    """
    section_id = int(payload.section_id or 0)
    if not section_id:
        raise HTTPException(status_code=422, detail="section_id is required")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Operation locked: You cannot leave a team once accepted.")
    if not is_formation_open(section_id):
        raise HTTPException(status_code=403, detail="Operation locked: Outside of scheduled formation window.")

    membership = (
        db.query(models.TeamMembership)
        .join(models.Team)
        .options(
            joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
            joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
            joinedload(models.TeamMembership.team).joinedload(models.Team.memberships),
        )
        .filter(
            models.TeamMembership.member_id == user.id,
            models.TeamMembership.status == "accepted",
            models.Team.section_id == section_id,
        )
        .first()
    )
    if not membership or not membership.team:
        raise HTTPException(status_code=404, detail="No accepted team found for this section")

    team = membership.team
    lead_id = int(team.lead_id)
    team_id = int(team.id)
    db.delete(membership)
    db.commit()

    # Notify the member (and refresh lead/team in real-time).
    manager.fire(manager.send_to(user.id, "exited_team", {"team_id": team_id, "section_id": section_id}))
    manager.fire(manager.send_to(lead_id, "team_updated", team_dict(team)))
    _broadcast_team_updated(db, team)

    _notify_admins(
        db,
        "admin_team_activity",
        {
            "kind": "member_exited",
            "section_id": section_id,
            "course_name": team.section.course.name if team.section and team.section.course else "",
            "section_name": team.section.name if team.section else "",
            "team_name": team.name,
            "lead_name": team.lead.name if team.lead else "",
            "member_name": user.name,
            "member_id": user.student_id,
            "member_email": user.email,
            "created_at": datetime.utcnow().isoformat(),
        },
    )

    return {"ok": True, "team_id": team_id, "section_id": section_id}


@router.post("/request")
async def send_join_request(
    data: JoinRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    team = (
        db.query(models.Team)
        .options(
            joinedload(models.Team.lead),
            joinedload(models.Team.section).joinedload(models.Section.course),
            joinedload(models.Team.memberships),
        )
        .filter(models.Team.id == data.team_id)
        .first()
    )
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if not is_formation_open(team.section_id):
        dbg = formation_window_debug(int(team.section_id or 0))
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Operation locked: Outside of scheduled formation window.",
                "formation": dbg,
            },
        )

    # Drop expired pending items before enforcing request limits.
    cleanup_expired_requests(db, section_id=team.section_id, member_id=user.id)

    # Check if user is enrolled in this section
    user_sec = db.query(models.UserSection).filter(
        models.UserSection.user_id == user.id,
        models.UserSection.section_id == team.section_id
    ).first()
    if not user_sec:
        raise HTTPException(status_code=403, detail="You are not enrolled in this section")

    # A user cannot send join requests in a section they are leading.
    own_team_in_section = db.query(models.Team).filter(
        models.Team.lead_id == user.id,
        models.Team.section_id == team.section_id
    ).first()
    if own_team_in_section:
        raise HTTPException(status_code=400, detail="You are already a lead in this section")

    # Check slots
    accepted_count = sum(1 for m in team.memberships if m.status == "accepted")
    section_limit = team.section.team_size_limit if team.section and team.section.team_size_limit else 4
    current_people = 1 + accepted_count  # lead + accepted
    if current_people >= section_limit:
        raise HTTPException(status_code=400, detail="Team is full")

    # Check already in a team IN THIS SECTION
    already_accepted = db.query(models.TeamMembership).join(models.Team).filter(
        models.TeamMembership.member_id == user.id,
        models.TeamMembership.status == "accepted",
        models.Team.section_id == team.section_id
    ).first()
    if already_accepted:
        raise HTTPException(status_code=400, detail="You are already in a team for this section")

    # A student can have only ONE active pending join-request per section (invites do not count).
    pending_in_section = (
        db.query(models.TeamMembership)
        .join(models.Team)
        .filter(
            models.TeamMembership.member_id == user.id,
            models.TeamMembership.status == "pending",
            models.Team.section_id == team.section_id,
        )
        .order_by(models.TeamMembership.created_at.desc())
        .all()
    )
    pending_join_requests = [m for m in pending_in_section if not _is_lead_invite(m)]
    if pending_join_requests:
        existing = pending_join_requests[0]
        if int(existing.team_id) == int(team.id):
            raise HTTPException(status_code=400, detail="You already have a pending request for this team")
        raise HTTPException(status_code=400, detail="You already have a pending request in this section")

    # Allow re-requesting the same team after rejection by reusing the existing row
    # (team_memberships enforces a unique constraint on member_id + team_id).
    existing_rejected = db.query(models.TeamMembership).filter(
        models.TeamMembership.member_id == user.id,
        models.TeamMembership.team_id == team.id,
        models.TeamMembership.status == "rejected",
    ).first()
    if existing_rejected:
        existing_rejected.status = "pending"
        existing_rejected.message = data.message
        existing_rejected.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing_rejected)

        membership = existing_rejected
        # Real-time: notify lead
        manager.fire(manager.send_to(team.lead_id, "new_request", {
            "request_id": membership.id,
            "member_name": user.name,
            "member_id": user.student_id,
            "member_email": user.email,
            "message": data.message,
            "team_name": team.name,
            "section_id": team.section_id,
            "created_at": membership.created_at.isoformat(),
        }))

        # Email lead (non-blocking)
        if settings.smtp_configured:
            email_utils.send_async(
                email_utils.send_join_request_to_lead,
                team.lead.email,
                team.lead.name,
                user.name,
                user.student_id,
                team.name,
                team.section.course.name,
                team.section.name,
                user.email,
            )
            email_utils.send_async(
                email_utils.send_join_request_confirmation_to_member,
                user.email,
                user.name,
                user.student_id,
                team.name,
                team.lead.name,
                team.section.course.name,
                team.section.name,
            )

        _notify_admins(db, "admin_team_activity", {
            "kind": "new_request",
            "section_id": team.section_id,
            "course_name": team.section.course.name if team.section and team.section.course else "",
            "section_name": team.section.name if team.section else "",
            "team_name": team.name,
            "lead_name": team.lead.name,
            "member_name": user.name,
            "member_id": user.student_id,
            "member_email": user.email,
            "created_at": membership.created_at.isoformat(),
        })

        return {"message": "Request sent", "request_id": membership.id}

    membership = models.TeamMembership(
        team_id=team.id,
        member_id=user.id,
        status="pending",
        message=data.message,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)

    # Real-time: notify lead
    manager.fire(manager.send_to(team.lead_id, "new_request", {
        "request_id": membership.id,
        "member_name": user.name,
        "member_id": user.student_id,
        "member_email": user.email,
        "message": data.message,
        "team_name": team.name,
        "section_id": team.section_id,
        "created_at": membership.created_at.isoformat(),
    }))

    # Email lead (non-blocking)
    if settings.smtp_configured:
        email_utils.send_async(
            email_utils.send_join_request_to_lead,
            team.lead.email,
            team.lead.name,
            user.name,
            user.student_id,
            team.name,
            team.section.course.name,
            team.section.name,
            user.email,
        )
        email_utils.send_async(
            email_utils.send_join_request_confirmation_to_member,
            user.email,
            user.name,
            user.student_id,
            team.name,
            team.lead.name,
            team.section.course.name,
            team.section.name,
        )

    _notify_admins(db, "admin_team_activity", {
        "kind": "new_request",
        "section_id": team.section_id,
        "course_name": team.section.course.name if team.section and team.section.course else "",
        "section_name": team.section.name if team.section else "",
        "team_name": team.name,
        "lead_name": team.lead.name,
        "member_name": user.name,
        "member_id": user.student_id,
        "member_email": user.email,
        "created_at": membership.created_at.isoformat(),
    })

    return {"message": "Request sent", "request_id": membership.id}


@router.post("/request/{request_id}/accept")
async def accept_request(
    request_id: int,
    lead: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = (
        db.query(models.TeamMembership)
        .options(
            joinedload(models.TeamMembership.member),
            joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
            joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
            joinedload(models.TeamMembership.team).joinedload(models.Team.memberships),
        )
        .filter(models.TeamMembership.id == request_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Request not found")
    if not is_formation_open(membership.team.section_id):
        raise HTTPException(status_code=403, detail="Operation locked: Outside of scheduled formation window.")
    if membership.team.lead_id != lead.id:
        raise HTTPException(status_code=403, detail="Not your team")
    if membership.status != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")

    # Check max limit
    accepted_count = sum(1 for m in membership.team.memberships if m.status == "accepted")
    section_limit = membership.team.section.team_size_limit if membership.team.section and membership.team.section.team_size_limit else 4
    current_people = 1 + accepted_count  # lead + accepted
    if current_people >= section_limit:
        raise HTTPException(status_code=400, detail="Team is already full")

    # Check if student is already in another team IN THIS SECTION
    already_accepted = db.query(models.TeamMembership).join(models.Team).filter(
        models.TeamMembership.member_id == membership.member_id,
        models.TeamMembership.status == "accepted",
        models.Team.section_id == membership.team.section_id
    ).first()
    if already_accepted:
        raise HTTPException(status_code=400, detail="Student is already in another team for this section")

    membership.status = "accepted"
    membership.updated_at = datetime.utcnow()
    
    # Auto-cancel other pending requests for this member IN THIS SECTION
    other_pending = db.query(models.TeamMembership).join(models.Team).filter(
        models.TeamMembership.member_id == membership.member_id,
        models.TeamMembership.status == "pending",
        models.TeamMembership.id != membership.id,
        models.Team.section_id == membership.team.section_id
    ).all()
    
    for p in other_pending:
        other_lead_id = p.team.lead_id
        other_req_id = p.id
        db.delete(p)
        manager.fire(
            manager.send_to(
                other_lead_id,
                "request_cancelled",
                {"request_id": other_req_id, "section_id": p.team.section_id},
            )
        )
    db.commit()
    db.refresh(membership)

    member = membership.member
    team = membership.team

    # Notify member in real-time
    manager.fire(manager.send_to(member.id, "request_accepted", {
        "team_name": team.name,
        "lead_name": lead.name,
        "team_id": team.id,
        "section_id": team.section_id,
    }))

    # Notify lead with updated team data
    manager.fire(manager.send_to(lead.id, "team_updated", team_dict(team)))
    # Notify all teammates (lead + accepted members) for real-time refresh
    _broadcast_team_updated(db, team)

    # Email member
    if settings.smtp_configured:
        email_utils.send_async(
            email_utils.send_accepted_to_member,
            member.email,
            member.name,
            member.student_id,
            team.name,
            lead.name,
            team.section.course.name,
            team.section.name,
        )
        email_utils.send_async(
            email_utils.send_request_accepted_to_lead,
            lead.email,
            lead.name,
            member.name,
            member.student_id,
            team.name,
            team.section.course.name,
            team.section.name,
            member.email,
        )

    # Google Sheets sync
    _sync_sheet(team)

    _notify_admins(db, "admin_team_activity", {
        "kind": "accepted",
        "section_id": team.section_id,
        "course_name": team.section.course.name if team.section and team.section.course else "",
        "section_name": team.section.name if team.section else "",
        "team_name": team.name,
        "lead_name": lead.name,
        "member_name": member.name,
        "member_id": member.student_id,
        "member_email": member.email,
        "created_at": datetime.utcnow().isoformat(),
    })

    return {"message": "Member accepted", "team": team_dict(team)}


@router.post("/request/{request_id}/reject")
async def reject_request(
    request_id: int,
    lead: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = (
        db.query(models.TeamMembership)
        .options(
            joinedload(models.TeamMembership.member),
            joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
        )
        .filter(models.TeamMembership.id == request_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Request not found")
    if membership.team.lead_id != lead.id:
        raise HTTPException(status_code=403, detail="Not your team")
    if membership.status != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")

    membership.status = "rejected"
    membership.updated_at = datetime.utcnow()
    db.commit()

    member = membership.member
    team = membership.team

    manager.fire(manager.send_to(member.id, "request_rejected", {
        "team_name": team.name,
        "lead_name": lead.name,
        "section_id": team.section_id,
    }))
    manager.fire(manager.send_to(lead.id, "team_updated", team_dict(team)))

    if settings.smtp_configured:
        email_utils.send_async(
            email_utils.send_rejected_to_member,
            member.email,
            member.name,
            member.student_id,
            team.name,
            lead.name,
            team.section.course.name,
            team.section.name,
        )

    _notify_admins(db, "admin_team_activity", {
        "kind": "rejected",
        "section_id": team.section_id,
        "course_name": team.section.course.name if team.section and team.section.course else "",
        "section_name": team.section.name if team.section else "",
        "team_name": team.name,
        "lead_name": lead.name,
        "member_name": member.name,
        "member_id": member.student_id,
        "member_email": member.email,
        "created_at": datetime.utcnow().isoformat(),
    })

    return {"message": "Request rejected"}


@router.delete("/request/{request_id}/cancel")
async def cancel_request(
    request_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = db.query(models.TeamMembership).filter(
        models.TeamMembership.id == request_id,
        models.TeamMembership.member_id == user.id,
        models.TeamMembership.status == "pending",
    ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Pending request not found")

    lead_id = membership.team.lead_id
    section_id = membership.team.section_id
    db.delete(membership)
    db.commit()

    manager.fire(manager.send_to(lead_id, "request_cancelled", {"request_id": request_id, "section_id": section_id}))
    _notify_admins(db, "admin_team_activity", {
        "kind": "cancelled",
        "section_id": section_id,
        "team_name": membership.team.name,
        "lead_name": membership.team.lead.name,
        "member_name": user.name,
        "member_id": user.student_id,
        "member_email": user.email,
        "created_at": datetime.utcnow().isoformat(),
    })
    return {"message": "Request cancelled"}


@router.get("/sections/{section_id}/eligible-students")
def eligible_students_for_invite(
    section_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lead-only: list students in the same section who are not yet accepted into any team
    for this section, and who do not already have a pending invite for this lead's team.
    """
    lead_team = (
        db.query(models.Team)
        .filter(models.Team.lead_id == current_user.id, models.Team.section_id == section_id)
        .first()
    )
    if not lead_team:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Ensure expired invites don't block eligibility.
    cleanup_expired_requests(db, section_id=section_id, team_id=lead_team.id)

    accepted_member_ids = {
        int(r[0])
        for r in (
            db.query(models.TeamMembership.member_id)
            .join(models.Team)
            .filter(models.TeamMembership.status == "accepted", models.Team.section_id == section_id)
            .all()
        )
        if r and r[0] is not None
    }

    pending_team_memberships = (
        db.query(models.TeamMembership)
        .filter(models.TeamMembership.team_id == lead_team.id, models.TeamMembership.status == "pending")
        .all()
    )
    pending_invited_ids = {int(m.member_id) for m in pending_team_memberships if _is_lead_invite(m)}

    excluded_ids = accepted_member_ids | pending_invited_ids

    q = (
        db.query(models.User)
        .join(models.UserSection, models.UserSection.user_id == models.User.id)
        .filter(
            models.UserSection.section_id == section_id,
            # IMPORTANT: eligibility is section-scoped (a user may be a global lead but a member in this section)
            models.UserSection.role == "member",
        )
        .order_by(models.User.name.asc())
    )
    if excluded_ids:
        q = q.filter(~models.User.id.in_(sorted(excluded_ids)))

    users = q.all()
    return [
        {"id": u.id, "name": u.name, "student_id": u.student_id, "email": u.email}
        for u in users
    ]


@router.post("/sections/{section_id}/invite")
async def send_lead_invite(
    section_id: int,
    payload: LeadInviteRequest,
    lead: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lead-only: invite a student in the same section to join the lead's team."""
    team = (
        db.query(models.Team)
        .options(
            joinedload(models.Team.section).joinedload(models.Section.course),
            joinedload(models.Team.memberships),
        )
        .filter(models.Team.lead_id == lead.id, models.Team.section_id == section_id)
        .first()
    )
    if not team:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Drop expired pending invites before enforcing the 3-invite cap.
    cleanup_expired_requests(db, section_id=section_id, team_id=team.id)

    target = db.query(models.User).filter(models.User.id == int(payload.user_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="Student not found")

    enrolled = (
        db.query(models.UserSection)
        .filter(models.UserSection.user_id == target.id, models.UserSection.section_id == section_id)
        .first()
    )
    if not enrolled:
        raise HTTPException(status_code=400, detail="Student is not enrolled in this section")
    if (getattr(enrolled, "role", None) or "member") != "member":
        # A user can be globally a lead/admin, but in this section they must be enrolled as a member to be invited.
        raise HTTPException(status_code=400, detail="Student is not eligible to join as a member in this section")

    active_pending_for_team = (
        db.query(models.TeamMembership)
        .filter(models.TeamMembership.team_id == team.id, models.TeamMembership.status == "pending")
        .order_by(models.TeamMembership.created_at.desc())
        .all()
    )
    active_invites = [m for m in active_pending_for_team if _is_lead_invite(m)]
    if len(active_invites) >= 3:
        raise HTTPException(status_code=400, detail="Invite limit reached (max 3 active invitations)")

    # Check team capacity (lead + accepted members <= section limit)
    accepted_count = sum(1 for m in team.memberships if m.status == "accepted")
    section_limit = team.section.team_size_limit if team.section and team.section.team_size_limit else 4
    current_people = 1 + accepted_count
    if current_people >= section_limit:
        raise HTTPException(status_code=400, detail="Team is full")

    # Check student not already accepted in any team in this section
    already_accepted = (
        db.query(models.TeamMembership)
        .join(models.Team)
        .filter(
            models.TeamMembership.member_id == target.id,
            models.TeamMembership.status == "accepted",
            models.Team.section_id == section_id,
        )
        .first()
    )
    if already_accepted:
        raise HTTPException(status_code=400, detail="Student is already in a team for this section")

    # Prevent duplicates (any pending membership for this team blocks inviting again)
    existing_pending = (
        db.query(models.TeamMembership)
        .filter(
            models.TeamMembership.member_id == target.id,
            models.TeamMembership.team_id == team.id,
            models.TeamMembership.status == "pending",
        )
        .first()
    )
    if existing_pending:
        raise HTTPException(status_code=400, detail="Student already has a pending item for this team")

    membership = models.TeamMembership(
        team_id=team.id,
        member_id=target.id,
        status="pending",
        message=payload.message,
        extra_data={"kind": "lead_invite"},
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)

    manager.fire(manager.send_to(target.id, "team_invite", {
        "invite_id": membership.id,
        "team_id": team.id,
        "team_name": team.name,
        "lead_name": lead.name,
        "lead_email": lead.email,
        "section_id": team.section_id,
        "created_at": membership.created_at.isoformat(),
        "message": payload.message,
    }))

    manager.fire(manager.send_to(lead.id, "team_updated", team_dict(team)))

    if settings.smtp_configured:
        cn = team.section.course.name if team.section and team.section.course else ""
        sn = team.section.name if team.section else ""
        email_utils.send_async(
            email_utils.send_team_invite_to_member,
            target.email,
            target.name,
            target.student_id,
            team.name,
            lead.name,
            cn,
            sn,
            payload.message,
        )

    _notify_admins(db, "admin_team_activity", {
        "kind": "invite_sent",
        "section_id": team.section_id,
        "course_name": team.section.course.name if team.section and team.section.course else "",
        "section_name": team.section.name if team.section else "",
        "team_name": team.name,
        "lead_name": lead.name,
        "member_name": target.name,
        "member_id": target.student_id,
        "member_email": target.email,
        "created_at": membership.created_at.isoformat(),
    })

    return {"message": "Invite sent", "invite_id": membership.id}


@router.post("/invites/{invite_id}/accept")
async def accept_lead_invite(
    invite_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = (
        db.query(models.TeamMembership)
        .options(
            joinedload(models.TeamMembership.team).joinedload(models.Team.lead),
            joinedload(models.TeamMembership.team).joinedload(models.Team.section).joinedload(models.Section.course),
            joinedload(models.TeamMembership.team).joinedload(models.Team.memberships),
        )
        .filter(models.TeamMembership.id == invite_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Invite not found")
    if membership.status != "pending" or not _is_lead_invite(membership):
        raise HTTPException(status_code=400, detail="Invite already processed")
    if membership.member_id != user.id:
        raise HTTPException(status_code=403, detail="Not your invite")

    team = membership.team
    if not team:
        raise HTTPException(status_code=400, detail="Invalid invite team")

    # Check max limit
    accepted_count = sum(1 for m in team.memberships if m.status == "accepted")
    section_limit = team.section.team_size_limit if team.section and team.section.team_size_limit else 4
    current_people = 1 + accepted_count  # lead + accepted
    if current_people >= section_limit:
        raise HTTPException(status_code=400, detail="Team is already full")

    # Check if student is already in another team IN THIS SECTION
    already_accepted = db.query(models.TeamMembership).join(models.Team).filter(
        models.TeamMembership.member_id == membership.member_id,
        models.TeamMembership.status == "accepted",
        models.Team.section_id == team.section_id
    ).first()
    if already_accepted:
        raise HTTPException(status_code=400, detail="You are already in another team for this section")

    membership.status = "accepted"
    membership.updated_at = datetime.utcnow()

    # Auto-cancel other pending requests/invites for this member IN THIS SECTION
    other_pending = db.query(models.TeamMembership).join(models.Team).filter(
        models.TeamMembership.member_id == membership.member_id,
        models.TeamMembership.status == "pending",
        models.TeamMembership.id != membership.id,
        models.Team.section_id == team.section_id
    ).all()

    for p in other_pending:
        other_lead_id = p.team.lead_id
        other_req_id = p.id
        db.delete(p)
        manager.fire(
            manager.send_to(
                other_lead_id,
                "request_cancelled",
                {"request_id": other_req_id, "section_id": p.team.section_id},
            )
        )

    db.commit()
    db.refresh(membership)

    lead = team.lead

    manager.fire(manager.send_to(user.id, "invite_accepted", {
        "team_name": team.name,
        "lead_name": lead.name if lead else "",
        "team_id": team.id,
        "section_id": team.section_id,
    }))

    if lead:
        manager.fire(manager.send_to(lead.id, "team_updated", team_dict(team)))
    _broadcast_team_updated(db, team)

    # Email member + lead (reuse existing templates; keeps behavior consistent with acceptance)
    if settings.smtp_configured:
        email_utils.send_async(
            email_utils.send_accepted_to_member,
            user.email,
            user.name,
            user.student_id,
            team.name,
            lead.name if lead else "",
            team.section.course.name if team.section and team.section.course else "",
            team.section.name if team.section else "",
        )
        if lead:
            email_utils.send_async(
                email_utils.send_request_accepted_to_lead,
                lead.email,
                lead.name,
                user.name,
                user.student_id,
                team.name,
                team.section.course.name if team.section and team.section.course else "",
                team.section.name if team.section else "",
                user.email,
            )

    _sync_sheet(team)

    _notify_admins(db, "admin_team_activity", {
        "kind": "invite_accepted",
        "section_id": team.section_id,
        "course_name": team.section.course.name if team.section and team.section.course else "",
        "section_name": team.section.name if team.section else "",
        "team_name": team.name,
        "lead_name": lead.name if lead else "",
        "member_name": user.name,
        "member_id": user.student_id,
        "member_email": user.email,
        "created_at": datetime.utcnow().isoformat(),
    })

    return {"message": "Invite accepted", "team": team_dict(team)}


@router.post("/invites/{invite_id}/decline")
async def decline_lead_invite(
    invite_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = db.query(models.TeamMembership).filter(models.TeamMembership.id == invite_id).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Invite not found")
    if membership.status != "pending" or not _is_lead_invite(membership):
        raise HTTPException(status_code=400, detail="Invite already processed")
    if membership.member_id != user.id:
        raise HTTPException(status_code=403, detail="Not your invite")

    membership.status = "rejected"
    membership.updated_at = datetime.utcnow()
    db.commit()

    team = membership.team
    lead = team.lead if team else None
    if lead:
        manager.fire(manager.send_to(lead.id, "invite_declined", {
            "invite_id": membership.id,
            "member_name": user.name,
            "member_id": user.student_id,
            "member_email": user.email,
            "team_id": team.id,
            "team_name": team.name,
            "section_id": team.section_id,
        }))
        manager.fire(manager.send_to(lead.id, "team_updated", team_dict(team)))

    if lead and settings.smtp_configured:
        cn = team.section.course.name if team.section and team.section.course else ""
        sn = team.section.name if team.section else ""
        email_utils.send_async(
            email_utils.send_invite_declined_to_lead,
            lead.email,
            lead.name,
            user.name,
            team.name,
            cn,
            sn,
            user.student_id,
            user.email,
        )

    return {"message": "Invite declined"}


@router.delete("/members/{member_id}/remove")
async def remove_member(
    member_id: int,
    section_id: int,
    lead: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not is_formation_open(section_id):
        raise HTTPException(status_code=403, detail="Operation locked: Outside of scheduled formation window.")
    if lead.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    try:
        team = (
            db.query(models.Team)
            .options(joinedload(models.Team.section).joinedload(models.Section.course))
            .filter(models.Team.lead_id == lead.id, models.Team.section_id == section_id)
            .first()
        )
        if not team:
            raise HTTPException(status_code=404, detail="Team not found for this section")

        membership = db.query(models.TeamMembership).filter(
            models.TeamMembership.team_id == team.id,
            models.TeamMembership.member_id == member_id,
            models.TeamMembership.status == "accepted"
        ).first()

        if not membership:
            raise HTTPException(status_code=404, detail="Member not found in your team")

        # Capture member info before deletion to avoid detached access after commit.
        removed_member_name = membership.member.name
        removed_member_sid = membership.member.student_id
        removed_member_email = membership.member.email

        db.delete(membership)
        db.commit()

        manager.fire(manager.send_to(member_id, "removed_from_team", {
            "team_name": team.name,
            "lead_name": lead.name,
            "section_id": team.section_id
        }))

        # Email member (non-blocking)
        if settings.smtp_configured:
            email_utils.send_async(
                email_utils.send_removed_from_team_to_member,
                removed_member_email,
                removed_member_name,
                removed_member_sid,
                team.name,
                lead.name,
                team.section.course.name,
                team.section.name,
            )

        _sync_sheet(team)

        # Email admin notifications only to addresses listed in ADMIN_EMAILS env.
        # Keeps gating consistent with admin.py (clearing ADMIN_EMAILS disables all admin CCs).
        if settings.smtp_configured:
            for admin_email in settings.admin_emails_list:
                email_utils.send_async(
                    email_utils.send_member_removed_by_lead_to_admin,
                    admin_email,
                    lead.name,
                    removed_member_name,
                    team.name,
                    team.section.course.name,
                    team.section.name,
                    removed_member_sid,
                    removed_member_email,
                )

        _notify_admins(db, "admin_team_activity", {
            "kind": "removed",
            "section_id": team.section_id,
            "course_name": team.section.course.name if team.section and team.section.course else "",
            "section_name": team.section.name if team.section else "",
            "team_name": team.name,
            "lead_name": lead.name,
            "member_name": removed_member_name,
            "member_id": removed_member_sid,
            "member_email": removed_member_email,
            "created_at": datetime.utcnow().isoformat(),
        })

        # Re-query fresh team state for response.
        fresh_team = db.query(models.Team).filter(models.Team.id == team.id).first()
        if fresh_team:
            _broadcast_team_updated(db, fresh_team)
        return {"message": "Member removed", "team": team_dict(fresh_team)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove member: {str(e)}")


@router.get("/sections/{section_id}/roster")
def get_lead_section_roster(section_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Verify the user is a lead in this section
    is_lead = (
        db.query(models.Team.id)
        .filter(models.Team.lead_id == current_user.id, models.Team.section_id == section_id)
        .first()
    )

    if not is_lead:
        raise HTTPException(status_code=403, detail="Not authorized")

    assignment = (
        db.query(models.UserSection)
        .options(
            joinedload(models.UserSection.section).joinedload(models.Section.course),
            joinedload(models.UserSection.section).selectinload(models.Section.teams).joinedload(models.Team.lead),
            joinedload(models.UserSection.section)
            .selectinload(models.Section.teams)
            .selectinload(models.Team.memberships)
            .joinedload(models.TeamMembership.member),
        )
        .filter(models.UserSection.user_id == current_user.id, models.UserSection.section_id == section_id)
        .first()
    )
    
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Section assignment for section {section_id} not found")

    section = assignment.section
    # Global cache for static-ish lookups; avoids repeated DB hits in other codepaths.
    # (Here it's already eager-loaded, so this remains zero-extra-queries.)
    _course_name_cache: dict[int, str] = {}
    if section and section.course:
        _course_name_cache[int(section.course.id)] = str(section.course.name)

    roster = []
    for i, team in enumerate(section.teams, 1):
        display_team_name = f"Team {i} ({team.lead.name})"
        roster.append({
            "user_id": team.lead.id,
            "role": "Lead",
            "name": team.lead.name,
            "student_id": team.lead.student_id,
            "email": team.lead.email,
            "team_name": display_team_name,
            "status": "Active"
        })
        for membership in team.memberships:
            if membership.status == "accepted":
                roster.append({
                    "user_id": membership.member.id,
                    "role": "Member",
                    "name": membership.member.name,
                    "student_id": membership.member.student_id,
                    "email": membership.member.email,
                    "team_name": display_team_name,
                    "status": "Accepted"
                })
    
    return {
        "section_name": section.name,
        "course_name": _course_name_cache.get(int(section.course.id), section.course.name) if section.course else "",
        "team_size_limit": getattr(section, "team_size_limit", 4),
        "roster": roster
    }


@router.get("/sections/{section_id}/roster/csv")
def export_lead_section_roster_csv(section_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
                                   token: str | None = Query(default=None)):
    is_lead = db.query(models.Team).filter(
        models.Team.lead_id == current_user.id,
        models.Team.section_id == section_id
    ).first()
    
    if not is_lead:
        raise HTTPException(status_code=403, detail="Not authorized")

    assignment = (
        db.query(models.UserSection)
        .options(
            joinedload(models.UserSection.section).joinedload(models.Section.course),
            joinedload(models.UserSection.section).selectinload(models.Section.teams).joinedload(models.Team.lead),
            joinedload(models.UserSection.section)
            .selectinload(models.Section.teams)
            .selectinload(models.Team.memberships)
            .joinedload(models.TeamMembership.member),
        )
        .filter(models.UserSection.user_id == current_user.id, models.UserSection.section_id == section_id)
        .first()
    )
    
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Section assignment for section {section_id} not found")
    
    section = assignment.section
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ROLE", "NAME", "STUDENT ID", "EMAIL", "TEAM NAME", "STATUS"])
    
    for i, team in enumerate(section.teams, 1):
        display_name = f"Team {i} ({team.lead.name})"
        writer.writerow(["Lead", team.lead.name, team.lead.student_id, team.lead.email, display_name, "Active"])
        for membership in team.memberships:
            if membership.status == "accepted":
                writer.writerow(["Member", membership.member.name, membership.member.student_id, membership.member.email, display_name, "Accepted"])
    
    output.seek(0)
    filename = f"roster_{section.course.name}_{section.name}.csv".replace(" ", "_")
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.patch("/roster/member/{user_id}")
def update_lead_roster_member(user_id: int, data: MemberUpdate, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "lead":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Verify this member is in a section where this lead also has a section assignment
    member = db.query(models.User).filter(models.User.id == user_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
        
    # Check if lead and member share a section
    # Actually, simpler: check if member is in a team where current_user is the lead
    is_in_team = db.query(models.Team).filter(
        models.Team.lead_id == current_user.id
    ).join(models.TeamMembership).filter(
        models.TeamMembership.member_id == user_id
    ).first()
    
    # Or if the user IS the lead themselves (editing their own ID?)
    is_self = current_user.id == user_id
    
    if not is_in_team and not is_self:
        raise HTTPException(status_code=403, detail="Not authorized to edit this member")

    if data.name is not None:
        member.name = data.name.strip()
    if data.student_id is not None:
        member.student_id = data.student_id.strip().upper()
        
    try:
        db.commit()
        return {"message": "Updated"}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Student ID already in use")


@router.get("/sections/{section_id}/roster-sheet")
def get_lead_section_roster_sheet(
    section_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Verify the user is a lead in this section
    is_lead = (
        db.query(models.Team)
        .filter(models.Team.lead_id == current_user.id, models.Team.section_id == section_id)
        .first()
        is not None
    )
    if not is_lead:
        raise HTTPException(status_code=403, detail="Not authorized")

    assignment = (
        db.query(models.UserSection)
        .options(
            joinedload(models.UserSection.section).joinedload(models.Section.course),
            joinedload(models.UserSection.section).joinedload(models.Section.user_sections),
            joinedload(models.UserSection.section).selectinload(models.Section.teams).joinedload(models.Team.lead),
            joinedload(models.UserSection.section)
            .selectinload(models.Section.teams)
            .selectinload(models.Team.memberships)
            .joinedload(models.TeamMembership.member),
        )
        .filter(models.UserSection.user_id == current_user.id, models.UserSection.section_id == section_id)
        .first()
    )
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Section assignment for section {section_id} not found")

    section = assignment.section
    teams = [
        {
            "team_id": t.id,
            "team_name": t.name,
            "lead_name": t.lead.name if t.lead else "",
        }
        for t in (section.teams or [])
    ]
    rows: list[dict] = []
    for team in section.teams:
        for membership in team.memberships:
            if membership.status != "accepted":
                continue
            rows.append(
                {
                    "membership_id": membership.id,
                    "student_name": membership.member.name,
                    "student_id": membership.member.student_id,
                    "team_id": team.id,
                    "team_name": team.name,
                    "lead_name": team.lead.name if team.lead else "",
                    "extra_data": getattr(membership, "extra_data", None) or {},
                }
            )

    return {
        "section_name": section.name,
        "course_name": section.course.name,
        "team_size_limit": getattr(section, "team_size_limit", 4),
        "column_config": getattr(section, "column_config", None) or [],
        "teams": teams,
        "roster": rows,
    }


@router.patch("/roster/cell-update")
def roster_cell_update_lead(
    payload: RosterCellUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "lead":
        raise HTTPException(status_code=403, detail="Not authorized")

    membership = db.query(models.TeamMembership).filter(models.TeamMembership.id == payload.row_id).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    section_id = membership.team.section_id if membership.team else None
    if not section_id:
        raise HTTPException(status_code=400, detail="Invalid membership section")

    # Lead must own this section
    owns_section = (
        db.query(models.Team)
        .filter(models.Team.lead_id == current_user.id, models.Team.section_id == section_id)
        .first()
        is not None
    )
    if not owns_section:
        raise HTTPException(status_code=403, detail="Not authorized for this section")

    key = (payload.column_name or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="column_name is required")

    # Lead can only edit keys that exist in section.column_config (dynamic columns),
    # and never base identity fields.
    section = db.query(models.Section).filter(models.Section.id == section_id).first()
    allowed_keys = set()
    if section and section.column_config:
        for c in section.column_config:
            k = (c or {}).get("key")
            if k:
                allowed_keys.add(str(k))
    if key not in allowed_keys:
        raise HTTPException(status_code=403, detail="Not allowed to edit this column")

    if membership.extra_data is None:
        membership.extra_data = {}
    membership.extra_data[key] = payload.new_value
    flag_modified(membership, "extra_data")
    membership.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}
