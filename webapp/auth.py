"""鉴权：本地用户表（JSON）+ pbkdf2 密码 + itsdangerous 签名 cookie。

用户已确认无 SSO/LDAP，故用最简的本地账号体系。无数据库——用户存
webapp/data/users.json（gitignored）。每用户可存自己的 Jira PAT（按用户凭证，
仅服务端使用、掩码写入、绝不回显明文），供 in-process 只读 Jira 与弱链子进程注入。

用户管理（命令行）：
  python -m webapp.auth adduser <用户名> [--name 显示名] [--role 角色]
  python -m webapp.auth passwd  <用户名>
  python -m webapp.auth setpat  <用户名>          # 交互输入，不回显
  python -m webapp.auth list
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from time import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_ITERATIONS = 200_000
_ALGO = "sha256"
_CRYPT_PREFIX = "fernet:"


def _cipher_or_none():
    try:
        from core.store.crypto import CredentialCipher, configured_keys
        return CredentialCipher(configured_keys(config.FERNET_KEY_FILE))
    except Exception:
        return None


def _encrypt_secret(value: str) -> str:
    if not value:
        return ""
    if str(value).startswith(_CRYPT_PREFIX):
        return value
    cipher = _cipher_or_none()
    if cipher is None:
        return value
    encrypted = cipher.encrypt(value)
    return _CRYPT_PREFIX + encrypted.ciphertext.decode("ascii")


def _decrypt_secret(value: str) -> str:
    if not value or not str(value).startswith(_CRYPT_PREFIX):
        return value or ""
    cipher = _cipher_or_none()
    if cipher is None:
        return ""
    return cipher.decrypt(str(value)[len(_CRYPT_PREFIX):])


def _decrypt_ai(ai: dict | None) -> dict:
    out = dict(ai or {})
    for kind in ("weak", "strong"):
        if isinstance(out.get(kind), dict):
            cur = dict(out[kind])
            cur["api_key"] = _decrypt_secret(cur.get("api_key", ""))
            out[kind] = cur
    return out


def _encrypt_ai(ai: dict | None) -> dict:
    out = dict(ai or {})
    for kind in ("weak", "strong"):
        if isinstance(out.get(kind), dict):
            cur = dict(out[kind])
            cur["api_key"] = _encrypt_secret(cur.get("api_key", ""))
            out[kind] = cur
    return out


def _db_engine():
    try:
        return config.platform_engine()
    except Exception:
        return None


def _db_cipher():
    try:
        from core.store.crypto import CredentialCipher, configured_keys
        return CredentialCipher(configured_keys(config.FERNET_KEY_FILE))
    except Exception:
        return None


def _db_role(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized in {"admin", "管理员"}:
        return "admin"
    if normalized in {"viewer", "只读", "只读用户"}:
        return "viewer"
    return "qa"


def _display_role(role: str) -> str:
    if role == "admin":
        return "admin"
    if role == "viewer":
        return "viewer"
    return "测试工程师"


def _jira_identity_payload(display_name: str, base_url: str) -> dict:
    return {
        "display_name": str(display_name or ""),
        "base_url": (base_url or "").strip().rstrip("/"),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------- 密码哈希 -----------------------------

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return salt, dk.hex()


def _verify(password: str, salt: str, expected_hash: str) -> bool:
    try:
        _, got = hash_password(password, salt)
    except ValueError:
        return False
    return hmac.compare_digest(got, expected_hash)


# ----------------------------- 用户模型 -----------------------------

@dataclass
class User:
    username: str
    display_name: str = ""
    role: str = "测试工程师"
    salt: str = ""
    pwd_hash: str = ""
    jira_pat: str = ""  # 该用户自己的 Jira PAT；仅服务端用
    jira_url: str = ""  # 该用户自定义 Jira 地址；空则用系统默认
    jira_identity: dict = field(default_factory=dict)  # Jira 连接探针的非敏感结果
    # 该用户独立的 AI 配置：{"weak": {base_url,api_key,model}, "strong": {base_url,api_key,model}}
    # weak=生成模型(写用例)，strong=复核模型(查用例)。仅服务端用、绝不回显 api_key。
    ai: dict = field(default_factory=dict)

    def public(self) -> dict:
        """供模板使用的安全视图——不含任何机密。"""
        return {
            "username": self.username,
            "display_name": self.display_name or self.username,
            "role": self.role,
            "has_jira_pat": bool(self.jira_pat),
            "jira_url": self.jira_url,
            "jira_identity": self.jira_identity,
            "initial": (self.display_name or self.username)[:1],
        }

    def ai_view(self) -> dict:
        """AI 配置的非密视图（base_url/model 明文，api_key 仅给是否已配的布尔）。"""
        def one(kind: str) -> dict:
            c = (self.ai or {}).get(kind) or {}
            return {"base_url": c.get("base_url", ""), "model": c.get("model", ""),
                    "has_key": bool(c.get("api_key")),
                    "provider": (c.get("provider") or "anthropic")}
        return {"weak": one("weak"), "strong": one("strong")}


# ----------------------------- 用户存储 -----------------------------

class UserStore:
    def __init__(self, path: Path = config.USERS_FILE) -> None:
        self.path = path

    def _db_enabled(self) -> bool:
        return self.path == config.USERS_FILE and _db_engine() is not None

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {"users": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {"users": []}
        except Exception:
            return {"users": []}

    def _save_raw(self, data: dict) -> None:
        config._ensure_data_dir()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子写

    def _json_all(self) -> list[User]:
        valid = {f.name for f in fields(User)}
        users = []
        for raw in self._load_raw().get("users", []):
            data = {k: v for k, v in dict(raw).items() if k in valid}
            data["jira_pat"] = _decrypt_secret(data.get("jira_pat", ""))
            data["ai"] = _decrypt_ai(data.get("ai") or {})
            users.append(User(**data))
        return users

    def _from_db_user(self, db_user, repo=None) -> User:
        ai = {}
        jira_pat = ""
        jira_url = ""
        jira_identity = {}
        for cred in getattr(db_user, "credentials", []) or []:
            secret = ""
            if repo is not None and cred.encrypted_value:
                try:
                    secret = repo.cipher.decrypt(cred.encrypted_value)
                except Exception:
                    secret = ""
            if cred.kind == "jira":
                jira_pat = secret
                jira_url = cred.base_url or ""
                jira_identity = dict((cred.metadata_json or {}).get("identity") or {})
            elif cred.kind in ("weak", "strong"):
                ai[cred.kind] = {
                    "provider": cred.provider or "anthropic",
                    "base_url": cred.base_url or "",
                    "model": cred.model or "",
                    "api_key": secret,
                }
        return User(
            username=db_user.username,
            display_name=db_user.display_name or db_user.username,
            role=_display_role(db_user.role),
            salt=db_user.password_salt or "",
            pwd_hash=db_user.password_hash or "",
            jira_pat=jira_pat,
            jira_url=jira_url,
            jira_identity=jira_identity,
            ai=ai,
        )

    def _db_get(self, username: str) -> Optional[User]:
        engine = _db_engine()
        cipher = _db_cipher()
        if engine is None or cipher is None:
            return None
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            from core.store import models
            from core.store.db import session_scope
            from core.store.repositories import CredentialRepository
            with session_scope(engine) as session:
                db_user = session.scalar(
                    select(models.User)
                    .options(selectinload(models.User.credentials))
                    .where(models.User.username == username, models.User.active.is_(True))
                )
                if db_user is None:
                    return None
                return self._from_db_user(db_user, CredentialRepository(session, cipher))
        except Exception:
            return None

    def _db_all(self) -> list[User] | None:
        engine = _db_engine()
        cipher = _db_cipher()
        if engine is None or cipher is None:
            return None
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            from core.store import models
            from core.store.db import session_scope
            from core.store.repositories import CredentialRepository
            with session_scope(engine) as session:
                repo = CredentialRepository(session, cipher)
                rows = session.scalars(
                    select(models.User)
                    .options(selectinload(models.User.credentials))
                    .where(models.User.active.is_(True))
                    .order_by(models.User.username)
                ).all()
                return [self._from_db_user(row, repo) for row in rows]
        except Exception:
            return None

    def _db_upsert(self, user: User) -> bool:
        engine = _db_engine()
        cipher = _db_cipher()
        if engine is None or cipher is None:
            return False
        try:
            from sqlalchemy import select
            from core.store import models
            from core.store.db import session_scope
            from core.store.repositories import CredentialRepository
            with session_scope(engine) as session:
                db_user = session.scalar(select(models.User).where(models.User.username == user.username))
                if db_user is None:
                    db_user = models.User(username=user.username)
                    session.add(db_user)
                db_user.display_name = user.display_name or user.username
                db_user.role = _db_role(user.role)
                db_user.password_salt = user.salt or ""
                db_user.password_hash = user.pwd_hash or ""
                db_user.active = True
                session.flush()
                repo = CredentialRepository(session, cipher)
                jira_metadata = {"identity": dict(user.jira_identity or {})} if user.jira_identity else None
                repo.upsert_credential(
                    user=db_user,
                    kind="jira",
                    value=user.jira_pat or "",
                    base_url=(user.jira_url or "").strip().rstrip("/"),
                    metadata=jira_metadata,
                )
                for kind in ("weak", "strong"):
                    cur = dict((user.ai or {}).get(kind) or {})
                    repo.upsert_credential(
                        user=db_user,
                        kind=kind,
                        value=cur.get("api_key", ""),
                        provider=cur.get("provider") or "anthropic",
                        base_url=(cur.get("base_url") or "").strip(),
                        model=(cur.get("model") or "").strip(),
                    )
            return True
        except Exception:
            return False

    def all(self) -> list[User]:
        if self._db_enabled():
            db_users = self._db_all()
            if db_users is not None:
                by_name = {u.username: u for u in db_users}
                for user in self._json_all():
                    by_name.setdefault(user.username, user)
                return list(by_name.values())
        return self._json_all()

    def get(self, username: str) -> Optional[User]:
        if self._db_enabled():
            user = self._db_get(username)
            if user is not None:
                return user
        for u in self._json_all():
            if u.username == username:
                return u
        return None

    def _persistable(self, user: User) -> dict:
        data = dict(user.__dict__)
        data["jira_pat"] = _encrypt_secret(data.get("jira_pat", ""))
        data["ai"] = _encrypt_ai(data.get("ai") or {})
        return data

    def upsert(self, user: User) -> None:
        if self._db_enabled() and self._db_upsert(user):
            return
        data = self._load_raw()
        users = data.setdefault("users", [])
        stored = self._persistable(user)
        for i, u in enumerate(users):
            if u.get("username") == user.username:
                users[i] = stored
                break
        else:
            users.append(stored)
        self._save_raw(data)

    def set_password(self, username: str, password: str) -> bool:
        u = self.get(username)
        if not u:
            return False
        u.salt, u.pwd_hash = hash_password(password)
        self.upsert(u)
        return True

    def set_jira_pat(self, username: str, pat: str) -> bool:
        u = self.get(username)
        if not u:
            return False
        u.jira_pat = pat or ""
        self.upsert(u)
        return True

    def set_jira_url(self, username: str, url: str) -> bool:
        u = self.get(username)
        if not u:
            return False
        u.jira_url = (url or "").strip().rstrip("/")
        self.upsert(u)
        return True

    def _db_set_jira_identity(self, username: str, display_name: str, base_url: str) -> dict | None:
        engine = _db_engine()
        cipher = _db_cipher()
        if engine is None or cipher is None:
            return None
        try:
            from sqlalchemy import select
            from core.store import models
            from core.store.db import session_scope
            from core.store.repositories import CredentialRepository
            payload = _jira_identity_payload(display_name, base_url)
            with session_scope(engine) as session:
                db_user = session.scalar(select(models.User).where(models.User.username == username, models.User.active.is_(True)))
                if db_user is None:
                    return None
                repo = CredentialRepository(session, cipher)
                repo.upsert_credential(user=db_user, kind="jira", base_url=payload["base_url"], metadata={"identity": payload})
            return payload
        except Exception:
            return None

    def set_jira_identity(self, username: str, display_name: str, base_url: str) -> dict:
        payload = _jira_identity_payload(display_name, base_url)
        if self._db_enabled():
            stored = self._db_set_jira_identity(username, display_name, base_url)
            if stored is not None:
                return stored
        u = self.get(username)
        if not u:
            return payload
        u.jira_identity = payload
        self.upsert(u)
        return payload

    def secret(self, username: str, kind: str) -> str:
        """读取该用户某机密明文（jira|weak|strong），供服务端临时调用；永不回显到浏览器。"""
        u = self.get(username)
        if not u:
            return ""
        if kind == "jira":
            return u.jira_pat or ""
        if kind in ("weak", "strong"):
            return ((u.ai or {}).get(kind) or {}).get("api_key") or ""
        return ""

    def set_ai(self, username: str, kind: str, base_url: str, model: str,
               api_key: str | None = None, provider: str | None = None) -> bool:
        """设置该用户某模型(kind=weak|strong)的 AI 配置。api_key 留空=保留原 key(掩码写)。
        provider=anthropic|openai；非法值则保留原值（默认 anthropic）。"""
        u = self.get(username)
        if not u or kind not in ("weak", "strong"):
            return False
        cur = dict((u.ai or {}).get(kind) or {})
        cur["base_url"] = (base_url or "").strip()
        cur["model"] = (model or "").strip()
        if provider in ("anthropic", "openai"):
            cur["provider"] = provider
        if api_key and api_key.strip():
            cur["api_key"] = api_key.strip()
        u.ai = {**(u.ai or {}), kind: cur}
        self.upsert(u)
        return True

    def authenticate(self, username: str, password: str) -> Optional[User]:
        u = self.get(username)
        if u and u.pwd_hash and _verify(password, u.salt, u.pwd_hash):
            if self._db_enabled():
                self._db_upsert(u)
                self.touch_login(u.username)
            return u
        return None

    def touch_login(self, username: str) -> None:
        engine = _db_engine()
        if engine is None:
            return
        try:
            from sqlalchemy import select
            from core.store import models
            from core.store.db import session_scope
            with session_scope(engine) as session:
                db_user = session.scalar(select(models.User).where(models.User.username == username))
                if db_user is not None:
                    db_user.last_login_at = datetime.now(timezone.utc)
        except Exception:
            return

    def count(self) -> int:
        if self._db_enabled():
            engine = _db_engine()
            if engine is not None:
                try:
                    from sqlalchemy import func, select
                    from core.store import models
                    from core.store.db import session_scope
                    with session_scope(engine) as session:
                        return int(session.scalar(select(func.count()).select_from(models.User).where(models.User.active.is_(True))) or 0)
                except Exception:
                    pass
        return len(self._load_raw().get("users", []))


store = UserStore()


# ----------------------------- 会话（签名 cookie + 吊销兼容层） -----------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.secret_key(), salt="qa-session")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_revoked() -> dict:
    path = config.REVOKED_SESSIONS_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    now = int(time())
    return {str(k): int(v) for k, v in raw.items() if int(v or 0) > now}


def _save_revoked(data: dict) -> None:
    config._ensure_data_dir()
    tmp = config.REVOKED_SESSIONS_FILE.with_name(config.REVOKED_SESSIONS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, config.REVOKED_SESSIONS_FILE)


def revoke_session(token: str) -> None:
    if not token:
        return
    _db_revoke_session(token)
    data = _load_revoked()
    data[_token_hash(token)] = int(time()) + config.SESSION_MAX_AGE
    _save_revoked(data)


def session_revoked(token: str) -> bool:
    return bool(token and _token_hash(token) in _load_revoked())


def _db_record_session(username: str, token: str, *, user_agent: str = "", ip_address: str = "") -> None:
    engine = _db_engine()
    if engine is None:
        return
    try:
        from sqlalchemy import select
        from core.store import models
        from core.store.db import session_scope
        with session_scope(engine) as session:
            user = session.scalar(select(models.User).where(models.User.username == username, models.User.active.is_(True)))
            if user is None:
                return
            session.add(models.Session(
                user_id=user.id,
                token_hash=_token_hash(token),
                user_agent=user_agent[:2000],
                ip_address=ip_address[:128],
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=config.SESSION_MAX_AGE),
            ))
    except Exception:
        return


def _db_session_username(token: str) -> str | None:
    engine = _db_engine()
    if engine is None:
        return None
    try:
        from sqlalchemy import select
        from core.store import models
        from core.store.db import session_scope
        now = datetime.now(timezone.utc)
        with session_scope(engine) as session:
            row = session.scalar(
                select(models.Session)
                .join(models.User)
                .where(
                    models.Session.token_hash == _token_hash(token),
                    models.Session.revoked_at.is_(None),
                    models.Session.expires_at > now,
                    models.User.active.is_(True),
                )
            )
            return row.user.username if row else None
    except Exception:
        return None


def _db_revoke_session(token: str) -> None:
    engine = _db_engine()
    if engine is None:
        return
    try:
        from sqlalchemy import select
        from core.store import models
        from core.store.db import session_scope
        with session_scope(engine) as session:
            row = session.scalar(select(models.Session).where(models.Session.token_hash == _token_hash(token)))
            if row is not None and row.revoked_at is None:
                row.revoked_at = datetime.now(timezone.utc)
    except Exception:
        return


def issue_session(username: str, *, user_agent: str = "", ip_address: str = "") -> str:
    token = _serializer().dumps({"u": username})
    _db_record_session(username, token, user_agent=user_agent, ip_address=ip_address)
    return token


def read_session(token: str) -> Optional[str]:
    if not token or session_revoked(token):
        return None
    try:
        data = _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
        username = data.get("u")
    except (BadSignature, SignatureExpired, Exception):
        return None
    if _db_engine() is None:
        return username
    return _db_session_username(token)


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(cookie_token: str, supplied_token: str) -> bool:
    return bool(cookie_token and supplied_token and hmac.compare_digest(cookie_token, supplied_token))


# ----------------------------- 用户管理 CLI -----------------------------

def _cli() -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(description="qa-workflow webapp 用户管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("adduser", help="新增用户")
    a.add_argument("username")
    a.add_argument("--name", default="")
    a.add_argument("--role", default="测试工程师")
    a.add_argument("--password-env", default="", help="从环境变量读取密码，避免写入命令行历史")
    a.add_argument("--password-file", default="", help="从文件读取密码第一行，适合容器密钥挂载")

    p = sub.add_parser("passwd", help="设置/重置密码")
    p.add_argument("username")
    p.add_argument("--password-env", default="", help="从环境变量读取密码，避免写入命令行历史")
    p.add_argument("--password-file", default="", help="从文件读取密码第一行，适合容器密钥挂载")

    sp = sub.add_parser("setpat", help="设置该用户的 Jira PAT（不回显）")
    sp.add_argument("username")

    sub.add_parser("list", help="列出用户")

    args = ap.parse_args()

    def read_password_from_args() -> str | None:
        env_name = getattr(args, "password_env", "") or ""
        file_name = getattr(args, "password_file", "") or ""
        if env_name:
            value = os.environ.get(env_name)
            if not value:
                print(f"环境变量未设置或为空：{env_name}")
                return ""
            return value
        if file_name:
            path = Path(file_name)
            if not path.exists() or not path.is_file():
                print(f"密码文件不存在：{file_name}")
                return ""
            return path.read_text(encoding="utf-8").splitlines()[0].strip()
        return None

    if args.cmd == "list":
        for u in store.all():
            tag = "（已配 Jira PAT）" if u.jira_pat else ""
            print(f"- {u.username}  [{u.role}] {u.display_name} {tag}")
        return 0

    if args.cmd == "adduser":
        if store.get(args.username):
            print(f"用户已存在：{args.username}")
            return 1
        pw = read_password_from_args()
        if pw is None:
            pw = getpass.getpass("设置密码: ")
            if pw != getpass.getpass("再次确认: "):
                print("两次密码不一致")
                return 1
        if not pw:
            print("密码不能为空")
            return 1
        salt, h = hash_password(pw)
        store.upsert(User(username=args.username, display_name=args.name or args.username,
                          role=args.role, salt=salt, pwd_hash=h))
        print(f"已创建用户：{args.username}")
        return 0

    if args.cmd == "passwd":
        pw = read_password_from_args()
        if pw is None:
            pw = getpass.getpass("新密码: ")
            if pw != getpass.getpass("再次确认: "):
                print("两次密码不一致")
                return 1
        if not pw:
            print("密码不能为空")
            return 1
        print("已更新" if store.set_password(args.username, pw) else f"用户不存在：{args.username}")
        return 0

    if args.cmd == "setpat":
        pat = getpass.getpass("Jira PAT（粘贴后回车，不回显）: ")
        print("已更新 Jira PAT" if store.set_jira_pat(args.username, pat) else f"用户不存在：{args.username}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
