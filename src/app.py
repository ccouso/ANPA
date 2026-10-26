from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sepaxml import SepaTransfer
from sqlmodel import Session, select

from .database import ExtracurricularActivity, FamilyProfile, Student, User, create_db_and_tables, engine, get_session
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
app = FastAPI(title="Comedor Escolar ANPA", version="1.2.0")

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


def _build_family_dashboard_context(
    session: Session,
    user: User,
    profile_override: dict | FamilyProfile | None = None,
    active_section: str = "home",
    profile_edit_mode: bool = False,
    editing_student: Student | dict | None = None,
) -> dict:
    profile_db = _get_family_profile(session, user.id)
    activities = _get_active_extracurricular_activities(session)
    extra_price_map = {activity.name: activity.monthly_price for activity in activities}
    student_total_fn = _build_student_total_fn(extra_price_map)

    students: list[Student] = []
    total = 0.0
    if profile_db:
        students = session.exec(select(Student).where(Student.family_id == profile_db.id)).all()
        total = round(sum(student_total_fn(student) for student in students), 2)

    return {
        "user": user,
        "profile": profile_override if profile_override is not None else profile_db,
        "profile_saved": profile_db is not None,
        "profile_edit_mode": profile_edit_mode,
        "active_section": active_section,
        "editing_student": editing_student,
        "students": students,
        "monthly_total": total,
        "student_total": student_total_fn,
        "extracurricular_activities": activities,
        "food_intolerance_options": FOOD_INTOLERANCE_OPTIONS,
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

    target = "/admin" if user.role == "admin" else "/dashboard"
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
        target = "/admin" if user.role == "admin" else "/dashboard"
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
    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"change_password_required": True, "user_email": user.email, "error": "La contraseña debe tener al menos 8 caracteres."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"change_password_required": True, "user_email": user.email, "error": "Las contraseñas no coinciden."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user.hashed_password = hash_password(password)
    user.must_change_password = False
    session.add(user)
    session.commit()

    target = "/admin" if user.role == "admin" else "/dashboard"
    redirect = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    return redirect


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    session: Session = Depends(get_session),
    section: str = "home",
    edit_profile: bool = False,
    edit_student_id: int | None = None,
):
    try:
        user = await require_user(request, session)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role != "familia":
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    allowed_sections = {"home", "perfil", "estudiantes"}
    active_section = section if section in allowed_sections else "home"

    editing_student: Student | None = None
    if active_section == "estudiantes" and edit_student_id is not None:
        profile = _get_family_profile(session, user.id)
        if profile:
            editing_student = _get_owned_student(session, profile.id, edit_student_id)

    context = _build_family_dashboard_context(
        session,
        user,
        active_section=active_section,
        profile_edit_mode=edit_profile,
        editing_student=editing_student,
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.post("/dashboard/profile")
async def save_family_profile(
    request: Request,
    session: Session = Depends(get_session),
    parent_name: Annotated[str, Form()] = "",
    dni_nie: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    iban: Annotated[str, Form()] = "",
    sepa_accepted: Annotated[bool, Form()] = False,
):
    user = await require_user(request, session)

    form_values = {
        "parent_name": parent_name,
        "dni_nie": dni_nie,
        "phone": phone,
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
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

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
    return RedirectResponse(url="/dashboard?section=perfil", status_code=status.HTTP_302_FOUND)


@app.post("/dashboard/student")
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
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if not cleaned_course:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "El curso del alumno es obligatorio."})
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if comedor_enabled and not comedor_selected:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "Si activas el comedor, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if aula_matinal_enabled and not matinal_selected:
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "Si activas el aula matinal, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if not first_name.strip() or not last_name.strip():
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=None)
        context.update({"error": "Nombre y apellidos son obligatorios para el alumno."})
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

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
    return RedirectResponse(url="/dashboard?section=estudiantes", status_code=status.HTTP_302_FOUND)


