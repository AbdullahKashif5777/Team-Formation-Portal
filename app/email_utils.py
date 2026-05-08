import smtplib
import logging
import html as html_module
from concurrent.futures import ThreadPoolExecutor
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Template
from app.config import settings

# Route mail logs through uvicorn's "error" channel so they appear in the
# server console (info, warning, and error). uvicorn always installs a handler
# for that logger name; using a plain ``__name__`` logger would be silent.
logger = logging.getLogger("uvicorn.error")

_EXECUTOR = ThreadPoolExecutor(max_workers=4)


def send_async(fn, *args, **kwargs) -> None:
    """
    Run email work off the request path with bounded concurrency.

    Callers keep the same email templates and arguments; only scheduling changes.
    """
    try:
        _EXECUTOR.submit(fn, *args, **kwargs)
    except Exception as e:
        logger.error("Failed to schedule email send: %s", e)


def _public_portal_url() -> str:
    return (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")


def _portal_block_html() -> str:
    base = _public_portal_url()
    if base:
        return (
            f'<p><strong>Portal:</strong> <a href="{base}/">{base}/</a></p>'
            '<p>Sign in with your registered email and password.</p>'
        )
    return "<p>Sign in using the Team Formation Portal URL shared by your instructor.</p>"


def _send(to_email: str, subject: str, html_body: str):
    if not settings.smtp_configured:
        logger.warning("Email not configured (set SMTP_USER and SMTP_PASSWORD) – skipping send to %s", to_email)
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.EMAIL_FROM
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(host=settings.SMTP_HOST, port=int(settings.SMTP_PORT)) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_USER, to_email, msg.as_string())
        logger.info("Email sent to %s", to_email)
    except Exception as e:
        logger.error("Failed to send email: %s", e)


_TMPL = """
<div style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #eee; padding: 20px;">
  <h2 style="color: #111827; border-bottom: 2px solid #eee; padding-bottom: 10px;">UMT Team Portal</h2>
  <div>{body}</div>
  <p style="margin-top: 30px; font-size: 12px; color: #777; border-top: 1px solid #eee; padding-top: 10px;">
    University of Management & Technology · Lahore
  </p>
</div>
"""

_MASTER_EMAIL_HTML = """
<div style="margin: 0; padding: 0;">
  <h1 style="margin: 0 0 14px 0; padding: 12px 14px; background: #111827; color: #ffffff; font-size: 18px; font-weight: 700; border-radius: 10px;">
    {{ event_type }}
  </h1>

  <table role="presentation" cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden; background: #ffffff;">
    <tr>
      <td style="padding: 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: collapse;">
          <tr>
            <td style="padding: 10px 12px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; width: 38%; font-weight: 700;">Student Name</td>
            <td style="padding: 10px 12px; background: #ffffff; border-bottom: 1px solid #e5e7eb;">{{ student_name }}</td>
          </tr>
          <tr>
            <td style="padding: 10px 12px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; width: 38%; font-weight: 700;">Student ID</td>
            <td style="padding: 10px 12px; background: #ffffff; border-bottom: 1px solid #e5e7eb;">{{ student_id }}</td>
          </tr>
          <tr>
            <td style="padding: 10px 12px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; width: 38%; font-weight: 700;">Course</td>
            <td style="padding: 10px 12px; background: #ffffff; border-bottom: 1px solid #e5e7eb;">{{ course_name }}</td>
          </tr>
          <tr>
            <td style="padding: 10px 12px; background: #f9fafb; width: 38%; font-weight: 700;">Section</td>
            <td style="padding: 10px 12px; background: #ffffff;">{{ section_name }}</td>
          </tr>
        </table>
      </td>
    </tr>
  </table>

  <div style="margin-top: 14px;">
    {{ body_html | safe }}
  </div>

  {% if portal_block %}
    <div style="margin-top: 14px;">
      {{ portal_block | safe }}
    </div>
  {% endif %}
</div>
"""

_REGISTRATION_BODY_HTML = """
<div>
  <p>Hello {{ student_name }},</p>
  <p>Your registration in the <strong>UMT Team Formation Portal</strong> is complete.</p>
  <p><strong>Account</strong><br>
    Email: {{ user_email }}<br>
    Role: {{ role }}
  </p>
  <p>If you did not register, contact your course administrator.</p>
</div>
"""


def _render_html(template_html: str, context: dict) -> str:
    """Render a Jinja2 HTML snippet with a safe, explicit context."""
    return Template(template_html).render(**context)


def _norm(v: str | None) -> str:
    return (v or "").strip() or "—"


