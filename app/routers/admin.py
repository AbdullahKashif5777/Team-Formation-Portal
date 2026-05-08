import csv
from io import StringIO, BytesIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.auth import require_admin, hash_password, validate_umt_email
from app.database import get_db
from app.config import settings
from app import email_utils
import logging

router = APIRouter(prefix="/api/admin", tags=["admin"])

logger = logging.getLogger("uvicorn.error")


def _warn_if_admin_emails_missing() -> None:
    """
    Admin notification emails are sent to ADMIN_EMAILS. If SMTP is configured but
    ADMIN_EMAILS is empty, emails will be silently skipped unless we warn.
    """
    if settings.smtp_configured and not settings.admin_emails_list:
        logger.warning("ADMIN_EMAILS is empty; admin notification emails will not be sent.")


class CourseCreate(BaseModel):
    name: str


class SectionCreate(BaseModel):
    course_id: int
    name: str
    team_size_limit: int = 4
    formation_start: datetime | None = None
    formation_end: datetime | None = None

    @field_validator("team_size_limit")
    @classmethod
    def validate_team_size_limit(cls, v: int):
        if v is None:
            return 4
        if int(v) < 2:
            raise ValueError("team_size_limit must be at least 2 (lead + 1 member)")
        return int(v)


class SectionUpdate(BaseModel):
    team_size_limit: int
    formation_start: datetime | None = None
    formation_end: datetime | None = None

    @field_validator("team_size_limit")
    @classmethod
    def validate_team_size_limit(cls, v: int):
        if int(v) < 2:
            raise ValueError("team_size_limit must be at least 2 (lead + 1 member)")
        return int(v)


class LeadCreate(BaseModel):
    name: str
    email: str
    employee_id: str | None = None
    password: str
    section_ids: list[int]

class MemberUpdate(BaseModel):
    name: str | None = None
    student_id: str | None = None


class RosterUpdate(BaseModel):
    row_id: int
    column_name: str
    value: str | int | float | None = None


class RosterCellUpdate(BaseModel):
    row_id: int
    column_name: str
    new_value: str | int | float | None = None


class RosterConfigUpdate(BaseModel):
    section_id: int
    column_config: list[dict]


def _member_slots_from_section_limit(section_limit: int) -> int:
    """Member slots per team (excluding lead), derived from total team size (lead + members)."""
    return max(0, int(section_limit) - 1)


def _course_dict(c: models.Course) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "sections": [
            {
                "id": s.id,
                "name": s.name,
                "team_size_limit": getattr(s, "team_size_limit", 4),
                "formation_start": s.formation_start.isoformat() if getattr(s, "formation_start", None) else None,
                "formation_end": s.formation_end.isoformat() if getattr(s, "formation_end", None) else None,
            }
            for s in c.sections
        ],
    }


