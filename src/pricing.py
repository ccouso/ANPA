from .database import Student


def parse_days(value: str) -> list[str]:
    if not value:
        return []
    return [day for day in ["L", "M", "X", "J", "V"] if day in value.upper().replace(" ", "")]


def parse_selected_extras(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


DEFAULT_EXTRA_COSTS = {
    "Inglés": 18.0,
    "Robótica": 20.0,
    "Baloncesto": 16.0,
}


def calculate_student_total(student: Student, extra_costs: dict[str, float] | None = None) -> float:
    total = 0.0

    comedor_days = parse_days(student.comedor_days)
    if comedor_days:
        multiplier = 1.35 if student.menu_type == "celiaco" else 1.00
        total += len(comedor_days) * 6.5 * multiplier
        if student.allergies:
            total += len(comedor_days) * 0.75

    matinal_days = parse_days(student.aula_matinal_days)
    if matinal_days:
        total += len(matinal_days) * 4.5
        if student.aula_matinal_breakfast:
            total += len(matinal_days) * 1.2

    if student.extras:
        extras = parse_selected_extras(student.extras)
        costs = extra_costs if extra_costs is not None else DEFAULT_EXTRA_COSTS
        total += sum(costs.get(item, 0.0) for item in extras)

    return round(total, 2)
