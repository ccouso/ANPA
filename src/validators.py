import re


def normalize_dni_nie(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"(?:\d{8}[A-Z])|(?:[XYZ]\d{7}[A-Z])", normalized):
        raise ValueError("DNI/NIE inválido.")
    return normalized


def normalize_phone(value: str) -> str:
    normalized = value.strip().replace(" ", "")
    if not re.fullmatch(r"\+?[0-9]{9,15}", normalized):
        raise ValueError("Número de teléfono no válido.")
    return normalized


def _iban_to_numeric(iban: str) -> str:
    chars: list[str] = []
    for ch in iban:
        if ch.isdigit():
            chars.append(ch)
        elif "A" <= ch <= "Z":
            chars.append(str(ord(ch) - 55))
        else:
            raise ValueError("IBAN no válido.")
    return "".join(chars)


def is_valid_spanish_iban(iban: str) -> bool:
    clean = iban.replace(" ", "").upper()
    if not re.fullmatch(r"ES\d{22}", clean):
        return False
    rearranged = clean[4:] + clean[:4]
    numeric = _iban_to_numeric(rearranged)
    return int(numeric) % 97 == 1


def normalize_iban(value: str) -> str:
    normalized = value.replace(" ", "").upper()
    if not is_valid_spanish_iban(normalized):
        raise ValueError("IBAN no válido.")
    return normalized
