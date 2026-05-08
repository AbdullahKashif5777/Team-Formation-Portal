from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(tags=["roster-sheet"])


@router.get("/api/roster/sheet/{section_id}")
def get_roster_sheet_v2(
    section_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Spreadsheet payload shaped to match the legacy roster table formatting:
    includes Role/Email plus dynamic extra_data keyed by Section.column_config.

    Additive endpoint: does not modify existing roster endpoints.
    """
    section = db.query(models.Section).filter(models.Section.id == section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    # Determine section-context access (do not rely on global user.role).
    # - admin: all section teams
    # - lead: only teams they lead in this section
    # - member: only the team they are accepted into in this section
    visible_team_ids: set[int] | None = None

    if user.role == "admin":
        visible_team_ids = None
    else:
        lead_team_ids = {
            t.id
            for t in (
                db.query(models.Team)
                .filter(models.Team.lead_id == user.id, models.Team.section_id == section_id)
                .all()
            )
        }
        if lead_team_ids:
            visible_team_ids = lead_team_ids
        else:
            membership = (
                db.query(models.TeamMembership)
                .join(models.Team, models.Team.id == models.TeamMembership.team_id)
                .filter(
                    models.TeamMembership.member_id == user.id,
                    models.TeamMembership.status == "accepted",
                    models.Team.section_id == section_id,
                )
                .first()
            )
            if not membership or not membership.team_id:
                raise HTTPException(status_code=403, detail="Not authorized for this section")
            visible_team_ids = {int(membership.team_id)}

    # visible_team_ids now encodes the section-context authorization scope:
    # - None => admin (all teams)
    # - set([...]) => limited teams for lead/member context

    # Preserve section.teams iteration order (matches legacy roster API).
    teams_payload: list[dict] = []
    rows: list[dict] = []

    team_size_limit = int(getattr(section, "team_size_limit", 4) or 4)
    member_slots = max(0, team_size_limit - 1)  # excludes lead

    all_teams = list(section.teams or [])
    team_index = {t.id: i for i, t in enumerate(all_teams, 1)}

    for team in all_teams:
        if visible_team_ids is not None and team.id not in visible_team_ids:
            continue

        i = team_index.get(team.id, 0) or 0
        lead = team.lead
        lead_name = lead.name if lead else ""
        display_team_name = f"Team {i} ({lead_name})" if i else f"Team ({lead_name})"

        accepted_memberships = [m for m in (team.memberships or []) if m.status == "accepted"]
        accepted_count = len(accepted_memberships)

        teams_payload.append(
            {
                "team_id": team.id,
                "team_name": display_team_name,
                "lead_name": lead_name,
                "accepted_count": accepted_count,
                "member_slots": member_slots,
            }
        )

        # Lead row (non-patchable; kept for exact visual parity)
        if lead:
            rows.append(
                {
                    "membership_id": None,
                    "role": "Lead",
                    "name": lead.name,
                    "student_id": lead.student_id,
                    "email": lead.email,
                    "team_name": display_team_name,
                    "extra_data": {},
                    "team_id": team.id,
                }
            )

        # Member rows (patchable)
        for membership in accepted_memberships:
            member = membership.member
            rows.append(
                {
                    "membership_id": membership.id,
                    "role": "Member",
                    "name": member.name if member else "",
                    "student_id": member.student_id if member else None,
                    "email": member.email if member else "",
                    "team_name": display_team_name,
                    "extra_data": getattr(membership, "extra_data", None) or {},
                    "team_id": team.id,
                }
            )

    return {
        "section_name": section.name,
        "course_name": section.course.name if section.course else "",
        "team_size_limit": team_size_limit,
        "member_slots": member_slots,
        "column_config": getattr(section, "column_config", None) or [],
        "teams": teams_payload,
        "roster": rows,
    }