def build_standard_template_data(
    *,
    student_name: str | None,
    student_id: str | None,
    course_name: str | None,
    section_name: str | None,
    event_type: str,
    **extra,
) -> dict:
    """
    Standardized context that every email can rely on.
    """
    data = {
        "student_name": _norm(student_name),
        "student_id": _norm(student_id),
        "course_name": _norm(course_name),
        "section_name": _norm(section_name),
        "event_type": _norm(event_type),
    }
    data.update(extra)
    return data


def build_subject(template_data: dict) -> str:
    return _render_html(
        "Update for {{ course_name }} ({{ section_name }}): {{ event_type }}",
        template_data,
    )


def render_master_email(*, template_data: dict, body_html: str) -> str:
    ctx = dict(template_data)
    ctx["body_html"] = body_html
    if "portal_block" not in ctx:
        ctx["portal_block"] = _portal_block_html()
    html_body = _render_html(_MASTER_EMAIL_HTML, ctx)
    return _TMPL.format(body=html_body)


def build_registration_email_context(
    *,
    student_name: str,
    student_id: str | None,
    team_lead_name: str | None,
    user_email: str,
    role: str,
    section_name: str | None = None,
    course_name: str | None = None,
) -> dict:
    return build_standard_template_data(
        student_name=student_name,
        student_id=student_id,
        course_name=course_name,
        section_name=section_name,
        event_type="New Registration",
        team_lead_name=_norm(team_lead_name) if team_lead_name else "Not assigned yet",
        user_email=_norm(user_email),
        role=_norm(role),
    )


