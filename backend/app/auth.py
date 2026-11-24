import base64
import binascii
import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Annotated, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from .db import APP_URL, Credential, Session, User, now

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
passwords = PasswordHasher()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(24))
COOKIE = "ebook_session"


def get_db():
    with Session() as db:
        yield db


DB = Annotated[DBSession, Depends(get_db)]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_token(db, user, scope, label="", expires=None):
    value = secrets.token_urlsafe(32)
    db.add(Credential(digest=digest(value), user_id=user.id, scope=scope, label=label, expires=expires))
    return value


def authenticate(db, value, scope):
    if not value or len(value) > 128:
        return None
    cred = db.scalar(select(Credential).where(Credential.digest == digest(value), Credential.scope == scope,
                                             (Credential.expires.is_(None) | (Credential.expires > now()))))
    return db.get(User, cred.user_id) if cred else None


def current(request: Request, db: DB):
    user = authenticate(db, request.cookies.get(COOKIE), "web")
    if not user:
        raise HTTPException(401, "Войдите в библиотеку")
    expected = request.headers.get("x-polka-user")
    if expected is not None and expected != str(user.id):
        raise HTTPException(409, "Учётная запись в браузере изменилась")
    return user


Current = Annotated[User, Depends(current)]


def admin(user: Current):
    if not user.admin:
        raise HTTPException(403, "Требуются права администратора")
    return user


Admin = Annotated[User, Depends(admin)]


def opds_user(request: Request, db: DB):
    login = request.headers.get("authorization", "").split(" ", 1)
    user = None
    if len(login) == 2:
        if login[0].lower() == "bearer":
            user = authenticate(db, login[1], "opds")
        elif login[0].lower() == "basic":
            try:
                username, token = base64.b64decode(login[1], validate=True).decode().split(":", 1)
                candidate = authenticate(db, token, "opds")
                if candidate and username.casefold() == candidate.username:
                    user = candidate
            except (ValueError, UnicodeError, binascii.Error):
                pass
    else:
        user = authenticate(db, request.cookies.get(COOKIE), "web")
    if not user:
        raise HTTPException(401, "Нужен персональный токен OPDS",
                            headers={"WWW-Authenticate": 'Basic realm="EBook OPDS", charset="UTF-8"'})
    return user


OpdsUser = Annotated[User, Depends(opds_user)]


class Login(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[\w.@-]+$")
    password: str = Field(min_length=1, max_length=256)


class Setup(Login):
    setup_key: str = Field(default="", max_length=256)
    password: str = Field(min_length=10, max_length=256)


class UserCreate(Login):
    admin: bool = False
    read_only: bool = False
    password: str = Field(min_length=10, max_length=256)


class Label(BaseModel):
    label: str = Field(min_length=1, max_length=80)


def describe(user):
    return {"id": user.id, "username": user.username, "admin": user.admin, "read_only": user.read_only}


def start_session(response, db, user):
    value = new_token(db, user, "web", expires=now() + timedelta(days=30))
    db.commit()
    response.set_cookie(COOKIE, value, max_age=30 * 86400, httponly=True, samesite="strict",
                        secure=APP_URL.startswith("https://"))
    return describe(user)


@router.get("/status")
def status(db: DB):
    return {"setup": db.scalar(select(func.count()).select_from(User)) == 0,
            "setup_key_required": bool(os.getenv("SETUP_KEY"))}


@router.post("/setup", status_code=201)
def setup(payload: Setup, response: Response, db: DB):
    key = os.getenv("SETUP_KEY", "")
    if key and not hmac.compare_digest(key.encode(), payload.setup_key.encode()):
        raise HTTPException(403, "Неверный ключ настройки")
    if db.bind.dialect.name == "postgresql":
        db.execute(text("LOCK TABLE users IN EXCLUSIVE MODE"))
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "Библиотека уже настроена")
    user = User(username=payload.username.casefold(), password=passwords.hash(payload.password), admin=True)
    db.add(user)
    db.flush()
    return start_session(response, db, user)


@router.post("/login")
def login(payload: Login, response: Response, db: DB):
    user = db.scalar(select(User).filter_by(username=payload.username.casefold()).with_for_update())
    try:
        passwords.verify(user.password if user else DUMMY_HASH, payload.password)
    except (VerificationError, InvalidHashError):
        raise HTTPException(401, "Проверьте имя и пароль") from None
    if not user:
        raise HTTPException(401, "Проверьте имя и пароль")
    return start_session(response, db, user)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: DB, user: Current):
    cred = db.get(Credential, digest(request.cookies[COOKIE]))
    if cred:
        db.delete(cred)
    db.commit()
    response.delete_cookie(COOKIE)


