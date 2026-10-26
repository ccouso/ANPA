import os
from datetime import datetime, timedelta

import jwt
from fastapi import Response
from jwt import ExpiredSignatureError, InvalidTokenError
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
APP_SECRET = os.getenv("APP_SECRET", "ANPA-SUPER-SECRET-CHANGE-ME-IN-PRODUCTION")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return pwd_context.verify(password, hashed_password)


def create_access_token(email: str, role: str) -> str:
    payload = {
        "sub": email,
        "role": role,
        "exp": datetime.utcnow() + timedelta(days=7),
    }
    return jwt.encode(payload, APP_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, APP_SECRET, algorithms=["HS256"])


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
        max_age=7 * 24 * 60 * 60,
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie("access_token", path="/")


__all__ = [
    "ExpiredSignatureError",
    "InvalidTokenError",
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "set_auth_cookie",
    "clear_auth_cookie",
]
