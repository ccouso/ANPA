import os
from collections.abc import Generator
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./comedor.db")
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, nullable=False)
    hashed_password: str = Field(nullable=False)
    role: str = Field(default="familia", nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    must_change_password: bool = Field(default=False, nullable=False)


class FamilyProfile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, nullable=False)
    parent_name: str = Field(nullable=False)
    dni_nie: str = Field(nullable=False)
    phone: str = Field(nullable=False)
    email: str = Field(nullable=False)
    iban: str = Field(nullable=False)
    sepa_accepted: bool = Field(default=False, nullable=False)


class Student(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    family_id: int = Field(foreign_key="familyprofile.id", nullable=False)
    first_name: str = Field(nullable=False)
    last_name: str = Field(nullable=False)
    course: str = Field(default="", nullable=False)
    comedor_days: str = Field(default="", nullable=False)
    menu_type: str = Field(default="estandar", nullable=False)
    allergies: str = Field(default="", nullable=False)
    aula_matinal_days: str = Field(default="", nullable=False)
    aula_matinal_breakfast: bool = Field(default=False, nullable=False)
    extras: str = Field(default="", nullable=False)
    excursion_authorized: bool = Field(default=False, nullable=False)


class ExtracurricularActivity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, nullable=False)
    monthly_price: float = Field(default=0.0, nullable=False)
    duration_hours: float = Field(default=1.0, nullable=False)
    material_required: bool = Field(default=False, nullable=False)
    schedule: str = Field(default="Lunes 16:00", nullable=False)
    is_active: bool = Field(default=True, nullable=False)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    ensure_database_compatibility()


def ensure_database_compatibility() -> None:
    with engine.begin() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info('extracurricularactivity')").fetchall()
        columns = {row[1] for row in rows}
        if "duration_hours" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE extracurricularactivity ADD COLUMN duration_hours REAL NOT NULL DEFAULT 1.0"
            )
        if "material_required" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE extracurricularactivity ADD COLUMN material_required INTEGER NOT NULL DEFAULT 0"
            )
        if "schedule" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE extracurricularactivity ADD COLUMN schedule TEXT NOT NULL DEFAULT 'Lunes 16:00'"
            )


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
