from __future__ import annotations

import csv
import os
import secrets
import smtplib
import ssl
from io import StringIO
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sepaxml import SepaTransfer
from sqlmodel import Session, select

from .database import (
    ExtracurricularActivity,
    FamilyProfile,
    Student,
    StudentAttendanceNotice,
    TrustedContact,
    User,
    create_db_and_tables,
    engine,
    get_session,
)
from .pricing import DEFAULT_EXTRA_COSTS, calculate_student_total
from .schemas import FamilyForm
from .security import (
    ExpiredSignatureError,
    InvalidTokenError,
    clear_auth_cookie,
    create_access_token,
    decode_access_token,
    hash_password,
    set_auth_cookie,
    verify_password,
)

templates = Jinja2Templates(directory="templates")
APP_VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
app = FastAPI(title="Comedor Escolar ANPA", version=APP_VERSION)

WEEKDAY_SEQUENCE = [("L", "Lunes"), ("M", "Martes"), ("X", "Miércoles"), ("J", "Jueves"), ("V", "Viernes")]
SCHOOL_MONTH_SEQUENCE = [
    (9, "Septiembre"),
    (10, "Octubre"),
    (11, "Noviembre"),
    (12, "Diciembre"),
    (1, "Enero"),
    (2, "Febrero"),
    (3, "Marzo"),
    (4, "Abril"),
    (5, "Mayo"),
    (6, "Junio"),
    (7, "Julio"),
    (8, "Agosto"),
]
EXTRA_DAY_MAP = {
    "lunes": "L",
    "martes": "M",
    "miercoles": "X",
    "miércoles": "X",
    "jueves": "J",
    "viernes": "V",
}
WEEKDAY_INDEX_TO_LETTER = {0: "L", 1: "M", 2: "X", 3: "J", 4: "V"}
COMEDOR_NOTICE_CUTOFF_TIME = time(9, 30)

FOOD_INTOLERANCE_OPTIONS = [
    "Gluten",
    "Lactosa",
    "Frutos secos",
    "Huevo",
    "Pescado",
    "Marisco",
    "Soja",
    "Sésamo",
]


def _get_family_profile(session: Session, user_id: int) -> FamilyProfile | None:
    return session.exec(select(FamilyProfile).where(FamilyProfile.user_id == user_id)).first()


def _get_owned_student(session: Session, family_id: int, student_id: int) -> Student | None:
    return session.exec(select(Student).where(Student.id == student_id).where(Student.family_id == family_id)).first()


def _get_owned_contact(session: Session, family_id: int, contact_id: int) -> TrustedContact | None:
    return session.exec(
        select(TrustedContact).where(TrustedContact.id == contact_id).where(TrustedContact.family_id == family_id)
    ).first()


def _get_active_extracurricular_activities(session: Session) -> list[ExtracurricularActivity]:
    statement = (
        select(ExtracurricularActivity)
        .where(ExtracurricularActivity.is_active)
        .order_by(ExtracurricularActivity.name.asc())
    )
    return session.exec(statement).all()


def _get_extra_price_map(session: Session) -> dict[str, float]:
    activities = _get_active_extracurricular_activities(session)
    return {activity.name: activity.monthly_price for activity in activities}


def _build_student_total_fn(extra_price_map: dict[str, float]):
    def _student_total(student: Student) -> float:
        return calculate_student_total(student, extra_costs=extra_price_map)

    return _student_total


def _normalize_extras(extras_selected: list[str] | None, extras_fallback: str) -> str:
    if extras_selected:
        clean = [item.strip() for item in extras_selected if item and item.strip()]
        return ",".join(clean)
    return extras_fallback.strip()


def _normalize_list_items(raw_value: str) -> list[str]:
    clean: list[str] = []
    for item in raw_value.split(","):
        normalized = item.strip()
        if not normalized:
            continue
        if normalized not in clean:
            clean.append(normalized)
    return clean


COUNTRY_PHONE_PREFIXES = [
    "+34",
    "+1",
    "+44",
    "+33",
    "+49",
    "+351",
    "+52",
    "+54",
    "+61",
    "+65",
    "+81",
    "+971",
    "+966",
]


def _split_phone(value: str) -> tuple[str, str]:
    clean_value = (value or "").strip()
    if not clean_value:
        return "+34", ""
    for prefix in sorted(COUNTRY_PHONE_PREFIXES, key=len, reverse=True):
        if clean_value.startswith(prefix):
            number = clean_value[len(prefix):].strip()
            return prefix, number
    if clean_value.startswith("+"):
        prefix = clean_value[:4] if len(clean_value) > 4 else clean_value
        number = clean_value[len(prefix):].strip()
        return prefix, number
    return "+34", clean_value.replace(" ", "")


templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["_split_phone"] = _split_phone


def _validate_allergy_items(items: list[str]) -> str | None:
    for item in items:
        if not item.lower().startswith("otro:"):
            continue
        custom_value = item.split(":", 1)[1].strip() if ":" in item else ""
        if not custom_value:
            return "La opción 'Otro' debe incluir una descripción."
        if "," in custom_value:
            return "La descripción personalizada no puede contener comas."
        if len(custom_value) > 100:
            return "La descripción personalizada de intolerancia no puede superar 100 caracteres."
    return None


def _selected_days(value: str) -> list[str]:
    if not value:
        return []
    return [day for day in ["L", "M", "X", "J", "V"] if day in value.upper().replace(" ", "")]


def _weekday_letter_for_date(target_date: date) -> str | None:
    return WEEKDAY_INDEX_TO_LETTER.get(target_date.weekday())


def _is_comedor_day_contracted(student: Student, target_date: date) -> bool:
    target_letter = _weekday_letter_for_date(target_date)
    if not target_letter:
        return False
    return target_letter in set(_selected_days(student.comedor_days))


def _is_comedor_notice_available_for_day(day_date: date, skip_comedor_already_marked: bool) -> bool:
    if skip_comedor_already_marked:
        return True
    today = date.today()
    if day_date < today:
        return False
    if day_date > today:
        return True
    return datetime.now().time() < COMEDOR_NOTICE_CUTOFF_TIME


def _validate_password_change(password: str, password_confirm: str, current_hashed_password: str) -> str | None:
    if len(password) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if password != password_confirm:
        return "Las contraseñas no coinciden."
    if verify_password(password, current_hashed_password):
        return "La nueva contraseña debe ser distinta a la contraseña provisional asignada por el administrador."
    return None


def _parse_extra_day_letter(schedule: str) -> str | None:
    if not schedule:
        return None
    day_token = schedule.strip().split(" ", 1)[0].lower()
    return EXTRA_DAY_MAP.get(day_token)