@router.get("/me")
def me(user: Current):
    return describe(user)


@router.post("/users", status_code=201)
def add_user(payload: UserCreate, db: DB, user: Admin):
    if payload.admin and payload.read_only:
        raise HTTPException(422, "Выберите одну роль")
    name = payload.username.casefold()
    if db.scalar(select(User).filter_by(username=name)):
        raise HTTPException(409, "Пользователь уже существует")
    created = User(username=name, password=passwords.hash(payload.password), admin=payload.admin, read_only=payload.read_only)
    db.add(created)
    try:
        db.commit()
    except IntegrityError:
        raise HTTPException(409, "Пользователь уже существует") from None
    return describe(created)


class RoleInput(BaseModel):
    role: Literal["admin", "member", "reader"]


class PasswordInput(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class PasswordChange(PasswordInput):
    current_password: str = Field(min_length=1, max_length=256)


@router.get("/users")
def users(db: DB, user: Admin):
    return [describe(entry) for entry in db.scalars(select(User).order_by(User.username))]


def manage_user(db, actor, user_id):
    if actor.id == user_id:
        raise HTTPException(409, "Используйте настройки своей учётной записи")
    if db.bind.dialect.name == "postgresql":
        db.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
    db.refresh(actor)
    admin(actor)
    owner = db.get(User, user_id)
    if not owner:
        raise HTTPException(404, "Пользователь не найден")
    return owner


@router.patch("/users/{user_id}/role")
def change_role(user_id: int, payload: RoleInput, db: DB, user: Admin):
    owner = manage_user(db, user, user_id)
    owner.admin, owner.read_only = payload.role == "admin", payload.role == "reader"
    db.commit()
    return describe(owner)


@router.put("/users/{user_id}/password", status_code=204)
def reset_password(user_id: int, payload: PasswordInput, db: DB, user: Admin):
    owner = manage_user(db, user, user_id)
    owner.password = passwords.hash(payload.password)
    db.execute(delete(Credential).where(Credential.user_id == owner.id))
    db.commit()


@router.put("/password")
def change_password(payload: PasswordChange, response: Response, db: DB, user: Current):
    db.refresh(user, with_for_update=True)
    try:
        passwords.verify(user.password, payload.current_password)
    except (VerificationError, InvalidHashError):
        raise HTTPException(403, "Проверьте текущий пароль") from None
    user.password = passwords.hash(payload.password)
    db.execute(delete(Credential).where(Credential.user_id == user.id, Credential.scope == "web"))
    return start_session(response, db, user)


@router.get("/sessions")
def sessions(request: Request, db: DB, user: Current):
    active = digest(request.cookies[COOKIE])
    rows = db.scalars(select(Credential).where(Credential.user_id == user.id, Credential.scope == "web",
                                            Credential.expires > now()).order_by(Credential.expires.desc()))
    return [{"id": row.digest, "current": row.digest == active, "expires": row.expires} for row in rows]


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(session_id: str, request: Request, response: Response, db: DB, user: Current):
    row = db.get(Credential, session_id)
    if not row or row.user_id != user.id or row.scope != "web":
        raise HTTPException(404, "Сеанс не найден")
    db.delete(row)
    db.commit()
    if session_id == digest(request.cookies[COOKIE]):
        response.delete_cookie(COOKIE)


@router.get("/opds-tokens")
def tokens(db: DB, user: Current):
    return [{"id": c.digest, "label": c.label} for c in db.scalars(select(Credential).filter_by(user_id=user.id, scope="opds"))]


@router.post("/opds-tokens", status_code=201)
def issue_token(payload: Label, db: DB, user: Current):
    secret = new_token(db, user, "opds", payload.label)
    db.commit()
    return {"token": secret, "username": user.username}


@router.delete("/opds-tokens/{token_id}", status_code=204)
def revoke_token(token_id: str, db: DB, user: Current):
    cred = db.get(Credential, token_id)
    if not cred or cred.user_id != user.id or cred.scope != "opds":
        raise HTTPException(404, "Токен не найден")
    db.delete(cred)
    db.commit()

