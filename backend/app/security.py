from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from backend.app.models import User

passwords = PasswordHash.recommended()
dummy_hash = passwords.hash("nonexistent-account-timing-placeholder")
bearer = HTTPBearer(auto_error=False)


def session_dependency(request: Request):
    if request.app.state.engine is None:
        raise HTTPException(503, "数据库未配置")
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        yield session


def signing_key(request: Request):
    key = request.app.state.settings.jwt_secret.get_secret_value()
    if len(key) < 32:
        raise HTTPException(503, "请先配置至少32字符的 JWT_SECRET")
    return key


def issue_token(user: User, request: Request):
    now = datetime.now(UTC)
    return jwt.encode({"sub": str(user.id), "iat": now, "exp": now + timedelta(hours=1)}, signing_key(request), algorithm="HS256")


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), session: Session = Depends(session_dependency)):
    error = HTTPException(401, "登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    if credentials is None:
        raise error
    try:
        payload = jwt.decode(credentials.credentials, signing_key(request), algorithms=["HS256"], options={"require": ["sub", "exp", "iat"]})
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise error
    user = session.get(User, user_id)
    if user is None or not user.active:
        raise error
    return user


def administrator(user: User = Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "仅管理员可执行此操作")
    return user


def stock_writer(user: User = Depends(current_user)):
    if user.role not in {"admin", "storekeeper"}:
        raise HTTPException(403, "当前账号没有库存登记权限")
    return user
