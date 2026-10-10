from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from backend import auth
from backend.auth import hash_password
from backend.config import settings
from backend.db import Session, engine
from backend.models import User
from backend import scripts_api
from backend import wallets_api, projects_api
from backend import adaptation_api
from backend import execution_api,schedules_api,system_api
from backend import deletion_api
from backend import analysis_api
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(app):
    with Session.begin() as db:
        if not db.scalar(select(User).where(User.username == settings.admin_username)):
            if len(settings.admin_password) < 12:
                raise RuntimeError("请先设置至少 12 字符的 AIRDROP_ADMIN_PASSWORD；可运行 scripts/init_instance.py")
            db.add(User(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
    yield


app = FastAPI(title="AirDrop V2", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins.split(","), allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])
app.include_router(auth.router, prefix="/api")
app.include_router(scripts_api.router, prefix="/api")
app.include_router(wallets_api.router, prefix="/api")
app.include_router(projects_api.router, prefix="/api")
app.include_router(adaptation_api.router, prefix="/api")
app.include_router(execution_api.router, prefix="/api")
app.include_router(schedules_api.router, prefix="/api")
app.include_router(system_api.router, prefix="/api")
app.include_router(deletion_api.router, prefix="/api")
app.include_router(analysis_api.router, prefix="/api")


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Pydantic's default error payload may include a rejected secret value.
    return JSONResponse(status_code=422,content={'detail':[{'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in exc.errors()]})


@app.get("/api/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "mode": "management", "execution_enabled": settings.execution_enabled, "real_signing_enabled": False}