def _build_week_days(today: date | None = None) -> list[dict]:
    base_day = today or date.today()
    week_start = base_day - timedelta(days=base_day.weekday())
    week_days: list[dict] = []
    for offset, (letter, label) in enumerate(WEEKDAY_SEQUENCE):
        day_date = week_start + timedelta(days=offset)
        week_days.append(
            {
                "letter": letter,
                "label": label,
                "date": day_date,
                "date_iso": day_date.isoformat(),
                "short_label": f"{label[:3]} {day_date.day:02d}",
            }
        )
    return week_days


def _build_weekly_student_services(
    students: list[Student],
    activities: list[ExtracurricularActivity],
    notices: list[StudentAttendanceNotice],
    week_days: list[dict],
) -> dict[int, dict[str, dict]]:
    activity_day_by_name = {activity.name: _parse_extra_day_letter(activity.schedule) for activity in activities}
    notice_map = {(notice.student_id, notice.notice_date.isoformat()): notice for notice in notices}

    weekly_services: dict[int, dict[str, dict]] = {}
    for student in students:
        comedor_days = set(_selected_days(student.comedor_days))
        matinal_days = set(_selected_days(student.aula_matinal_days))
        selected_extras = _normalize_list_items(student.extras)
        student_days: dict[str, dict] = {}

        for day in week_days:
            day_letter = day["letter"]
            day_iso = day["date_iso"]
            day_date = day["date"]
            extras_for_day = [name for name in selected_extras if activity_day_by_name.get(name) == day_letter]
            notice = notice_map.get((student.id, day_iso))
            skip_comedor = bool(notice and notice.skip_comedor)
            comedor_contracted = day_letter in comedor_days

            student_days[day_iso] = {
                "comedor_contracted": comedor_contracted,
                "aula_matinal_contracted": day_letter in matinal_days,
                "extras": extras_for_day,
                "skip_comedor": skip_comedor,
                "skip_aula_matinal": bool(notice and notice.skip_aula_matinal),
                "comedor_notice_available": comedor_contracted
                and _is_comedor_notice_available_for_day(day_date, skip_comedor),
            }

        weekly_services[student.id] = student_days

    return weekly_services


def _build_admin_user_display_name(role: str, email: str, family_name: str | None) -> str:
    if role == "familia" and family_name:
        return family_name
    local_part = email.split("@", 1)[0].strip()
    return local_part or email


def _generate_provisional_password(length: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%&*?"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _is_valid_user_email(email: str) -> bool:
    if "@" not in email:
        return False
    local, domain = email.split("@", 1)
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    return True


def _extract_emails_from_csv_content(csv_text: str) -> list[str]:
    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:5]).lower()
    has_email_header = "email" in sample or "correo" in sample
    emails: list[str] = []
    if has_email_header:
        reader = csv.DictReader(lines)
        for row in reader:
            if not row:
                continue
            raw_email = str(row.get("email") or row.get("Email") or row.get("correo") or row.get("Correo") or "").strip()
            if raw_email:
                emails.append(raw_email.lower())
        return emails

    reader = csv.reader(lines)
    for row in reader:
        if not row:
            continue
        raw_email = str(row[0]).strip()
        if raw_email:
            emails.append(raw_email.lower())
    return emails


