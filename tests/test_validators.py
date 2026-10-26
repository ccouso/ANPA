import pytest

from src.app import _validate_password_change
from src.security import hash_password
from src.validators import is_valid_spanish_iban, normalize_iban


@pytest.mark.parametrize(
    "iban",
    [
        "ES9121000418450200051332",
        "ES91 2100 0418 4502 0005 1332",
        "ES6621000418401234567891",
    ],
)
def test_valid_spanish_iban(iban: str) -> None:
    assert is_valid_spanish_iban(iban)


@pytest.mark.parametrize(
    "iban",
    [
        "ES0021000418450200051332",
        "ES9121000418450200051333",
        "ES91",
        "DE89370400440532013000",
        "asdf",
    ],
)
def test_invalid_spanish_iban(iban: str) -> None:
    assert not is_valid_spanish_iban(iban)


def test_normalize_iban_removes_spaces_and_uppercases() -> None:
    assert normalize_iban("es91 2100 0418 4502 0005 1332") == "ES9121000418450200051332"


def test_normalize_iban_raises_for_invalid() -> None:
    with pytest.raises(ValueError, match="IBAN no válido"):
        normalize_iban("ES9121000418450200051333")


def test_validate_password_change_rejects_reusing_provisional_password() -> None:
    provisional = "Admin1234!"
    current_hash = hash_password(provisional)

    assert _validate_password_change(provisional, provisional, current_hash) == (
        "La nueva contraseña debe ser distinta a la contraseña provisional asignada por el administrador."
    )
    assert _validate_password_change("NuevaClave1!", "NuevaClave1!", current_hash) is None
