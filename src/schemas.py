from pydantic import BaseModel, EmailStr, field_validator

from .validators import normalize_dni_nie, normalize_iban, normalize_phone


class FamilyForm(BaseModel):
    parent_name: str
    dni_nie: str
    phone: str
    email: EmailStr
    iban: str
    sepa_accepted: bool = False

    @field_validator("dni_nie")
    @classmethod
    def validate_dni_nie(cls, value: str) -> str:
        return normalize_dni_nie(value)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("iban")
    @classmethod
    def validate_iban(cls, value: str) -> str:
        return normalize_iban(value)