def _send_provisional_password_email(recipient_email: str, provisional_password: str, role: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from_email = os.getenv("SMTP_FROM_EMAIL", "").strip() or smtp_username
    smtp_from_name = os.getenv("SMTP_FROM_NAME", "ANPA Comedor").strip() or "ANPA Comedor"
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
    smtp_use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes"}
    app_public_url = os.getenv("APP_PUBLIC_URL", "http://localhost:8000").strip() or "http://localhost:8000"

    if not smtp_host:
        raise ValueError("SMTP_HOST no configurado.")
    if not smtp_from_email:
        raise ValueError("SMTP_FROM_EMAIL o SMTP_USERNAME debe estar configurado.")
    if smtp_username and not smtp_password:
        raise ValueError("SMTP_PASSWORD no configurado.")

    message = EmailMessage()
    message["Subject"] = "Acceso ANPA: contraseña provisional"
    message["From"] = f"{smtp_from_name} <{smtp_from_email}>"
    message["To"] = recipient_email
    message.set_content(
        "\n".join(
            [
                "Hola,",
                "",
                f"Se ha creado tu usuario de tipo '{role}' en la plataforma ANPA.",
                f"Email: {recipient_email}",
                f"Contraseña provisional: {provisional_password}",
                "",
                f"Accede en: {app_public_url}/login",
                "Por seguridad, deberás cambiar la contraseña en el primer inicio de sesión.",
                "",
                "Este es un mensaje automático, por favor no respondas a este correo.",
            ]
        )
    )

    if smtp_use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as smtp:
            if smtp_username:
                smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        if smtp_use_tls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if smtp_username:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def _build_student_monthly_breakdown(student: Student, extra_price_map: dict[str, float]) -> dict[str, float | int]:
    comedor_days_count = len(_selected_days(student.comedor_days))
    comedor_multiplier = 1.35 if student.menu_type == "celiaco" else 1.00
    comedor_cost = (comedor_days_count * 6.5 * comedor_multiplier) + (comedor_days_count * 0.75 if student.allergies else 0.0)

    matinal_days_count = len(_selected_days(student.aula_matinal_days))
    matinal_cost = matinal_days_count * 4.5
    if student.aula_matinal_breakfast:
        matinal_cost += matinal_days_count * 1.2

    extras_selected = _normalize_list_items(student.extras)
    activities_count = len(extras_selected)
    activities_cost = sum(extra_price_map.get(activity, 0.0) for activity in extras_selected)

    return {
        "matinal_days": matinal_days_count,
        "matinal_cost": round(matinal_cost, 2),
        "comedor_days": comedor_days_count,
        "comedor_cost": round(comedor_cost, 2),
        "activities_count": activities_count,
        "activities_cost": round(activities_cost, 2),
        "total_cost": round(matinal_cost + comedor_cost + activities_cost, 2),
    }


def _build_school_payment_calendar(
    students: list[Student], extra_price_map: dict[str, float], today: date | None = None
) -> dict[str, str | list[dict]]:
    today_date = today or date.today()
    school_start_year = today_date.year if today_date.month >= 9 else today_date.year - 1
    current_month_date = date(today_date.year, today_date.month, 1)
    months: list[dict] = []

    for month_number, month_name in SCHOOL_MONTH_SEQUENCE:
        month_year = school_start_year if month_number >= 9 else school_start_year + 1
        month_date = date(month_year, month_number, 1)
        is_no_school = month_number in (7, 8)
        is_past = month_date < current_month_date
        is_overdue_30_days = (today_date - month_date).days > 30 if is_past and not is_no_school else False

        student_summaries: list[dict] = []
        month_total = 0.0
        if not is_no_school:
            for student in students:
                summary = _build_student_monthly_breakdown(student, extra_price_map)
                month_total += float(summary["total_cost"])
                student_summaries.append({"student": student, "summary": summary})

        if is_no_school:
            status = "no_school"
            status_label = "Sin actividad"
        elif is_past:
            status = "pending"
            status_label = "Pendiente"
        else:
            status = "estimated"
            status_label = "Estimado"

        months.append(
            {
                "month_name": month_name,
                "year": month_year,
                "month_date": month_date,
                "status": status,
                "status_label": status_label,
                "is_no_school": is_no_school,
                "is_overdue_30_days": is_overdue_30_days,
                "student_summaries": student_summaries,
                "month_total": round(month_total, 2),
            }
        )

    return {
        "title": f"Calendario escolar {school_start_year}/{school_start_year + 1}",
        "months": months,
    }


def _build_family_dashboard_context(
    session: Session,
    user: User,
    profile_override: dict | FamilyProfile | None = None,
    active_section: str = "home",
    profile_edit_mode: bool = False,
    editing_student: Student | dict | None = None,
    editing_contact: TrustedContact | dict | None = None,
) -> dict:
    profile_db = _get_family_profile(session, user.id)
    activities = _get_active_extracurricular_activities(session)
    extra_price_map = {activity.name: activity.monthly_price for activity in activities}
    student_total_fn = _build_student_total_fn(extra_price_map)

    students: list[Student] = []
    contacts: list[TrustedContact] = []
    total = 0.0
    if profile_db:
        students = session.exec(select(Student).where(Student.family_id == profile_db.id)).all()
        contacts = session.exec(
            select(TrustedContact)
            .where(TrustedContact.family_id == profile_db.id)
            .order_by(TrustedContact.last_name.asc(), TrustedContact.first_name.asc())
        ).all()
        total = round(sum(student_total_fn(student) for student in students), 2)

    week_days = _build_week_days()
    week_start = week_days[0]["date"]
    week_end = week_days[-1]["date"]
    student_ids = [student.id for student in students if student.id is not None]
    notices: list[StudentAttendanceNotice] = []
    if student_ids:
        notice_statement = (
            select(StudentAttendanceNotice)
            .where(StudentAttendanceNotice.notice_date >= week_start)
            .where(StudentAttendanceNotice.notice_date <= week_end)
        )
        notices = [notice for notice in session.exec(notice_statement).all() if notice.student_id in student_ids]
    weekly_student_services = _build_weekly_student_services(students, activities, notices, week_days)
    school_payment_calendar = _build_school_payment_calendar(students, extra_price_map)

    monthly_skip_comedor_days = 0
    total_skip_comedor_days = 0
    payments_status_label = "al día"

    if student_ids:
        today = date.today()
        month_start = date(today.year, today.month, 1)
        next_month_start = date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)
        student_map = {student.id: student for student in students if student.id is not None}
        attendance_notices = session.exec(
            select(StudentAttendanceNotice).where(StudentAttendanceNotice.student_id.in_(student_ids))
        ).all()

        valid_skip_comedor_notices = []
        for notice in attendance_notices:
            student = student_map.get(notice.student_id)
            if not student or not notice.skip_comedor:
                continue
            if notice.notice_date >= today:
                continue
            if not _is_comedor_day_contracted(student, notice.notice_date):
                continue
            valid_skip_comedor_notices.append(notice)

        monthly_skip_comedor_days = sum(
            1 for notice in valid_skip_comedor_notices if month_start <= notice.notice_date < next_month_start
        )
        total_skip_comedor_days = len(valid_skip_comedor_notices)

    if any(month.get("is_overdue_30_days") for month in school_payment_calendar["months"]):
        payments_status_label = "pendiente"

    profile_obj = profile_override if profile_override is not None else profile_db
    raw_phone = ""
    if isinstance(profile_obj, dict):
        raw_phone = str(profile_obj.get("phone") or "")
    elif profile_obj is not None:
        raw_phone = str(profile_obj.phone or "")
    phone_prefix, phone_number = _split_phone(raw_phone)

    return {
        "user": user,
        "profile": profile_obj,
        "profile_saved": profile_db is not None,
        "profile_edit_mode": profile_edit_mode,
        "active_section": active_section,
        "editing_student": editing_student,
        "students": students,
        "contacts": contacts,
        "editing_contact": editing_contact,
        "monthly_total": total,
        "student_total": student_total_fn,
        "extracurricular_activities": activities,
        "food_intolerance_options": FOOD_INTOLERANCE_OPTIONS,
        "week_days": week_days,
        "weekly_student_services": weekly_student_services,
        "school_payment_calendar_title": school_payment_calendar["title"],
        "school_payment_calendar_months": school_payment_calendar["months"],
        "payments_status_label": payments_status_label,
        "monthly_skip_comedor_days": monthly_skip_comedor_days,
        "total_skip_comedor_days": total_skip_comedor_days,
        "phone_prefix": phone_prefix,
        "phone_number": phone_number,
    }


