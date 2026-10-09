import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession
from backend.config import settings
from backend.db import get_db
from backend.models import AuditLog, LoginSession, User

router = APIRouter(prefix="/auth", tags=["认证"])
attempts: dict[str, deque] = defaultdict(deque)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2-sha256${salt}${hashed}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, _ = encoded.split("$")
        return hmac.compare_digest(hash_password(password, salt), encoded)
    except ValueError:
        return False


DUMMY_HASH = hash_password('nonexistent', '00'*16)


def admin(request: Request, db: DBSession = Depends(get_db)) -> User:
    token = request.cookies.get("airdrop_session", "")
    session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
    if not session or session.expires_at <= time.time():
        raise HTTPException(401, "请先登录")
    user = db.get(User, session.user_id)
    if not user or user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise HTTPException(403, "CSRF 验证失败，请重新登录")
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins.split(","):
            raise HTTPException(403, "来源不受信任")
    request.state.user = user
    return user


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: DBSession = Depends(get_db)):
    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins.split(","):
        raise HTTPException(403, "来源不受信任")
    ip = request.client.host if request.client else "local"
    queue = attempts[ip]
    while queue and queue[0] < time.time() - 60:
        queue.popleft()
    if len(queue) >= 10:
        raise HTTPException(429, "登录过于频繁，请稍后再试")
    queue.append(time.time())
    user = db.scalar(select(User).where(User.username == body.username))
    # Equal-cost password verification avoids a fast username existence oracle.
    encoded = user.password_hash if user else DUMMY_HASH
    if not verify_password(body.password, encoded) or not user:
        raise HTTPException(401, "用户名或密码错误")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, csrf=csrf, expires_at=time.time()+8*3600))
    db.add(AuditLog(user_id=user.id, action="auth.login", target=user.id))
    db.commit()
    response.set_cookie("airdrop_session", token, httponly=True, secure=settings.secure_cookie, samesite="strict", max_age=8*3600, path="/")
    return {"username": user.username, "csrf": csrf, "role": user.role}


@router.get("/me")
def me(request: Request, user: User = Depends(admin), db: DBSession = Depends(get_db)):
    token_hash = hashlib.sha256(request.cookies["airdrop_session"].encode()).hexdigest()
    return {"username": user.username, "csrf": db.get(LoginSession, token_hash).csrf, "role": user.role}


@router.post("/logout")
def logout(request: Request, response: Response, _: User = Depends(admin), db: DBSession = Depends(get_db)):
    db.delete(db.get(LoginSession, hashlib.sha256(request.cookies["airdrop_session"].encode()).hexdigest()))
    db.commit()
    response.delete_cookie("airdrop_session")
    return {"ok": True}