@router.get("/courses")
def list_courses(_: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    courses = db.query(models.Course).options(selectinload(models.Course.sections)).all()
    return [_course_dict(c) for c in courses]


@router.post("/courses")
def create_course(data: CourseCreate, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Course name is required")
    existing = db.query(models.Course).filter(models.Course.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Course already exists")
    course = models.Course(name=name)
    db.add(course)
    db.commit()
    db.refresh(course)

    if settings.smtp_configured:
        _warn_if_admin_emails_missing()
        for admin_email in settings.admin_emails_list:
            email_utils.send_async(
                email_utils.send_course_created_notice,
                admin_email,
                course.name,
                "N/A",
            )

    return {"message": "Course created", "course": {"id": course.id, "name": course.name}}


@router.delete("/courses/{course_id}")
def delete_course(course_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    course = (
        db.query(models.Course)
        .options(
            selectinload(models.Course.sections)
            .selectinload(models.Section.teams)
            .joinedload(models.Team.lead),
            selectinload(models.Course.sections)
            .selectinload(models.Section.teams)
            .selectinload(models.Team.memberships)
            .joinedload(models.TeamMembership.member),
        )
        .filter(models.Course.id == course_id)
        .first()
    )
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    course_name = course.name
    
    # Collect all affected leads and members
    leads_to_notify = []
    members_to_notify = []
    for section in course.sections:
        for team in section.teams:
            # Lead
            leads_to_notify.append({"email": team.lead.email, "name": team.lead.name})
            # Members
            for membership in team.memberships:
                if membership.status == "accepted":
                    members_to_notify.append({
                        "email": membership.member.email,
                        "name": membership.member.name,
                        "team_name": team.name
                    })

    try:
        db.delete(course)
        db.commit()

        if settings.smtp_configured:
            _warn_if_admin_emails_missing()
            # Notify Admins
            for admin_email in settings.admin_emails_list:
                email_utils.send_async(
                    email_utils.send_course_section_removed_notice,
                    admin_email,
                    "Course",
                    course_name,
                )
            
            # Notify Leads
            for l in leads_to_notify:
                email_utils.send_async(
                    email_utils.send_lead_removed_notice,
                    l["email"],
                    l["name"],
                )
            
            # Notify Members
            for m in members_to_notify:
                email_utils.send_async(
                    email_utils.send_member_team_removed_notice,
                    m["email"],
                    m["name"],
                    m["team_name"],
                )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove course: {str(e)}")
    return {"message": "Course deleted"}


@router.post("/sections")
def create_section(data: SectionCreate, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    course = db.query(models.Course).filter(models.Course.id == data.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    section_name = data.name.strip()
    if not section_name:
        raise HTTPException(status_code=422, detail="Section name is required")
    existing = db.query(models.Section).filter(
        models.Section.course_id == course.id, models.Section.name == section_name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Section already exists in this course")
    section = models.Section(
        course_id=course.id,
        name=section_name,
        team_size_limit=data.team_size_limit,
        formation_start=data.formation_start,
        formation_end=data.formation_end,
    )
    db.add(section)
    db.commit()
    db.refresh(section)

    if settings.smtp_configured:
        _warn_if_admin_emails_missing()
        for admin_email in settings.admin_emails_list:
            email_utils.send_async(
                email_utils.send_section_created_notice,
                admin_email,
                section.name,
                course.name,
            )
    
    return {
        "message": "Section created",
        "section": {
            "id": section.id,
            "name": section.name,
            "course_id": course.id,
            "team_size_limit": section.team_size_limit,
            "formation_start": section.formation_start.isoformat() if section.formation_start else None,
            "formation_end": section.formation_end.isoformat() if section.formation_end else None,
        },
    }


@router.put("/sections/{section_id}")
@router.patch("/sections/{section_id}")
def update_section(section_id: int, data: SectionUpdate, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    section = (
        db.query(models.Section)
        .options(selectinload(models.Section.teams))
        .filter(models.Section.id == section_id)
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    section.team_size_limit = data.team_size_limit
    # Preserve existing timer fields unless explicitly sent by the client.
    fields_set = getattr(data, "model_fields_set", set())
    if "formation_start" in fields_set:
        section.formation_start = data.formation_start
    if "formation_end" in fields_set:
        section.formation_end = data.formation_end
    member_slots_total = _member_slots_from_section_limit(section.team_size_limit)
    teams_updated = 0
    for team in section.teams:
        team.max_members = member_slots_total
        teams_updated += 1
    print(
        f"[admin update_section] section_id={section_id} team_size_limit={data.team_size_limit} "
        f"member_slots_total={member_slots_total} teams_updated={teams_updated}"
    )
    db.commit()
    db.refresh(section)
    return {
        "message": "Section updated",
        "section": {
            "id": section.id,
            "name": section.name,
            "course_id": section.course_id,
            "team_size_limit": section.team_size_limit,
            "formation_start": section.formation_start.isoformat() if section.formation_start else None,
            "formation_end": section.formation_end.isoformat() if section.formation_end else None,
        },
    }


@router.delete("/sections/{section_id}")
def delete_section(section_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    section = (
        db.query(models.Section)
        .options(
            joinedload(models.Section.course),
            selectinload(models.Section.teams).joinedload(models.Team.lead),
            selectinload(models.Section.teams).selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .filter(models.Section.id == section_id)
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    
    section_name = section.name
    
    # Collect all affected leads and members
    leads_to_notify = []
    members_to_notify = []
    for team in section.teams:
        # Lead
        leads_to_notify.append({"email": team.lead.email, "name": team.lead.name})
        # Members
        for membership in team.memberships:
            if membership.status == "accepted":
                members_to_notify.append({
                    "email": membership.member.email,
                    "name": membership.member.name,
                    "team_name": team.name
                })

    try:
        db.delete(section)
        db.commit()

        if settings.smtp_configured:
            _warn_if_admin_emails_missing()
            # Notify Admin
            for admin_email in settings.admin_emails_list:
                email_utils.send_async(
                    email_utils.send_course_section_removed_notice,
                    admin_email,
                    "Section",
                    section_name,
                )
            
            # Notify Leads
            for l in leads_to_notify:
                email_utils.send_async(
                    email_utils.send_lead_removed_notice,
                    l["email"],
                    l["name"],
                )
            
            # Notify Members
            for m in members_to_notify:
                email_utils.send_async(
                    email_utils.send_member_team_removed_notice,
                    m["email"],
                    m["name"],
                    m["team_name"],
                )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove section: {str(e)}")
    return {"message": "Section deleted"}


@router.delete("/leads/{lead_id}")
def delete_lead(lead_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    lead = (
        db.query(models.User)
        .options(
            selectinload(models.User.led_teams)
            .selectinload(models.Team.memberships)
            .joinedload(models.TeamMembership.member)
        )
        .filter(models.User.id == lead_id, models.User.role == "lead")
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    
    lead_email = lead.email
    lead_name = lead.name
    lead_sid = lead.student_id

    # Collect assigned course/section details before deletion for the removal email.
    assigned_pairs = []
    try:
        user_sections = (
            db.query(models.UserSection)
            .options(joinedload(models.UserSection.section).joinedload(models.Section.course))
            .filter(models.UserSection.user_id == lead.id)
            .all()
        )
        for us in user_sections:
            sec = us.section
            if not sec:
                continue
            assigned_pairs.append(
                {
                    "course": (sec.course.name if sec.course else ""),
                    "section": (sec.name or ""),
                }
            )
    except Exception:
        assigned_pairs = []
    
    # Collect all accepted members in all teams led by this lead to notify them.
    members_to_notify = []
    for team in lead.led_teams:
        for membership in team.memberships:
            if membership.status == "accepted":
                members_to_notify.append({
                    "email": membership.member.email,
                    "name": membership.member.name,
                    "team_name": team.name
                })

    try:
        db.delete(lead)
        db.commit()

        if settings.smtp_configured:
            _warn_if_admin_emails_missing()
            # Notify the lead
            email_utils.send_async(
                email_utils.send_lead_removed_notice,
                lead_email,
                lead_name,
                lead_sid,
                assigned_pairs,
            )
            
            # Notify all members
            for m in members_to_notify:
                email_utils.send_async(
                    email_utils.send_member_team_removed_notice,
                    m["email"],
                    m["name"],
                    m["team_name"],
                )
            
            # Notify Admins
            for admin_email in settings.admin_emails_list:
                email_utils.send_async(
                    email_utils.send_course_section_removed_notice,
                    admin_email,
                    "Lead Account",
                    lead_name,
                )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove lead: {str(e)}")
    return {"message": "Lead deleted"}


@router.delete("/leads/{lead_id}/sections/{section_id}")
def remove_lead_from_section(
    lead_id: int,
    section_id: int,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove a lead from one section (one course). Disbands that team. Deletes the lead account if no assignments remain."""
    lead = db.query(models.User).filter(models.User.id == lead_id, models.User.role == "lead").first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    user_section = (
        db.query(models.UserSection)
        .options(joinedload(models.UserSection.section).joinedload(models.Section.course))
        .filter(
            models.UserSection.user_id == lead_id,
            models.UserSection.section_id == section_id,
        )
        .first()
    )

    team = (
        db.query(models.Team)
        .options(
            selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
            joinedload(models.Team.section).joinedload(models.Section.course),
        )
        .filter(models.Team.lead_id == lead_id, models.Team.section_id == section_id)
        .first()
    )

    if not user_section and not team:
        raise HTTPException(status_code=404, detail="Lead has no assignment for this section")

    sec = (user_section.section if user_section else None) or (team.section if team else None)
    removed_assignments = [
        {
            "course": (sec.course.name if sec and sec.course else ""),
            "section": (sec.name or "") if sec else "",
        }
    ]

    members_to_notify = []
    if team:
        for membership in team.memberships:
            if membership.status == "accepted" and membership.member:
                members_to_notify.append(
                    {
                        "email": membership.member.email,
                        "name": membership.member.name,
                        "team_name": team.name,
                    }
                )

    lead_email = lead.email
    lead_name = lead.name
    lead_sid = lead.student_id

    try:
        if team:
            db.delete(team)
        if user_section:
            db.delete(user_section)
        db.flush()

        remaining_links = (
            db.query(models.UserSection)
            .options(joinedload(models.UserSection.section).joinedload(models.Section.course))
            .filter(models.UserSection.user_id == lead_id, models.UserSection.role == "lead")
            .all()
        )
        removed_account = len(remaining_links) == 0

        if removed_account:
            db.delete(lead)

        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to remove lead from section: {str(e)}")

    remaining = [
        {
            "section_id": us.section.id,
            "section_name": us.section.name,
            "course_name": us.section.course.name if us.section and us.section.course else "",
        }
        for us in remaining_links
    ]

    if settings.smtp_configured:
        _warn_if_admin_emails_missing()
        for m in members_to_notify:
            email_utils.send_async(
                email_utils.send_member_team_removed_notice,
                m["email"],
                m["name"],
                m["team_name"],
            )
        email_utils.send_async(
            email_utils.send_lead_removed_notice,
            lead_email,
            lead_name,
            lead_sid,
            removed_assignments,
        )
        for admin_email in settings.admin_emails_list:
            email_utils.send_async(
                email_utils.send_course_section_removed_notice,
                admin_email,
                "Lead Account",
                lead_name,
            )

    return {"message": "Lead removed from section", "removed_account": removed_account, "remaining": remaining}


@router.get("/leads")
def list_leads(_: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    leads = (
        db.query(models.User)
        .options(
            selectinload(models.User.user_sections)
            .joinedload(models.UserSection.section)
            .joinedload(models.Section.course)
        )
        .filter(models.User.role == "lead")
        .all()
    )
    result = []
    for lead in leads:
        sections = []
        for us in lead.user_sections:
            if us.role != "lead":
                continue
            sec = us.section
            sections.append({
                "section_id": sec.id,
                "section_name": sec.name,
                "course_name": sec.course.name if sec.course else "",
            })
        result.append({
            "id": lead.id,
            "name": lead.name,
            "email": lead.email,
            "employee_id": lead.student_id,
            "sections": sections,
        })
    return result


@router.post("/leads")
def create_lead(data: LeadCreate, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    if not data.section_ids:
        raise HTTPException(status_code=422, detail="Select at least one section")

    existing = db.query(models.User).filter(models.User.email == data.email.lower()).first()
    was_new_lead_account = existing is None
    plain_password: str | None = None
    assigned_pairs: list[dict[str, str]] = []
    # Password is required only for brand-new lead accounts.
    if not existing:
        if len(data.password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    else:
        # For upgrades, password is optional; if provided, enforce minimum length.
        if data.password and len(data.password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    # Enforce Email-ID match
    email_local = data.email.split('@')[0].upper()
    if data.employee_id:
        if email_local != data.employee_id.upper():
             raise HTTPException(status_code=400, detail=f"ID '{data.employee_id}' does not match email '{data.email}'. Must be '{email_local}'.")
        existing_by_id = db.query(models.User).filter(models.User.student_id == data.employee_id.upper()).first()
        if existing_by_id and (not existing or existing_by_id.id != existing.id):
            raise HTTPException(status_code=400, detail="ID already registered")

    if existing:
        # Upgrade existing account to lead (preserves user record; adds/updates section roles)
        lead = existing
        lead.role = "lead"
        lead.name = data.name.strip() or lead.name
        if data.employee_id:
            lead.student_id = data.employee_id.strip()
        if data.password:
            lead.password_hash = hash_password(data.password)
    else:
        # Capture plain password only transiently for the one-time welcome email.
        plain_password = data.password
        lead = models.User(
            name=data.name.strip(),
            email=data.email.lower(),
            student_id=data.employee_id.strip() if data.employee_id else None,
            password_hash=hash_password(data.password),
            role="lead",
        )
        db.add(lead)
        db.flush()

    for sec_id in data.section_ids:
        section = db.query(models.Section).filter(models.Section.id == sec_id).first()
        if not section:
            continue
        assigned_pairs.append(
            {
                "course": (section.course.name if section.course else ""),
                "section": (section.name or ""),
            }
        )
        existing_link = db.query(models.UserSection).filter(
            models.UserSection.user_id == lead.id,
            models.UserSection.section_id == sec_id
        ).first()
        if not existing_link:
            db.add(models.UserSection(user_id=lead.id, section_id=sec_id, role="lead"))
        else:
            existing_link.role = "lead"
        existing_team = db.query(models.Team).filter(
            models.Team.lead_id == lead.id,
            models.Team.section_id == sec_id,
        ).first()
        if not existing_team:
            slots = _member_slots_from_section_limit(section.team_size_limit or 4)
            db.add(
                models.Team(
                    name=f"{lead.name}'s Team",
                    lead_id=lead.id,
                    section_id=sec_id,
                    max_members=slots,
                )
            )

    db.commit()

    # We no longer sync to Google Sheets, using internal roster management

    # One-time welcome email with plain password (only for brand-new lead accounts).
    # Never log the password; keep it only in memory for this call.
    if settings.smtp_configured and was_new_lead_account and plain_password:
        email_utils.send_async(
            email_utils.send_lead_welcome_email,
            lead.email,
            lead.name,
            plain_password,
            assigned_pairs,
        )

    if settings.smtp_configured:
        _warn_if_admin_emails_missing()
        assigned_names = []
        for sec_id in data.section_ids:
            s = db.query(models.Section).filter(models.Section.id == sec_id).first()
            if s:
                assigned_names.append(f"{s.course.name} ({s.name})")

        sections_str = " — ".join(assigned_names) if assigned_names else "N/A"

        email_utils.send_async(
            email_utils.send_lead_assigned_notice,
            lead.email,
            lead.name,
            sections_str,
            is_new_account=was_new_lead_account,
        )
        for admin_email in settings.admin_emails_list:
            email_utils.send_async(
                email_utils.send_admin_new_lead_notice,
                admin_email,
                lead.name,
                lead.email,
                sections_str,
            )

    return {"message": "Lead created successfully"}


@router.get("/overview")
def overview(_: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    courses_count = db.query(models.Course).count()
    sections_count = db.query(models.Section).count()
    leads_count = db.query(models.User).filter(models.User.role == "lead").count()
    members_count = db.query(models.User).filter(models.User.role == "member").count()
    teams = (
        db.query(models.Team)
        .options(
            joinedload(models.Team.lead),
            joinedload(models.Team.section).joinedload(models.Section.course),
            selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .all()
    )

    teams_payload = []
    pending_total = 0
    accepted_total = 0
    for t in teams:
        accepted = [m for m in t.memberships if m.status == "accepted"]
        pending = [m for m in t.memberships if m.status == "pending"]
        pending_total += len(pending)
        accepted_total += len(accepted)
        section_limit = int(t.section.team_size_limit) if t.section and t.section.team_size_limit else 4
        member_slots_total = _member_slots_from_section_limit(section_limit)
        if t.max_members != member_slots_total:
            print(
                f"[admin overview] team_id={t.id} stored_max_members={t.max_members} "
                f"computed_member_slots={member_slots_total} section_team_size_limit={section_limit}"
            )
        teams_payload.append({
            "team_id": t.id,
            "team_name": t.name,
            "course_name": t.section.course.name if t.section and t.section.course else "",
            "section_name": t.section.name if t.section else "",
            "lead_name": t.lead.name,
            "lead_email": t.lead.email,
            "accepted_count": len(accepted),
            "pending_count": len(pending),
            "team_size_limit": section_limit,
            "member_slots_total": member_slots_total,
            "max_members": member_slots_total,
            "members": [
                {"name": m.member.name, "student_id": m.member.student_id, "email": m.member.email}
                for m in accepted
            ],
        })

    return {
        "stats": {
            "courses": courses_count,
            "sections": sections_count,
            "leads": leads_count,
            "members": members_count,
            "teams": len(teams_payload),
            "accepted_memberships": accepted_total,
            "pending_requests": pending_total,
        },
        "teams": teams_payload,
    }


@router.get("/sections/{section_id}/roster")
def get_section_roster(section_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    section = (
        db.query(models.Section)
        .options(
            joinedload(models.Section.course),
            selectinload(models.Section.teams).joinedload(models.Team.lead),
            selectinload(models.Section.teams).selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .filter(models.Section.id == section_id)
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    
    roster = []
    # Add Teams and their members
    # Add Teams and their members with professional numbering
    for i, team in enumerate(section.teams, 1):
        display_team_name = f"Team {i} ({team.lead.name})"
        # Lead
        roster.append({
            "user_id": team.lead.id,
            "role": "Lead",
            "name": team.lead.name,
            "student_id": team.lead.student_id,
            "email": team.lead.email,
            "team_name": display_team_name,
            "status": "Active"
        })
        # Members
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
        "course_name": section.course.name,
        "team_size_limit": getattr(section, "team_size_limit", 4),
        "roster": roster
    }


@router.get("/sections/{section_id}/roster/csv")
def export_section_roster_csv(section_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db),
                              token: str | None = Query(default=None)):
    section = (
        db.query(models.Section)
        .options(
            joinedload(models.Section.course),
            selectinload(models.Section.teams).joinedload(models.Team.lead),
            selectinload(models.Section.teams).selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .filter(models.Section.id == section_id)
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail=f"Section {section_id} not found")
    
    output = StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(["ROLE", "NAME", "STUDENT ID", "EMAIL", "TEAM NAME", "STATUS"])
    
    # Data
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
def update_member_roster_data(user_id: int, data: MemberUpdate, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if data.name is not None:
        user.name = data.name.strip()
    
    if data.student_id is not None:
        new_sid = data.student_id.strip().upper()
        # Basic check: ID should match email local part if possible, but we allow override for corrections
        user.student_id = new_sid
    
    try:
        db.commit()
        return {"message": "Member updated", "name": user.name, "student_id": user.student_id}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Student ID already in use")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sections/{section_id}/roster-sheet")
def get_section_roster_sheet(section_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    section = (
        db.query(models.Section)
        .options(
            joinedload(models.Section.course),
            selectinload(models.Section.teams).joinedload(models.Team.lead),
            selectinload(models.Section.teams).selectinload(models.Team.memberships).joinedload(models.TeamMembership.member),
        )
        .filter(models.Section.id == section_id)
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

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
def roster_cell_update(
    payload: RosterCellUpdate,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    membership = db.query(models.TeamMembership).filter(models.TeamMembership.id == payload.row_id).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    key = (payload.column_name or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="column_name is required")

    if membership.extra_data is None:
        membership.extra_data = {}

    membership.extra_data[key] = payload.new_value
    flag_modified(membership, "extra_data")
    membership.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.put("/roster/config-update")
def roster_config_update(
    payload: RosterConfigUpdate,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    section = db.query(models.Section).filter(models.Section.id == payload.section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    section.column_config = payload.column_config
    flag_modified(section, "column_config")
    db.commit()
    return {"ok": True}


@router.patch("/roster/update")
@router.patch("/roster/update/")
def update_roster_cell_admin(
    payload: RosterUpdate,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    membership = db.query(models.TeamMembership).filter(models.TeamMembership.id == payload.row_id).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    col = (payload.column_name or "").strip()
    allowed = {"student_name", "student_id", "team_name"}
    if col not in allowed:
        raise HTTPException(status_code=422, detail="Invalid column_name")

    try:
        if col == "student_name":
            membership.member.name = ("" if payload.value is None else str(payload.value)).strip()
        elif col == "student_id":
            membership.member.student_id = ("" if payload.value is None else str(payload.value)).strip().upper() or None
        elif col == "team_name":
            membership.team.name = ("" if payload.value is None else str(payload.value)).strip()

        membership.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
    except ValueError:
        db.rollback()
        raise HTTPException(status_code=422, detail="Invalid value type")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