async def require_user(request: Request, session: Session) -> User:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")
    try:
        claims = decode_access_token(token)
    except (ExpiredSignatureError, InvalidTokenError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido") from exc

    user = session.exec(select(User).where(User.email == claims["sub"]).where(User.is_active)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no válido")
    return user


def family_total(family_id: int, session: Session, extra_price_map: dict[str, float] | None = None) -> float:
    students = session.exec(select(Student).where(Student.family_id == family_id)).all()
    return round(sum(calculate_student_total(student, extra_costs=extra_price_map) for student in students), 2)


def _seed_default_extracurricular_activities(session: Session) -> None:
    if session.exec(select(ExtracurricularActivity)).first():
        return
    for name, price in DEFAULT_EXTRA_COSTS.items():
        session.add(
            ExtracurricularActivity(
                name=name,
                monthly_price=price,
                duration_hours=1.0,
                material_required=False,
                schedule="Lunes 16:00",
                is_active=True,
            )
        )
    session.commit()


def _requires_initial_admin_setup(session: Session) -> bool:
    return session.exec(select(User.id).limit(1)).first() is None


@app.on_event("startup")
def startup_event() -> None:
    create_db_and_tables()
    with Session(engine) as session:
        _seed_default_extracurricular_activities(session)


@app.get("/", response_class=HTMLResponse)
async def root(_: Request):
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, session: Session = Depends(get_session)):
    setup_required = _requires_initial_admin_setup(session)
    return templates.TemplateResponse(request, "login.html", {"setup_required": setup_required})


@app.post("/login")
async def login(
    request: Request,
    session: Session = Depends(get_session),
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    normalized_email = email.lower().strip()
    if _requires_initial_admin_setup(session):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Debes crear el administrador inicial antes de iniciar sesión.",
                "setup_required": True,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    user = session.exec(select(User).where(User.email == normalized_email)).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Credenciales incorrectas. Comprueba email y contraseña.", "email": normalized_email},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.is_active:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "La cuenta está desactivada.", "email": normalized_email},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_access_token(user.email, user.role)
    if user.must_change_password:
        redirect = RedirectResponse(url="/change-password", status_code=status.HTTP_302_FOUND)
        set_auth_cookie(redirect, token)
        return redirect

    target = "/admin" if user.role == "admin" else "/user_family"
    redirect = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    set_auth_cookie(redirect, token)
    return redirect


@app.post("/logout")
async def logout():
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    clear_auth_cookie(redirect)
    return redirect


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, session: Session = Depends(get_session)):
    user = await require_user(request, session)
    if not user.must_change_password:
        target = "/admin" if user.role == "admin" else "/user_family"
        return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"change_password_required": True, "user_email": user.email},
    )


@app.post("/change-password")
async def change_password_submit(
    request: Request,
    session: Session = Depends(get_session),
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    user = await require_user(request, session)
    validation_error = _validate_password_change(password, password_confirm, user.hashed_password)
    if validation_error:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"change_password_required": True, "user_email": user.email, "error": validation_error},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user.hashed_password = hash_password(password)
    user.must_change_password = False
    session.add(user)
    session.commit()

    target = "/admin" if user.role == "admin" else "/user_family"
    redirect = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    return redirect


@app.get("/user_family", response_class=HTMLResponse)
async def user_family_page(
    request: Request,
    session: Session = Depends(get_session),
    section: str = "home",
    edit_profile: bool = False,
    edit_student_id: int | None = None,
    edit_contact_id: int | None = None,
):
    try:
        user = await require_user(request, session)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role != "familia":
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    allowed_sections = {"home", "perfil", "estudiantes", "contactos", "pagos"}
    active_section = section if section in allowed_sections else "home"
    attendance_status = request.query_params.get("attendance_status", "")

    editing_student: Student | None = None
    editing_contact: TrustedContact | None = None
    if active_section == "estudiantes" and edit_student_id is not None:
        profile = _get_family_profile(session, user.id)
        if profile:
            editing_student = _get_owned_student(session, profile.id, edit_student_id)

    if active_section == "contactos" and edit_contact_id is not None:
        profile = _get_family_profile(session, user.id)
        if profile:
            editing_contact = _get_owned_contact(session, profile.id, edit_contact_id)

    context = _build_family_dashboard_context(
        session,
        user,
        active_section=active_section,
        profile_edit_mode=edit_profile,
        editing_student=editing_student,
        editing_contact=editing_contact,
    )
    context["attendance_status"] = attendance_status
    return templates.TemplateResponse(request, "user_family.html", context)