def send_join_request_to_lead(
    lead_email: str,
    lead_name: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    course_name: str,
    section_name: str,
):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Join Request",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ lead_name }},</p>
          <p>A student has <strong>requested to join</strong> your team.</p>
          <p><strong>Team:</strong> {{ team_name }}</p>
          <p>Open the portal as <strong>Team Lead</strong> to accept or decline this request.</p>
        </div>
        """,
        template_data,
    )
    _send(lead_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_join_request_confirmation_to_member(
    member_email: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    lead_name: str,
    course_name: str,
    section_name: str,
):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Join Request Submitted",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p>Your request to join <strong>{{ team_name }}</strong> has been sent.</p>
          <p><strong>Team lead:</strong> {{ lead_name }}</p>
          <p>You will receive another email when the lead accepts or declines your request.</p>
        </div>
        """,
        template_data,
    )
    _send(member_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_accepted_to_member(
    member_email: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    lead_name: str,
    course_name: str,
    section_name: str,
):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Enrollment Confirmed",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p>Great news — you have been <strong>accepted</strong> into team <strong>{{ team_name }}</strong>.</p>
          <p><strong>Team lead:</strong> {{ lead_name }}</p>
          <p>Log in as a <strong>member</strong> to see your team.</p>
        </div>
        """,
        template_data,
    )
    _send(member_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_rejected_to_member(
    member_email: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    lead_name: str,
    course_name: str,
    section_name: str,
):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Join Request Declined",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p>Your request to join <strong>{{ team_name }}</strong> was <strong>not accepted</strong> by {{ lead_name }}.</p>
          <p>You can browse other teams and send a new request if formation is still open.</p>
        </div>
        """,
        template_data,
    )
    _send(member_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_removed_from_team_to_member(
    member_email: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    lead_name: str,
    course_name: str,
    section_name: str,
):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Member Removal",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p>You have been removed from team <strong>{{ team_name }}</strong> by lead <strong>{{ lead_name }}</strong>.</p>
          <p>If formation is still open, you may request to join another team.</p>
        </div>
        """,
        template_data,
    )
    _send(member_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_password_reset(email: str, name: str, reset_link: str):
    template_data = build_standard_template_data(
        student_name=name,
        student_id=None,
        course_name=None,
        section_name=None,
        event_type="Password Reset",
        reset_link=reset_link,
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p>We received a request to reset your password for the UMT Team Formation Portal.</p>
          <p><a href="{{ reset_link }}">Click here to set a new password</a></p>
          <p>If the button does not work, copy this link into your browser:<br><code>{{ reset_link }}</code></p>
          <p>This link expires in about <strong>60 minutes</strong>.</p>
          <p>If you did not request this, you can ignore this email.</p>
        </div>
        """,
        template_data,
    )
    _send(email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_course_created_notice(admin_email: str, course_name: str, course_code: str):
    body = f"""
    <p><strong>New Course Created:</strong></p>
    <p><strong>Name:</strong> {course_name}<br>
    <strong>Code:</strong> {course_code}</p>
    """
    _send(admin_email, f"Admin Notice: Course Created - {course_name}", _TMPL.format(body=body))


def send_section_created_notice(admin_email: str, section_name: str, course_name: str):
    body = f"""
    <p><strong>New Section Added:</strong></p>
    <p><strong>Section:</strong> {section_name}<br>
    <strong>Course:</strong> {course_name}</p>
    """
    _send(admin_email, f"Admin Notice: Section Added - {section_name}", _TMPL.format(body=body))


def send_registration_notice(
    to_email: str,
    user_name: str,
    user_email: str,
    role: str,
    student_id: str | None = None,
    team_lead_name: str | None = None,
    section_name: str | None = None,
    course_name: str | None = None,
):
    context = build_registration_email_context(
        student_name=user_name,
        student_id=student_id,
        team_lead_name=team_lead_name,
        user_email=user_email,
        role=role,
        section_name=section_name,
        course_name=course_name,
    )
    body_html = _render_html(_REGISTRATION_BODY_HTML, context)
    _send(to_email, build_subject(context), render_master_email(template_data=context, body_html=body_html))


def send_invite_declined_to_lead(lead_email: str, lead_name: str, member_name: str, team_name: str, course_name: str, section_name: str):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=None,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Invite Declined",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ lead_name }},</p>
          <p><strong>{{ student_name }}</strong> declined your invitation to join <strong>{{ team_name }}</strong>.</p>
        </div>
        """,
        template_data,
    )
    _send(lead_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_member_removed_by_lead_to_admin(admin_email: str, lead_name: str, member_name: str, team_name: str, course_name: str):
    body = f"""
    <p><strong>Manual Team Removal Alert:</strong></p>
    <p>Lead <strong>{lead_name}</strong> has removed <strong>{member_name}</strong> from team <strong>{team_name}</strong>.</p>
    <p><strong>Course:</strong> {course_name}</p>
    """
    _send(admin_email, f"Admin Notice: Member removed from {team_name}", _TMPL.format(body=body))


def send_course_section_removed_notice(admin_email: str, removed_type: str, name: str):
    body = f"""
    <p>The following {removed_type} has been deleted: <strong>{name}</strong></p>
    <p>All associated teams and memberships have been removed.</p>
    """
    _send(admin_email, f"{removed_type} Deleted: {name}", _TMPL.format(body=body))


def send_request_accepted_to_lead(
    lead_email: str,
    lead_name: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    course_name: str,
    section_name: str,
):
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Enrollment Confirmed",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ lead_name }},</p>
          <p><strong>{{ student_name }}</strong> has joined <strong>{{ team_name }}</strong>.</p>
          <p>This email includes the student’s ID for your records.</p>
        </div>
        """,
        template_data,
    )
    _send(lead_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_team_invite_to_member(
    member_email: str,
    member_name: str,
    member_id: str | None,
    team_name: str,
    lead_name: str,
    course_name: str,
    section_name: str,
    invite_message: str | None,
):
    msg = html_module.escape((invite_message or "").strip())
    template_data = build_standard_template_data(
        student_name=member_name,
        student_id=member_id,
        course_name=course_name,
        section_name=section_name,
        event_type="Team Invitation",
        lead_name=_norm(lead_name),
        team_name=_norm(team_name),
        invite_message=msg,
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p><strong>{{ lead_name }}</strong> invited you to join team <strong>{{ team_name }}</strong>.</p>
          {% if invite_message %}
            <p><strong>Message from your lead:</strong><br>{{ invite_message | safe }}</p>
          {% endif %}
          <p>Log in to the <strong>member</strong> portal to accept or decline the invite.</p>
        </div>
        """,
        template_data,
    )
    _send(member_email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_lead_assigned_notice(
    lead_email: str,
    lead_name: str,
    sections_summary: str,
    *,
    is_new_account: bool,
):
    """
    Email the lead when an admin assigns them as team lead for one or more sections.
    """
    acct = (
        "<p>An account was created for you. Use the <strong>password set by the administrator</strong> (or reset it from the login page).</p>"
        if is_new_account
        else "<p>Your existing account was updated with Team Lead access for the section(s) below.</p>"
    )
    body = f"""
    <p>Hello {lead_name},</p>
    <p>You have been assigned as a <strong>Team Lead</strong> in the UMT Team Formation Portal.</p>
    {acct}
    <p><strong>Your sections</strong><br>{sections_summary}</p>
    {_portal_block_html()}
    <p>After signing in, open the <strong>Team Lead</strong> portal to manage your team and join requests.</p>
    """
    _send(lead_email, "You are a Team Lead — UMT Team Portal", _TMPL.format(body=body))


def send_lead_welcome_email(
    email: str,
    lead_name: str,
    password: str,
    assignments: list[dict],
):
    """
    One-time welcome email for a newly created Team Lead account.
    NOTE: Do not log the password; it must only be used to render this email.
    """
    pairs = []
    for a in (assignments or []):
        try:
            course = _norm((a or {}).get("course"))
            section = _norm((a or {}).get("section"))
        except Exception:
            course, section = "—", "—"
        pairs.append({"course": course, "section": section})

    lead_id = (email.split("@", 1)[0] if email else "").strip().upper() or "—"

    # Use the first assignment for the standard subject fields, but include all in the body.
    first = pairs[0] if pairs else {"course": "—", "section": "—"}
    template_data = build_standard_template_data(
        student_name=lead_name,
        student_id=None,
        course_name=first.get("course"),
        section_name=first.get("section"),
        event_type="Team Lead Account Created",
        lead_name=_norm(lead_name),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ lead_name }},</p>
          <p>Your Admin has registered you as a <strong>Team Lead</strong>.</p>
          <p>
            Login with password: <code>{{ password }}</code><br>
            ID: <strong>{{ lead_id }}</strong>
          </p>
          <p><strong>Assignments</strong></p>
          <ul>
            {% for a in assignments %}
              <li>Course: <strong>{{ a.course }}</strong>, Section: <strong>{{ a.section }}</strong></li>
            {% endfor %}
          </ul>
          {{ portal_block | safe }}
        </div>
        """,
        {
            **template_data,
            "password": password,
            "lead_id": lead_id,
            "assignments": pairs,
        },
    )

    subject = (
        "Welcome — Team Lead account created (Multiple sections)"
        if len(pairs) > 1
        else "Welcome — Team Lead account created"
    )
    _send(email, subject, render_master_email(template_data=template_data, body_html=body_html))


def send_member_verification_email(
    email: str,
    name: str,
    verify_link: str,
    course: str,
    section: str,
):
    """
    Member-only: verification email for account creation.
    """
    template_data = build_standard_template_data(
        student_name=name,
        student_id=None,
        course_name=course,
        section_name=section,
        event_type="Email Verification",
        user_email=_norm(email),
    )
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ student_name }},</p>
          <p>Your Admin has registered you. Please verify your email to create your account.</p>
          <p>
            Course: <strong>{{ course_name }}</strong><br>
            Section: <strong>{{ section_name }}</strong>
          </p>
          <p>
            <a href="{{ verify_link }}" style="display:inline-block;padding:10px 14px;border-radius:10px;background:#111827;color:#ffffff;text-decoration:none;font-weight:700;">
              Verify Email
            </a>
          </p>
          <p>If the button does not work, copy this link into your browser:<br><code>{{ verify_link }}</code></p>
          {{ portal_block | safe }}
        </div>
        """,
        {**template_data, "verify_link": verify_link},
    )
    _send(email, build_subject(template_data), render_master_email(template_data=template_data, body_html=body_html))


def send_admin_new_lead_notice(admin_email: str, lead_name: str, lead_user_email: str, sections_summary: str):
    body = f"""
    <p><strong>Team lead assigned</strong></p>
    <p>Name: {lead_name}<br>
    Email: {lead_user_email}<br>
    Sections: {sections_summary}</p>
    {_portal_block_html()}
    """
    _send(admin_email, f"Admin notice: lead {lead_name}", _TMPL.format(body=body))


def send_lead_removed_notice(
    lead_email: str,
    lead_name: str,
    lead_id: str | None = None,
    assignments: list[dict] | None = None,
):
    template_data = build_standard_template_data(
        student_name=lead_name,
        student_id=_norm(lead_id) if lead_id else None,
        course_name=(assignments[0].get("course") if assignments else None),
        section_name=(assignments[0].get("section") if assignments else None),
        event_type="Team Lead Assignment Removed",
        lead_name=_norm(lead_name),
    )
    pairs = []
    for a in (assignments or []):
        try:
            course = _norm((a or {}).get("course"))
            section = _norm((a or {}).get("section"))
        except Exception:
            course, section = "—", "—"
        pairs.append({"course": course, "section": section})
    body_html = _render_html(
        """
        <div>
          <p>Hello {{ lead_name }},</p>
          <p>Your <strong>Team Lead</strong> assignment has been removed by the administrator.</p>
          {% if pairs %}
            <p><strong>Removed assignments</strong></p>
            <ul>
              {% for a in pairs %}
                <li>Course: <strong>{{ a.course }}</strong>, Section: <strong>{{ a.section }}</strong></li>
              {% endfor %}
            </ul>
          {% endif %}
          <p>Associated teams have been disbanded.</p>
          {{ portal_block | safe }}
        </div>
        """,
        {**template_data, "pairs": pairs},
    )
    _send(
        lead_email,
        "Account Update: Team Lead assignment removed",
        render_master_email(template_data=template_data, body_html=body_html),
    )


def send_member_team_removed_notice(member_email: str, member_name: str, team_name: str):
    body = f"""
    <p>Hello {member_name},</p>
    <p>Your team <strong>{team_name}</strong> has been disbanded by the administrator. You can now join another team.</p>
    {_portal_block_html()}
    """
    _send(member_email, f"Notice: Team {team_name} disbanded", _TMPL.format(body=body))
