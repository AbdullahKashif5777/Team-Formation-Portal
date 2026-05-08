from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Float, JSON
from sqlalchemy.orm import relationship

from app.database import Base

try:
    # Postgres-optimized JSON type.
    from sqlalchemy.dialects.postgresql import JSONB as _JSONType  # type: ignore
except Exception:
    _JSONType = JSON

class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, unique=True)
    sections = relationship("Section", back_populates="course", cascade="all, delete-orphan")

class Section(Base):
    __tablename__ = "sections"
    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(50), nullable=False)  # e.g., "W-7"
    google_sheet_url = Column(String(500), nullable=True)
    team_size_limit = Column(Integer, default=4, nullable=False)  # total people per team (lead + accepted members)
    column_config = Column(_JSONType, nullable=True)
    formation_start = Column(DateTime, nullable=True)
    formation_end = Column(DateTime, nullable=True)
    
    course = relationship("Course", back_populates="sections")
    user_sections = relationship("UserSection", back_populates="section", cascade="all, delete-orphan")
    teams = relationship("Team", back_populates="section", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("course_id", "name", name="uix_course_section"),)

class UserSection(Base):
    __tablename__ = "user_sections"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    section_id = Column(Integer, ForeignKey("sections.id", ondelete="CASCADE"), nullable=False, index=True)
    # Section-scoped role (a user can be lead in one section, member in another).
    role = Column(String(10), nullable=False, default="member")
    
    user = relationship("User", back_populates="user_sections")
    section = relationship("Section", back_populates="user_sections")

    __table_args__ = (UniqueConstraint("user_id", "section_id", name="uix_user_section"),)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    student_id = Column(String(20), unique=True, index=True, nullable=True)  # None for leads
    password_hash = Column(String(255), nullable=False)
    role = Column(String(10), default="member")  # "lead" or "member"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    led_teams = relationship("Team", back_populates="lead", cascade="all, delete-orphan")
    memberships = relationship(
        "TeamMembership", back_populates="member", foreign_keys="TeamMembership.member_id", cascade="all, delete-orphan"
    )
    user_sections = relationship("UserSection", back_populates="user", cascade="all, delete-orphan")


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    lead_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    section_id = Column(Integer, ForeignKey("sections.id", ondelete="CASCADE"), index=True, nullable=False)
    max_members = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("User", back_populates="led_teams")
    section = relationship("Section", back_populates="teams")
    memberships = relationship(
        "TeamMembership", back_populates="team", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("lead_id", "section_id", name="uix_team_lead_section"),)

    @property
    def accepted_count(self):
        return sum(1 for m in self.memberships if m.status == "accepted")


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=False)
    member_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    status = Column(String(20), default="pending", index=True)  # pending, accepted, rejected
    message = Column(Text, nullable=True)
    extra_data = Column(_JSONType, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="memberships")
    member = relationship("User", back_populates="memberships", foreign_keys=[member_id])

    __table_args__ = (UniqueConstraint("member_id", "team_id", name="uix_member_team"),)