@app.get("/dashboard")
async def legacy_dashboard_redirect():
    return RedirectResponse(url="/user_family", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/profile")
async def save_family_profile(
    request: Request,
    session: Session = Depends(get_session),
    parent_name: Annotated[str, Form()] = "",
    dni_nie: Annotated[str, Form()] = "",
    phone_prefix: Annotated[str, Form()] = "+34",
    phone_number: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    iban: Annotated[str, Form()] = "",
    sepa_accepted: Annotated[bool, Form()] = False,
):
    user = await require_user(request, session)

    full_phone = f"{phone_prefix}{phone_number.strip()}" if phone_number.strip() else phone_prefix
    form_values = {
        "parent_name": parent_name,
        "dni_nie": dni_nie,
        "phone": full_phone,
        "email": email,
        "iban": iban,
        "sepa_accepted": sepa_accepted,
    }

    try:
        form = FamilyForm(**form_values)
    except ValidationError as exc:
        context = _build_family_dashboard_context(
            session,
            user,
            profile_override=form_values,
            active_section="perfil",
            profile_edit_mode=True,
        )
        context.update(
            {
                "error": "Revisa los datos del formulario.",
                "error_detail": "; ".join(err.get("msg", "Campo inválido") for err in exc.errors()),
                "form_errors": {item["loc"][0]: True for item in exc.errors() if item.get("loc")},
            }
        )
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    profile = _get_family_profile(session, user.id)
    if profile is None:
        profile = FamilyProfile(user_id=user.id, **form.model_dump())
        session.add(profile)
    else:
        profile.parent_name = form.parent_name
        profile.dni_nie = form.dni_nie
        profile.phone = form.phone
        profile.email = str(form.email)
        profile.iban = form.iban
        profile.sepa_accepted = form.sepa_accepted

    session.commit()
    return RedirectResponse(url="/user_family?section=perfil", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/attendance")
async def save_attendance_notice(
    request: Request,
    session: Session = Depends(get_session),
    student_id: Annotated[int, Form()] = 0,
    notice_date: Annotated[str, Form()] = "",
    skip_comedor: Annotated[bool, Form()] = False,
    skip_aula_matinal: Annotated[bool, Form()] = False,
):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    student = _get_owned_student(session, profile.id, student_id)
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alumno no encontrado.")

    try:
        notice_day = date.fromisoformat(notice_date)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fecha de aviso no válida.") from exc

    existing_notice = session.exec(
        select(StudentAttendanceNotice)
        .where(StudentAttendanceNotice.student_id == student.id)
        .where(StudentAttendanceNotice.notice_date == notice_day)
    ).first()

    if skip_comedor:
        already_marked_comedor = bool(existing_notice and existing_notice.skip_comedor)
        today = date.today()
        if not _is_comedor_day_contracted(student, notice_day):
            return RedirectResponse(
                url="/user_family?section=home&attendance_status=comedor_not_available",
                status_code=status.HTTP_302_FOUND,
            )
        if notice_day < today and not already_marked_comedor:
            return RedirectResponse(
                url="/user_family?section=home&attendance_status=comedor_not_available",
                status_code=status.HTTP_302_FOUND,
            )
        if notice_day == today and datetime.now().time() >= COMEDOR_NOTICE_CUTOFF_TIME and not already_marked_comedor:
            return RedirectResponse(
                url="/user_family?section=home&attendance_status=comedor_not_available",
                status_code=status.HTTP_302_FOUND,
            )

    if not skip_comedor and not skip_aula_matinal:
        if existing_notice:
            session.delete(existing_notice)
            session.commit()
        return RedirectResponse(url="/user_family?section=home&attendance_status=cleared", status_code=status.HTTP_302_FOUND)

    if existing_notice:
        existing_notice.skip_comedor = skip_comedor
        existing_notice.skip_aula_matinal = skip_aula_matinal
        session.add(existing_notice)
    else:
        session.add(
            StudentAttendanceNotice(
                student_id=student.id,
                notice_date=notice_day,
                skip_comedor=skip_comedor,
                skip_aula_matinal=skip_aula_matinal,
            )
        )

    session.commit()
    return RedirectResponse(url="/user_family?section=home&attendance_status=saved", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/student")
async def add_student(
    request: Request,
    session: Session = Depends(get_session),
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    course: Annotated[str, Form()] = "",
    comedor_enabled: Annotated[bool, Form()] = False,
    comedor_days: Annotated[str, Form()] = "",
    menu_type: Annotated[str, Form()] = "estandar",
    allergies: Annotated[str, Form()] = "",
    aula_matinal_enabled: Annotated[bool, Form()] = False,
    aula_matinal_days: Annotated[str, Form()] = "",
    aula_matinal_breakfast: Annotated[bool, Form()] = False,
    extras_selected: Annotated[list[str] | None, Form()] = None,
    extras: Annotated[str, Form()] = "",
    excursion_authorized: Annotated[bool, Form()] = False,
):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    normalized_extras = _normalize_extras(extras_selected, extras)
    normalized_allergy_items = _normalize_list_items(allergies)
    allergies_error = _validate_allergy_items(normalized_allergy_items)
    normalized_allergies = ",".join(normalized_allergy_items)
    cleaned_course = course.strip()
    comedor_selected = _selected_days(comedor_days)
    matinal_selected = _selected_days(aula_matinal_days)

    if allergies_error:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": allergies_error})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if not cleaned_course:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "El curso del alumno es obligatorio."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if comedor_enabled and not comedor_selected:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "Si activas el comedor, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if aula_matinal_enabled and not matinal_selected:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "Si activas el aula matinal, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if not first_name.strip() or not last_name.strip():
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "Nombre y apellidos son obligatorios para el alumno."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if not comedor_enabled:
        comedor_days = ""
    if not aula_matinal_enabled:
        aula_matinal_days = ""

    student = Student(
        family_id=profile.id,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        course=cleaned_course,
        comedor_days=comedor_days if comedor_enabled else "",
        menu_type=menu_type,
        allergies=normalized_allergies,
        aula_matinal_days=aula_matinal_days if aula_matinal_enabled else "",
        aula_matinal_breakfast=aula_matinal_breakfast and aula_matinal_enabled,
        extras=normalized_extras,
        excursion_authorized=excursion_authorized,
    )

    session.add(student)
    session.commit()
    return RedirectResponse(url="/user_family?section=estudiantes", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/student/{student_id}/edit")
async def edit_student(
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    course: Annotated[str, Form()] = "",
    comedor_enabled: Annotated[bool, Form()] = False,
    comedor_days: Annotated[str, Form()] = "",
    menu_type: Annotated[str, Form()] = "estandar",
    allergies: Annotated[str, Form()] = "",
    aula_matinal_enabled: Annotated[bool, Form()] = False,
    aula_matinal_days: Annotated[str, Form()] = "",
    aula_matinal_breakfast: Annotated[bool, Form()] = False,
    extras_selected: Annotated[list[str] | None, Form()] = None,
    extras: Annotated[str, Form()] = "",
    excursion_authorized: Annotated[bool, Form()] = False,
):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    student = _get_owned_student(session, profile.id, student_id)
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alumno no encontrado.")

    normalized_extras = _normalize_extras(extras_selected, extras)
    normalized_allergy_items = _normalize_list_items(allergies)
    allergies_error = _validate_allergy_items(normalized_allergy_items)
    normalized_allergies = ",".join(normalized_allergy_items)
    first_name_clean = first_name.strip()
    last_name_clean = last_name.strip()
    course_clean = course.strip()
    comedor_selected = _selected_days(comedor_days)
    matinal_selected = _selected_days(aula_matinal_days)

    if not first_name_clean or not last_name_clean or not course_clean or allergies_error:
        editing_student = {
            "id": student_id,
            "first_name": first_name_clean,
            "last_name": last_name_clean,
            "course": course_clean,
            "comedor_days": comedor_days,
            "menu_type": menu_type,
            "allergies": normalized_allergies,
            "aula_matinal_days": aula_matinal_days,
            "aula_matinal_breakfast": aula_matinal_breakfast,
            "extras": normalized_extras,
            "excursion_authorized": excursion_authorized,
        }
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=editing_student)
        if allergies_error:
            context.update({"error": allergies_error})
        elif not course_clean:
            context.update({"error": "El curso del alumno es obligatorio."})
        else:
            context.update({"error": "Nombre y apellidos son obligatorios para el alumno."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if comedor_enabled and not comedor_selected:
        editing_student = {"id": student_id, "first_name": first_name_clean, "last_name": last_name_clean, "course": course_clean, "comedor_days": comedor_days, "menu_type": menu_type, "allergies": normalized_allergies, "aula_matinal_days": aula_matinal_days, "aula_matinal_breakfast": aula_matinal_breakfast, "extras": normalized_extras, "excursion_authorized": excursion_authorized}
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=editing_student)
        context.update({"error": "Si activas el comedor, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if aula_matinal_enabled and not matinal_selected:
        editing_student = {"id": student_id, "first_name": first_name_clean, "last_name": last_name_clean, "course": course_clean, "comedor_days": comedor_days, "menu_type": menu_type, "allergies": normalized_allergies, "aula_matinal_days": aula_matinal_days, "aula_matinal_breakfast": aula_matinal_breakfast, "extras": normalized_extras, "excursion_authorized": excursion_authorized}
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=editing_student)
        context.update({"error": "Si activas el aula matinal, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    normalized_comedor_days = comedor_days if comedor_enabled else ""
    normalized_matinal_days = aula_matinal_days if aula_matinal_enabled else ""

    student.first_name = first_name_clean
    student.last_name = last_name_clean
    student.course = course_clean
    student.comedor_days = normalized_comedor_days
    student.menu_type = menu_type
    student.allergies = normalized_allergies
    student.aula_matinal_days = normalized_matinal_days
    student.aula_matinal_breakfast = aula_matinal_breakfast and aula_matinal_enabled
    student.extras = normalized_extras
    student.excursion_authorized = excursion_authorized
    session.commit()
    return RedirectResponse(url="/user_family?section=estudiantes", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/student/{student_id}/delete")
async def delete_student(student_id: int, request: Request, session: Session = Depends(get_session)):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    student = _get_owned_student(session, profile.id, student_id)
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alumno no encontrado.")

    session.delete(student)
    session.commit()
    return RedirectResponse(url="/user_family?section=estudiantes", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/contact")
async def add_contact(
    request: Request,
    session: Session = Depends(get_session),
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    dni_nie: Annotated[str, Form()] = "",
    phone_prefix: Annotated[str, Form()] = "+34",
    phone_number: Annotated[str, Form()] = "",
):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    first_name_clean = first_name.strip()
    last_name_clean = last_name.strip()
    dni_nie_clean = dni_nie.strip().upper()
    phone_clean = f"{phone_prefix}{phone_number.strip()}" if phone_number.strip() else phone_prefix

    if not first_name_clean or not last_name_clean or not dni_nie_clean or not phone_number.strip():
        editing_contact = {
            "id": None,
            "first_name": first_name_clean,
            "last_name": last_name_clean,
            "dni_nie": dni_nie_clean,
            "phone": phone_clean,
        }
        context = _build_family_dashboard_context(session, user, active_section="contactos", editing_contact=editing_contact)
        context.update({"error": "Nombre, apellidos, DNI/NIE y teléfono son obligatorios para el contacto."})
        context["contact_phone_prefix"] = phone_prefix
        context["contact_phone_number"] = phone_number.strip()
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    contact = TrustedContact(
        family_id=profile.id,
        first_name=first_name_clean,
        last_name=last_name_clean,
        dni_nie=dni_nie_clean,
        phone=phone_clean,
    )
    session.add(contact)
    session.commit()
    return RedirectResponse(url="/user_family?section=contactos", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/contact/{contact_id}/edit")
async def edit_contact(
    contact_id: int,
    request: Request,
    session: Session = Depends(get_session),
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    dni_nie: Annotated[str, Form()] = "",
    phone_prefix: Annotated[str, Form()] = "+34",
    phone_number: Annotated[str, Form()] = "",
):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    contact = _get_owned_contact(session, profile.id, contact_id)
    if not contact:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contacto no encontrado.")

    first_name_clean = first_name.strip()
    last_name_clean = last_name.strip()
    dni_nie_clean = dni_nie.strip().upper()
    phone_clean = f"{phone_prefix}{phone_number.strip()}" if phone_number.strip() else phone_prefix

    if not first_name_clean or not last_name_clean or not dni_nie_clean or not phone_number.strip():
        editing_contact = {
            "id": contact_id,
            "first_name": first_name_clean,
            "last_name": last_name_clean,
            "dni_nie": dni_nie_clean,
            "phone": phone_clean,
        }
        context = _build_family_dashboard_context(session, user, active_section="contactos", editing_contact=editing_contact)
        context.update({"error": "Nombre, apellidos, DNI/NIE y teléfono son obligatorios para el contacto."})
        context["contact_phone_prefix"] = phone_prefix
        context["contact_phone_number"] = phone_number.strip()
        return templates.TemplateResponse(request, "user_family.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    contact.first_name = first_name_clean
    contact.last_name = last_name_clean
    contact.dni_nie = dni_nie_clean
    contact.phone = phone_clean
    session.commit()
    return RedirectResponse(url="/user_family?section=contactos", status_code=status.HTTP_302_FOUND)


@app.post("/user_family/contact/{contact_id}/delete")
async def delete_contact(contact_id: int, request: Request, session: Session = Depends(get_session)):
    user = await require_user(request, session)
    profile = _get_family_profile(session, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Primero completa tus datos de familia.")

    contact = _get_owned_contact(session, profile.id, contact_id)
    if not contact:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contacto no encontrado.")

    session.delete(contact)
    session.commit()
    return RedirectResponse(url="/user_family?section=contactos", status_code=status.HTTP_302_FOUND)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    session: Session = Depends(get_session),
    section: str = "home",
    q: str | None = None,
    activity_status: str | None = None,
):
    try:
        user = await require_user(request, session)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role != "admin":
        return RedirectResponse(url="/user_family", status_code=status.HTTP_302_FOUND)

    allowed_sections = {"home", "usuarios", "extraescolares", "estudiantes"}
    active_section = section if section in allowed_sections else "home"

    students_all = session.exec(select(Student)).all()
    students = list(students_all)
    activities = session.exec(select(ExtracurricularActivity).order_by(ExtracurricularActivity.name.asc())).all()
    extra_price_map = {activity.name: activity.monthly_price for activity in activities if activity.is_active}
    if q:
        q_l = q.lower()
        students = [s for s in students if q_l in f"{s.first_name} {s.last_name} {s.course}".lower()]

    total_students = len(students)
    total_students_global = len(students_all)
    menu_counts = {"estandar": 0, "celiaco": 0, "vegetariano": 0}
    for student in students:
        menu_counts[student.menu_type] = menu_counts.get(student.menu_type, 0) + 1
    total_facturado = round(sum(calculate_student_total(student, extra_costs=extra_price_map) for student in students), 2)
    student_total_fn = _build_student_total_fn(extra_price_map)

    users = session.exec(select(User).order_by(User.email.asc())).all()
    family_profiles = session.exec(select(FamilyProfile)).all()
    profile_name_by_user_id = {profile.user_id: profile.parent_name for profile in family_profiles}
    users_by_role: dict[str, list[dict]] = {"admin": [], "familia": [], "comedor": []}
    for item in users:
        if item.role not in users_by_role:
            continue
        users_by_role[item.role].append(
            {
                "id": item.id,
                "display_name": _build_admin_user_display_name(
                    item.role, item.email, profile_name_by_user_id.get(item.id or -1)
                ),
                "email": item.email,
                "must_change_password": item.must_change_password,
                "role": item.role,
            }
        )

    family_users_total = sum(1 for item in users if item.role == "familia")
    offered_extracurriculars_total = sum(1 for activity in activities if activity.is_active)
    user_status = request.query_params.get("user_status", "")
    import_created = request.query_params.get("import_created", "0")
    import_skipped = request.query_params.get("import_skipped", "0")
    import_invalid = request.query_params.get("import_invalid", "0")
    import_email_errors = request.query_params.get("import_email_errors", "0")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "active_section": active_section,
            "students": students,
            "query": q or "",
            "total_students": total_students,
            "total_students_global": total_students_global,
            "menu_counts": menu_counts,
            "total_facturado": total_facturado,
            "student_total": student_total_fn,
            "activities": activities,
            "family_users_total": family_users_total,
            "offered_extracurriculars_total": offered_extracurriculars_total,
            "activity_status": activity_status or "",
            "users": users,
            "users_by_role": users_by_role,
            "user_status": user_status,
            "import_created": import_created,
            "import_skipped": import_skipped,
            "import_invalid": import_invalid,
            "import_email_errors": import_email_errors,
        },
    )


@app.post("/admin/users")
async def add_user(
    request: Request,
    session: Session = Depends(get_session),
    role: Annotated[str, Form()] = "familia",
    email: Annotated[str, Form()] = "",
):
    user = await require_user(request, session)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    normalized_role = role.strip().lower()
    if normalized_role == "admin":
        return RedirectResponse(url="/admin?section=usuarios&user_status=cannot_create_admin", status_code=status.HTTP_302_FOUND)
    if normalized_role not in {"familia", "comedor"}:
        return RedirectResponse(url="/admin?section=usuarios&user_status=invalid_role", status_code=status.HTTP_302_FOUND)

    normalized_email = email.lower().strip()
    if not normalized_email:
        return RedirectResponse(url="/admin?section=usuarios&user_status=empty_email", status_code=status.HTTP_302_FOUND)

    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing:
        return RedirectResponse(url="/admin?section=usuarios&user_status=exists", status_code=status.HTTP_302_FOUND)

    provisional_password = _generate_provisional_password()
    created_user = User(
        email=normalized_email,
        hashed_password=hash_password(provisional_password),
        role=normalized_role,
        is_active=True,
        must_change_password=True,
    )
    session.add(created_user)
    session.commit()
    try:
        _send_provisional_password_email(normalized_email, provisional_password, normalized_role)
    except (ValueError, smtplib.SMTPException, OSError):
        try:
            session.delete(created_user)
            session.commit()
        except SQLAlchemyError as cleanup_error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"No se pudo enviar el email y tampoco revertir la creación del usuario: {cleanup_error}",
            ) from cleanup_error
        return RedirectResponse(url="/admin?section=usuarios&user_status=email_error", status_code=status.HTTP_302_FOUND)

    return RedirectResponse(url="/admin?section=usuarios&user_status=created", status_code=status.HTTP_302_FOUND)


@app.post("/admin/users/import")
async def import_users_csv(
    request: Request,
    session: Session = Depends(get_session),
    role: Annotated[str, Form()] = "familia",
    send_email: Annotated[bool, Form()] = False,
    csv_file: UploadFile = File(...),
):
    current_user = await require_user(request, session)
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    normalized_role = role.strip().lower()
    if normalized_role not in {"familia", "comedor"}:
        return RedirectResponse(url="/admin?section=usuarios&user_status=invalid_role", status_code=status.HTTP_302_FOUND)

    if not csv_file.filename:
        return RedirectResponse(url="/admin?section=usuarios&user_status=import_empty_file", status_code=status.HTTP_302_FOUND)

    raw_content = await csv_file.read()
    if not raw_content.strip():
        return RedirectResponse(url="/admin?section=usuarios&user_status=import_empty_file", status_code=status.HTTP_302_FOUND)
    try:
        decoded_content = raw_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return RedirectResponse(url="/admin?section=usuarios&user_status=import_invalid_file", status_code=status.HTTP_302_FOUND)

    emails = _extract_emails_from_csv_content(decoded_content)
    if not emails:
        return RedirectResponse(url="/admin?section=usuarios&user_status=import_no_emails", status_code=status.HTTP_302_FOUND)

    created = 0
    skipped = 0
    invalid = 0
    email_errors = 0
    seen: set[str] = set()

    for email in emails:
        normalized_email = email.strip().lower()
        if normalized_email in seen:
            skipped += 1
            continue
        seen.add(normalized_email)

        if not _is_valid_user_email(normalized_email):
            invalid += 1
            continue

        existing = session.exec(select(User).where(User.email == normalized_email)).first()
        if existing:
            skipped += 1
            continue

        provisional_password = _generate_provisional_password()
        created_user = User(
            email=normalized_email,
            hashed_password=hash_password(provisional_password),
            role=normalized_role,
            is_active=True,
            must_change_password=True,
        )
        session.add(created_user)
        session.commit()

        if send_email:
            try:
                _send_provisional_password_email(normalized_email, provisional_password, normalized_role)
            except (ValueError, smtplib.SMTPException, OSError):
                try:
                    session.delete(created_user)
                    session.commit()
                except SQLAlchemyError as cleanup_error:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"No se pudo enviar el email y tampoco revertir la creación del usuario: {cleanup_error}",
                    ) from cleanup_error
                email_errors += 1
                continue

        created += 1

    status_code = "import_done"
    query = urlencode(
        {
            "section": "usuarios",
            "user_status": status_code,
            "import_created": created,
            "import_skipped": skipped,
            "import_invalid": invalid,
            "import_email_errors": email_errors,
        }
    )
    return RedirectResponse(url=f"/admin?{query}", status_code=status.HTTP_302_FOUND)


@app.post("/admin/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    current_user = await require_user(request, session)
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    target_user = session.get(User, user_id)
    if not target_user:
        return RedirectResponse(url="/admin?section=usuarios&user_status=not_found", status_code=status.HTTP_302_FOUND)
    if target_user.id == current_user.id:
        return RedirectResponse(url="/admin?section=usuarios&user_status=cannot_delete_self", status_code=status.HTTP_302_FOUND)
    if target_user.role == "admin":
        return RedirectResponse(url="/admin?section=usuarios&user_status=cannot_delete_admin", status_code=status.HTTP_302_FOUND)

    session.delete(target_user)
    session.commit()
    return RedirectResponse(url="/admin?section=usuarios&user_status=deleted", status_code=status.HTTP_302_FOUND)


@app.post("/admin/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    send_email: Annotated[bool, Form()] = True,
):
    current_user = await require_user(request, session)
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    target_user = session.get(User, user_id)
    if not target_user:
        return RedirectResponse(url="/admin?section=usuarios&user_status=not_found", status_code=status.HTTP_302_FOUND)
    if target_user.role == "admin":
        return RedirectResponse(url="/admin?section=usuarios&user_status=cannot_reset_admin", status_code=status.HTTP_302_FOUND)

    previous_hash = target_user.hashed_password
    previous_change_required = target_user.must_change_password
    provisional_password = _generate_provisional_password()
    target_user.hashed_password = hash_password(provisional_password)
    target_user.must_change_password = True
    session.add(target_user)
    session.commit()

    if send_email:
        try:
            _send_provisional_password_email(target_user.email, provisional_password, target_user.role)
        except (ValueError, smtplib.SMTPException, OSError):
            try:
                target_user.hashed_password = previous_hash
                target_user.must_change_password = previous_change_required
                session.add(target_user)
                session.commit()
            except SQLAlchemyError as rollback_error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"No se pudo enviar el email ni restaurar la contraseña previa: {rollback_error}",
                ) from rollback_error
            return RedirectResponse(url="/admin?section=usuarios&user_status=reset_email_error", status_code=status.HTTP_302_FOUND)
        return RedirectResponse(url="/admin?section=usuarios&user_status=reset_sent", status_code=status.HTTP_302_FOUND)

    return RedirectResponse(url="/admin?section=usuarios&user_status=reset_done_no_email", status_code=status.HTTP_302_FOUND)


@app.get("/admin/users/export")
async def export_users_csv(
    request: Request,
    session: Session = Depends(get_session),
    role: str = "familia",
):
    current_user = await require_user(request, session)
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    target_role = role.strip().lower()
    if target_role not in {"admin", "familia", "comedor"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rol no válido para exportación.")

    users = session.exec(select(User).where(User.role == target_role).order_by(User.email.asc())).all()
    family_profiles = session.exec(select(FamilyProfile)).all()
    profile_name_by_user_id = {profile.user_id: profile.parent_name for profile in family_profiles}

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["nombre", "email", "actualizar_contrasena"])
    for item in users:
        writer.writerow(
            [
                _build_admin_user_display_name(target_role, item.email, profile_name_by_user_id.get(item.id or -1)),
                item.email,
                "Sí" if item.must_change_password else "No",
            ]
        )

    filename = f"usuarios_{target_role}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/activities")
async def add_activity(
    request: Request,
    session: Session = Depends(get_session),
    name: Annotated[str, Form()] = "",
    monthly_price: Annotated[float, Form()] = 0.0,
    duration_hours: Annotated[float, Form()] = 1.0,
    material_required: Annotated[bool, Form()] = False,
    schedule: Annotated[str, Form()] = "Lunes 16:00",
):
    user = await require_user(request, session)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    clean_name = name.strip()
    clean_schedule = schedule.strip()
    if not clean_name:
        return RedirectResponse(url="/admin?section=extraescolares&activity_status=empty_name", status_code=status.HTTP_302_FOUND)
    if monthly_price < 0:
        return RedirectResponse(url="/admin?section=extraescolares&activity_status=invalid_price", status_code=status.HTTP_302_FOUND)
    if duration_hours <= 0:
        return RedirectResponse(url="/admin?section=extraescolares&activity_status=invalid_duration", status_code=status.HTTP_302_FOUND)
    if not clean_schedule:
        return RedirectResponse(url="/admin?section=extraescolares&activity_status=empty_schedule", status_code=status.HTTP_302_FOUND)

    existing = session.exec(select(ExtracurricularActivity).where(ExtracurricularActivity.name == clean_name)).first()
    if existing:
        existing.monthly_price = monthly_price
        existing.duration_hours = duration_hours
        existing.material_required = material_required
        existing.schedule = clean_schedule
        existing.is_active = True
        session.commit()
        return RedirectResponse(url="/admin?section=extraescolares&activity_status=updated", status_code=status.HTTP_302_FOUND)

    session.add(
        ExtracurricularActivity(
            name=clean_name,
            monthly_price=monthly_price,
            duration_hours=duration_hours,
            material_required=material_required,
            schedule=clean_schedule,
            is_active=True,
        )
    )
    session.commit()
    return RedirectResponse(url="/admin?section=extraescolares&activity_status=created", status_code=status.HTTP_302_FOUND)


@app.get("/admin/export-sepa")
async def export_sepa(request: Request, session: Session = Depends(get_session)):
    user = await require_user(request, session)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    profiles = session.exec(select(FamilyProfile).where(FamilyProfile.sepa_accepted)).all()
    if not profiles:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No hay familias con mandato SEPA activo.")

    config = {
        "name": "ANPA Comedor",
        "IBAN": "ES9121000418450200051332",
        "BIC": "CAIXESBB",
        "currency": "EUR",
        "batch": False,
    }
    transfer = SepaTransfer(config)
    extra_price_map = _get_extra_price_map(session)

    payment_count = 0
    for profile in profiles:
        total = family_total(profile.id, session, extra_price_map=extra_price_map)
        if total <= 0:
            continue
        transfer.add_payment(
            {
                "name": profile.parent_name,
                "IBAN": profile.iban,
                "amount": int(round(total * 100)),
                "description": f"Cuota comedor escolar - {profile.parent_name}",
                "execution_date": date.today() + timedelta(days=7),
                "endtoend_id": f"ANPA-{profile.id}-{date.today().strftime('%Y%m')}"[:35],
            }
        )
        payment_count += 1

    if payment_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hay cobros activos con importe mayor que cero.",
        )

    output = transfer.export(validate=True)
    target = Path("sepa_export.xml")
    target.write_text(output, encoding="utf-8")
    return FileResponse(target, media_type="application/xml", filename="remesa-sepa.xml")




@app.post("/setup-admin")
async def setup_admin(
    request: Request,
    session: Session = Depends(get_session),
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    if not _requires_initial_admin_setup(session):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    normalized_email = email.lower().strip()
    if not normalized_email:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "El email del administrador es obligatorio.",
                "setup_required": True,
                "setup_email": normalized_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "La contraseña debe tener al menos 8 caracteres.",
                "setup_required": True,
                "setup_email": normalized_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Las contraseñas no coinciden.",
                "setup_required": True,
                "setup_email": normalized_email,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = User(email=normalized_email, hashed_password=hash_password(password), role="admin", is_active=True)
    session.add(user)
    session.commit()

    token = create_access_token(user.email, user.role)
    redirect = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    set_auth_cookie(redirect, token)
    return redirect


@app.post("/seed-admin")
async def seed_admin(
    session: Session = Depends(get_session),
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    if not _requires_initial_admin_setup(session):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    normalized_email = email.lower().strip()
    if not normalized_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El email es obligatorio.")
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La contraseña debe tener al menos 8 caracteres.")

    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing:
        return {"message": "El administrador ya existe."}

    user = User(email=normalized_email, hashed_password=hash_password(password), role="admin", is_active=True)
    session.add(user)
    session.commit()
    return {"message": "Administrador creado correctamente."}
