from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app import models

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@umt\.edu\.pk$", re.IGNORECASE)
STUDENT_ID_RE = re.compile(r"^[Ff]\d{10}$")


def validate_umt_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def validate_student_id(sid: str) -> bool:
    return bool(STUDENT_ID_RE.match(sid))


def _bcrypt_secret_bytes(plain: str) -> bytes:
    """Bcrypt uses at most the first 72 bytes of the UTF-8 password (legacy truncation)."""
    return plain.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return pwd_context.hash(_bcrypt_secret_bytes(password))


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(_bcrypt_secret_bytes(plain), hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached

    payload = decode_token(credentials.credentials)
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    try:
        user_id = int(sub)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    request.state.current_user = user
    return user


def require_lead(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "lead":
        raise HTTPException(status_code=403, detail="Team lead access required")
    return user


def require_member(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "member":
        raise HTTPException(status_code=403, detail="Member access required")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

