from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List

from app.database import get_db
from app.config import settings
from app import models
from app.auth import (
    hash_password, verify_password, create_access_token, decode_token,
    validate_umt_email, validate_student_id, get_current_user,
)
from app import email_utils
from sqlalchemy.orm import joinedload, selectinload
import threading

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    name: str
    email: str
    student_id: str | None = None
    password: str
    section_ids: List[int] = []

    @field_validator("email")
    @classmethod
    def email_must_be_umt(cls, v):
        if not validate_umt_email(v):
            raise ValueError("Email must end with @umt.edu.pk")
        return v.lower()

    @field_validator("student_id")
    @classmethod
    def sid_format(cls, v):
        if v and not validate_student_id(v):
            raise ValueError("Student ID must start with F or S followed by 10 digits (e.g. F2023065011 or S2023065011)")
        return v.upper() if v else v


class LoginRequest(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.get("/courses")
def get_courses(db: Session = Depends(get_db)):
    courses = db.query(models.Course).options(selectinload(models.Course.sections)).all()
    result = []
    for c in courses:
        result.append({
            "id": c.id,
            "name": c.name,
            "sections": [{"id": s.id, "name": s.name} for s in c.sections]
        })
    return result


@router.post("/register")
def register(data: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    normalized_email = (data.email or "").strip().lower()
    # Check if email is a declared lead
    is_lead = normalized_email in settings.lead_emails_list
    is_admin = normalized_email in settings.admin_emails_list
    is_member = (not is_lead) and (not is_admin)

    if is_member and not data.student_id:
        raise HTTPException(status_code=422, detail="Student ID is required for members")

    existing_user = db.query(models.User).filter(models.User.email == normalized_email).first()
    # Back-compat: if email lookup fails but the same account exists by student_id, treat it as the same user.
    if not existing_user and data.student_id:
        by_sid = db.query(models.User).filter(models.User.student_id == data.student_id.upper()).first()
        if by_sid and (by_sid.email or "").strip().lower() == normalized_email:
            existing_user = by_sid
    if existing_user:
        # Existing account: allow adding enrollments only if password matches (self-service add-section).
        # This preserves the "no duplicate emails" rule while enabling cross-course enrollment.
        if verify_password(data.password, existing_user.password_hash) and existing_user.role != "admin":
            # Enforce: at most one section per course for member enrollments.
            # (Users may enroll across different courses.)
            requested_sections = (
                db.query(models.Section)
                .filter(models.Section.id.in_(list(map(int, data.section_ids or []))))
                .all()
            )
            seen_course_ids: set[int] = set()
            for s in requested_sections:
                if s.course_id in seen_course_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="You can only enroll in one section per course. Deselect extra sections from the same course.",
                    )
                seen_course_ids.add(int(s.course_id))

            for sec_id in data.section_ids:
                section = db.query(models.Section).filter(models.Section.id == sec_id).first()
                if not section:
                    continue

                already_in_same_course = (
                    db.query(models.UserSection)
                    .join(models.Section, models.UserSection.section_id == models.Section.id)
                    .filter(models.UserSection.user_id == existing_user.id, models.Section.course_id == section.course_id)
                    .first()
                    is not None
                )
                if already_in_same_course:
                    raise HTTPException(
                        status_code=400,
                        detail="Already enrolled in a section for this course. You can only enroll in one section per course.",
                    )

                link = (
                    db.query(models.UserSection)
                    .filter(models.UserSection.user_id == existing_user.id, models.UserSection.section_id == section.id)
                    .first()
                )
                if not link:
                    db.add(models.UserSection(user_id=existing_user.id, section_id=section.id, role="member"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(status_code=400, detail="Could not add section enrollment")

            token = create_access_token({"sub": str(existing_user.id), "role": existing_user.role})
            return {
                "token": token,
                "access_token": token,
                "token_type": "bearer",
                "user_role": existing_user.role,
                "role": existing_user.role,
                "name": existing_user.name,
                "user_id": existing_user.id,
            }

        # Allow a pre-declared lead email to "activate lead access" for an existing account
        # by linking sections + creating missing teams. This keeps normal registration behavior unchanged.
        if data.email in settings.lead_emails_list and existing_user.role != "admin":
            # Ensure their account can access lead-only endpoints (still section-constrained elsewhere).
            existing_user.role = "lead"
            if data.name and data.name.strip():
                existing_user.name = data.name.strip()
            if data.password and len(data.password) >= 8:
                existing_user.password_hash = hash_password(data.password)

            for sec_id in data.section_ids:
                section = db.query(models.Section).filter(models.Section.id == sec_id).first()
                if not section:
                    continue
                link = (
                    db.query(models.UserSection)
                    .filter(models.UserSection.user_id == existing_user.id, models.UserSection.section_id == section.id)
                    .first()
                )
                if not link:
                    db.add(models.UserSection(user_id=existing_user.id, section_id=section.id, role="lead"))
                else:
                    link.role = "lead"

                # Auto-create team for lead in each section (if missing)
                team = db.query(models.Team).filter(models.Team.lead_id == existing_user.id, models.Team.section_id == section.id).first()
                if not team:
                    db.add(models.Team(name=f"{existing_user.name}'s Team", lead_id=existing_user.id, section_id=section.id))

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(status_code=400, detail="Could not update lead sections")

            token = create_access_token({"sub": str(existing_user.id), "role": existing_user.role})
            return {
                "token": token,
                "access_token": token,
                "token_type": "bearer",
                "user_role": existing_user.role,
                "role": existing_user.role,
                "name": existing_user.name,
                "user_id": existing_user.id,
            }

        raise HTTPException(status_code=400, detail="Email already registered")

    # Member-only email verification: do not create DB user until link is clicked.
    if is_member:
        # Enforce Email-ID match (same behavior as existing registration).
        email_local = normalized_email.split("@")[0].upper()
        if not data.student_id:
            raise HTTPException(status_code=422, detail="Student ID is required for members")
        if email_local != data.student_id.upper():
            raise HTTPException(
                status_code=400,
                detail=f"Student ID '{data.student_id}' does not match email '{normalized_email}'. Must be '{email_local}'.",
            )

        # Members can only pick one section per course at registration time.
        requested_sections = (
            db.query(models.Section)
            .filter(models.Section.id.in_(list(map(int, data.section_ids or []))))
            .all()
        )
        seen_course_ids: set[int] = set()
        for s in requested_sections:
            if s.course_id in seen_course_ids:
                raise HTTPException(
                    status_code=400,
                    detail="You can only enroll in one section per course. Deselect extra sections from the same course.",
                )
            seen_course_ids.add(int(s.course_id))

        # Pre-hash password for later DB insertion (password itself never stored in token).
        password_hash = hash_password(data.password)
        verify_token = create_access_token(
            {
                "purpose": "member_verify",
                "email": normalized_email,
                "name": data.name,
                "student_id": data.student_id.upper(),
                "section_ids": list(map(int, data.section_ids or [])),
                "password_hash": password_hash,
            },
            expires_delta=timedelta(hours=1),
        )

        configured_base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        if configured_base:
            base_url = configured_base
        else:
            host = (request.headers.get("host") or "").strip()
            if host:
                base_url = f"{request.url.scheme}://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")

        verify_link = f"{base_url}/api/auth/verify?token={verify_token}"

        # Add basic course/section context for email (use first section for display).
        section_name = "—"
        course_name = "—"
        first_section_id = int(data.section_ids[0]) if (data.section_ids and len(data.section_ids) > 0) else 0
        if first_section_id:
            sec = (
                db.query(models.Section)
                .options(joinedload(models.Section.course))
                .filter(models.Section.id == first_section_id)
                .first()
            )
            if sec:
                section_name = (sec.name or "").strip() or "—"
                if sec.course:
                    course_name = (sec.course.name or "").strip() or "—"

        if settings.smtp_configured:
            email_utils.send_async(
                email_utils.send_member_verification_email,
                normalized_email,
                data.name,
                verify_link,
                course_name,
                section_name,
                data.student_id.upper(),
            )

        return {"message": "Verification email sent. Please check your inbox to create your account."}

    # Enforce Email-ID match
    email_local = normalized_email.split('@')[0].upper()
    if data.student_id:
        if email_local != data.student_id.upper():
            raise HTTPException(status_code=400, detail=f"Student ID '{data.student_id}' does not match email '{normalized_email}'. Must be '{email_local}'.")
        
        if db.query(models.User).filter(models.User.student_id == data.student_id.upper()).first():
            raise HTTPException(status_code=400, detail="Student ID already registered")

    user = models.User(
        name=data.name,
        email=normalized_email,
        student_id=data.student_id.upper() if data.student_id else None,
        password_hash=hash_password(data.password),
        role="admin" if is_admin else ("lead" if is_lead else "member"),
    )
    db.add(user)
    db.flush()

    # Link user to sections
    if not is_lead and not is_admin:
        # Members can only pick one section per course at registration time.
        requested_sections = (
            db.query(models.Section)
            .filter(models.Section.id.in_(list(map(int, data.section_ids or []))))
            .all()
        )
        seen_course_ids: set[int] = set()
        for s in requested_sections:
            if s.course_id in seen_course_ids:
                raise HTTPException(
                    status_code=400,
                    detail="You can only enroll in one section per course. Deselect extra sections from the same course.",
                )
            seen_course_ids.add(int(s.course_id))

    for sec_id in data.section_ids:
        section = db.query(models.Section).filter(models.Section.id == sec_id).first()
        if section:
            user_sec = models.UserSection(
                user_id=user.id,
                section_id=section.id,
                role=("lead" if is_lead else "member"),
            )
            db.add(user_sec)
            db.flush()
            
            # Auto-create team for lead in each section
            if is_lead:
                team = models.Team(name=f"{data.name}'s Team", lead_id=user.id, section_id=section.id)
                db.add(team)

    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email or Student ID already registered")

    # Notify user by email (non-blocking when SMTP is configured).
    if settings.smtp_configured:
        section_name = "—"
        course_name = "—"
        first_section_id = int(data.section_ids[0]) if (data.section_ids and len(data.section_ids) > 0) else 0
        if first_section_id:
            sec = (
                db.query(models.Section)
                .options(joinedload(models.Section.course))
                .filter(models.Section.id == first_section_id)
                .first()
            )
            if sec:
                section_name = (sec.name or "").strip() or "—"
                if sec.course:
                    course_name = (sec.course.name or "").strip() or "—"

        email_utils.send_async(
            email_utils.send_registration_notice,
            user.email,
            user.name,
            user.email,
            user.role,
            user.student_id,
            None,
            section_name,
            course_name,
        )

    # Auto-login: reuse the same JWT payload as /login
    token = create_access_token({"sub": str(user.id), "role": user.role})
    return {
        "token": token,
        # Backward-compatible fields (existing frontend may still read these)
        "access_token": token,
        "token_type": "bearer",
        "user_role": user.role,
        "role": user.role,
        "name": user.name,
        "user_id": user.id,
    }


@router.get("/verify", response_class=HTMLResponse)
def verify_member(token: str, request: Request, db: Session = Depends(get_db)):
    payload = decode_token(token)
    if payload.get("purpose") != "member_verify":
        raise HTTPException(status_code=400, detail="Invalid verification token")

    email = (payload.get("email") or "").strip().lower()
    name = (payload.get("name") or "").strip()
    student_id = (payload.get("student_id") or "").strip().upper() or None
    section_ids = payload.get("section_ids") or []
    password_hash = payload.get("password_hash")

    if not email or not name or not student_id or not isinstance(section_ids, list) or not password_hash:
        raise HTTPException(status_code=400, detail="Invalid verification token")

    # Idempotent: if already created, just redirect to login with success flag.
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/") or str(request.base_url).rstrip("/")
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={base}/?verified=1"></head>
<body>Verified. Redirecting…</body></html>"""
        )

    # Enforce member constraints again (one section per course, and SID uniqueness).
    if db.query(models.User).filter(models.User.student_id == student_id).first():
        raise HTTPException(status_code=400, detail="Student ID already registered")

    requested_sections = (
        db.query(models.Section)
        .filter(models.Section.id.in_(list(map(int, section_ids or []))))
        .all()
    )
    seen_course_ids: set[int] = set()
    for s in requested_sections:
        if s.course_id in seen_course_ids:
            raise HTTPException(
                status_code=400,
                detail="You can only enroll in one section per course.",
            )
        seen_course_ids.add(int(s.course_id))

    user = models.User(
        name=name,
        email=email,
        student_id=student_id,
        password_hash=password_hash,
        role="member",
    )
    db.add(user)
    db.flush()

    for sec_id in section_ids:
        section = db.query(models.Section).filter(models.Section.id == int(sec_id)).first()
        if not section:
            continue
        db.add(models.UserSection(user_id=user.id, section_id=section.id, role="member"))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Could not verify account")

    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/") or str(request.base_url).rstrip("/")
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={base}/?verified=1&tab=login"></head>
<body>Email verified. Redirecting to login…</body></html>"""
    )


@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email.lower()).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")

    token = create_access_token({"sub": str(user.id), "role": user.role})

    # Role Selector (append-only): if user belongs to multiple sections, let frontend choose portal+section.
    if user.role != "admin":
        memberships = (
            db.query(models.UserSection)
            .filter(models.UserSection.user_id == user.id)
            .all()
        )
        options = []
        for us in memberships:
            sec = us.section
            if not sec:
                continue
            # Back-compat: if role wasn't populated historically, infer lead by owning a team in that section.
            inferred_lead = (
                db.query(models.Team)
                .filter(models.Team.lead_id == user.id, models.Team.section_id == sec.id)
                .first()
                is not None
            )
            section_role = (getattr(us, "role", None) or "").strip().lower() or ("lead" if inferred_lead else "member")
            if section_role not in {"lead", "member"}:
                section_role = "member"
            options.append(
                {
                    "section_id": sec.id,
                    "section_name": sec.name,
                    "course_name": sec.course.name if sec.course else "",
                    "role": section_role,
                    "portal": ("/lead" if section_role == "lead" else "/member"),
                }
            )

        if len(options) > 1:
            return {
                "access_token": token,
                "role": user.role,  # global (admin/lead/member) for backward-compat
                "name": user.name,
                "user_id": user.id,
                "action": "select_role",
                "options": options,
            }
        elif len(options) == 1:
            return {
                "access_token": token,
                "role": user.role,
                "name": user.name,
                "user_id": user.id,
                "section_id": options[0]["section_id"],
                "section_role": options[0]["role"],
            }

    return {"access_token": token, "role": user.role, "name": user.name, "user_id": user.id}


@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email.lower()).first()
    if user:
        reset_token = create_access_token(
            {"sub": str(user.id), "purpose": "password_reset"},
            expires_delta=timedelta(hours=1),
        )
        # Use the public landing page for password resets (works for all roles).
        portal_path = "/"
        configured_base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        if configured_base:
            base_url = configured_base
        else:
            host = (request.headers.get("host") or "").strip()
            if host:
                base_url = f"{request.url.scheme}://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
        reset_link = f"{base_url}{portal_path}?reset_token={reset_token}"

        if settings.smtp_configured:
            email_utils.send_async(
                email_utils.send_password_reset,
                user.email,
                user.name,
                reset_link,
            )
    # Always return success to avoid email enumeration.
    return {"message": "If an account exists, a reset link has been sent."}


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest, db: Session = Depends(get_db)):
    if len(data.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    payload = decode_token(data.token)
    if payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid reset token")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=400, detail="Invalid reset token")
    user = db.query(models.User).filter(models.User.id == int(sub)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = hash_password(data.new_password)
    db.commit()
    return {"message": "Password reset successful"}


@router.get("/me")
def me(user: models.User = Depends(get_current_user)):
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "student_id": user.student_id,
        "role": user.role,
    }