@app.post("/dashboard/student/{student_id}/edit")
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
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if comedor_enabled and not comedor_selected:
        editing_student = {"id": student_id, "first_name": first_name_clean, "last_name": last_name_clean, "course": course_clean, "comedor_days": comedor_days, "menu_type": menu_type, "allergies": normalized_allergies, "aula_matinal_days": aula_matinal_days, "aula_matinal_breakfast": aula_matinal_breakfast, "extras": normalized_extras, "excursion_authorized": excursion_authorized}
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=editing_student)
        context.update({"error": "Si activas el comedor, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

    if aula_matinal_enabled and not matinal_selected:
        editing_student = {"id": student_id, "first_name": first_name_clean, "last_name": last_name_clean, "course": course_clean, "comedor_days": comedor_days, "menu_type": menu_type, "allergies": normalized_allergies, "aula_matinal_days": aula_matinal_days, "aula_matinal_breakfast": aula_matinal_breakfast, "extras": normalized_extras, "excursion_authorized": excursion_authorized}
        context = _build_family_dashboard_context(session, user, active_section="estudiantes", editing_student=editing_student)
        context.update({"error": "Si activas el aula matinal, debe haber al menos un día seleccionado."})
        return templates.TemplateResponse(request, "dashboard.html", context, status_code=status.HTTP_400_BAD_REQUEST)

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
    return RedirectResponse(url="/dashboard?section=estudiantes", status_code=status.HTTP_302_FOUND)


@app.post("/dashboard/student/{student_id}/delete")
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
    return RedirectResponse(url="/dashboard?section=estudiantes", status_code=status.HTTP_302_FOUND)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    session: Session = Depends(get_session),
    q: str | None = None,
    activity_status: str | None = None,
):
    try:
        user = await require_user(request, session)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

    students = session.exec(select(Student)).all()
    activities = session.exec(select(ExtracurricularActivity).order_by(ExtracurricularActivity.name.asc())).all()
    extra_price_map = {activity.name: activity.monthly_price for activity in activities if activity.is_active}
    if q:
        q_l = q.lower()
        students = [s for s in students if q_l in f"{s.first_name} {s.last_name} {s.course}".lower()]

    total_students = len(students)
    menu_counts = {"estandar": 0, "celiaco": 0, "vegetariano": 0}
    for student in students:
        menu_counts[student.menu_type] = menu_counts.get(student.menu_type, 0) + 1
    total_facturado = round(sum(calculate_student_total(student, extra_costs=extra_price_map) for student in students), 2)
    student_total_fn = _build_student_total_fn(extra_price_map)

    users = session.exec(select(User).order_by(User.email.asc())).all()
    user_status = request.query_params.get("user_status", "")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "students": students,
            "query": q or "",
            "total_students": total_students,
            "menu_counts": menu_counts,
            "total_facturado": total_facturado,
            "student_total": student_total_fn,
            "activities": activities,
            "activity_status": activity_status or "",
            "users": users,
            "user_status": user_status,
        },
    )


@app.post("/admin/users")
async def add_user(
    request: Request,
    session: Session = Depends(get_session),
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    user = await require_user(request, session)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No autorizado")

    normalized_email = email.lower().strip()
    if not normalized_email:
        return RedirectResponse(url="/admin?user_status=empty_email", status_code=status.HTTP_302_FOUND)
    if len(password) < 8:
        return RedirectResponse(url="/admin?user_status=weak_password", status_code=status.HTTP_302_FOUND)
    if password != password_confirm:
        return RedirectResponse(url="/admin?user_status=bad_match", status_code=status.HTTP_302_FOUND)

    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing:
        return RedirectResponse(url="/admin?user_status=exists", status_code=status.HTTP_302_FOUND)

    session.add(
        User(
            email=normalized_email,
            hashed_password=hash_password(password),
            role="familia",
            is_active=True,
            must_change_password=True,
        )
    )
    session.commit()
    return RedirectResponse(url="/admin?user_status=created", status_code=status.HTTP_302_FOUND)


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
        return RedirectResponse(url="/admin?activity_status=empty_name", status_code=status.HTTP_302_FOUND)
    if monthly_price < 0:
        return RedirectResponse(url="/admin?activity_status=invalid_price", status_code=status.HTTP_302_FOUND)
    if duration_hours <= 0:
        return RedirectResponse(url="/admin?activity_status=invalid_duration", status_code=status.HTTP_302_FOUND)
    if not clean_schedule:
        return RedirectResponse(url="/admin?activity_status=empty_schedule", status_code=status.HTTP_302_FOUND)

    existing = session.exec(select(ExtracurricularActivity).where(ExtracurricularActivity.name == clean_name)).first()
    if existing:
        existing.monthly_price = monthly_price
        existing.duration_hours = duration_hours
        existing.material_required = material_required
        existing.schedule = clean_schedule
        existing.is_active = True
        session.commit()
        return RedirectResponse(url="/admin?activity_status=updated", status_code=status.HTTP_302_FOUND)

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
    return RedirectResponse(url="/admin?activity_status=created", status_code=status.HTTP_302_FOUND)


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
