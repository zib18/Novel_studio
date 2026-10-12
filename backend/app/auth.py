import hashlib
import hmac
import secrets
import time
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import get_db
from .models import User, WebSession

ITERATIONS = 600_000


def fail(status, code, message, **extra):
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "$".join(("pbkdf2_sha256", str(ITERATIONS), salt.hex(), digest.hex()))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 100000 <= int(rounds) <= 2000000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_from_request(request: Request, db: Session):
    token = request.cookies.get(request.app.state.settings.cookie_name)
    if not token or len(token) > 200:
        return None
    result = db.get(WebSession, token_hash(token))
    if result and result.expires_at > time.time():
        return result
    return None


def create_session(request: Request, response: Response, db: Session, user_id=None):
    settings = request.app.state.settings
    token = secrets.token_urlsafe(32)
    session = WebSession(token_hash=token_hash(token), user_id=user_id,
                         csrf_token=secrets.token_urlsafe(32),
                         expires_at=time.time() + settings.session_days * 86400)
    db.add(session)
    db.commit()
    response.set_cookie(settings.cookie_name, token, max_age=settings.session_days * 86400,
                        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return session


def require_csrf(request: Request, db: Session = Depends(get_db)):
    origin = request.headers.get("origin")
    allowed = request.app.state.settings.allowed_origins
    if origin not in allowed:
        fail(403, "origin_forbidden", "Источник запроса не разрешён")
    if request.headers.get("sec-fetch-site") == "cross-site":
        fail(403, "cross_site", "Межсайтовый запрос запрещён")
    session = session_from_request(request, db)
    token = request.headers.get("x-csrf-token", "")
    if not session or not token or not hmac.compare_digest(token, session.csrf_token):
        fail(403, "csrf_failed", "Получите сессию и передайте действующий CSRF-токен")
    return session


def get_current_user(request: Request, db: Session = Depends(get_db)):
    session = session_from_request(request, db)
    if not session or not session.user_id:
        fail(401, "authentication_required", "Необходимо войти в студию")
    user = db.get(User, session.user_id)
    if not user:
        fail(401, "authentication_required", "Необходимо войти в студию")
    return user


def public_user(user):
    return {"id": user.id, "email": user.email} if user else None
