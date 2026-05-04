"""
auth.py – BIMPruef authentication module

Features:
  - Email/password sign-up and sign-in
  - PBKDF2 password hashing with per-user salt
  - Signed HTTP-only session cookie
  - File-based user storage under uploads/accounts/users.json
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import uuid
from email.utils import parseaddr
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.storage import UPLOADS_DIR

AUTH_COOKIE_NAME = "bimpruef_auth"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("AUTH_SESSION_MAX_AGE_SECONDS", str(60 * 60 * 12)))
PASSWORD_ITERATIONS = int(os.environ.get("AUTH_PASSWORD_ITERATIONS", "260000"))
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "dev-change-this-secret-key")

USERS_FILE = os.path.join(UPLOADS_DIR, "accounts", "users.json")
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


auth_router = APIRouter(prefix="/auth")


def _ensure_user_store() -> None:
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def _load_users() -> list[dict]:
    _ensure_user_store()
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_users(users: list[dict]) -> None:
    _ensure_user_store()
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def validate_email(email: str) -> Optional[str]:
    email = normalize_email(email)
    parsed_name, parsed_addr = parseaddr(email)
    if parsed_addr != email or parsed_name:
        return "Please enter a valid email address."
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return "Please enter a valid email address."
    local, domain = email.rsplit("@", 1)
    if len(local) > 64 or len(domain) > 253:
        return "Please enter a valid email address."
    return None


def validate_password(password: str) -> Optional[str]:
    password = password or ""
    if len(password) < 10:
        return "Password must contain at least 10 characters."
    checks = [
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ]
    if sum(checks) < 3:
        return "Password must include at least three of: lowercase, uppercase, number, special character."
    return None


def _hash_password(password: str, salt_b64: Optional[str] = None) -> tuple[str, str]:
    if salt_b64:
        salt = base64.b64decode(salt_b64.encode("ascii"))
    else:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def _verify_password(password: str, user: dict) -> bool:
    salt = user.get("password_salt", "")
    expected = user.get("password_hash", "")
    if not salt or not expected:
        return False
    _, actual = _hash_password(password, salt)
    return hmac.compare_digest(actual, expected)


def get_user_by_email(email: str) -> Optional[dict]:
    email = normalize_email(email)
    for user in _load_users():
        if user.get("email") == email:
            return user
    return None


def get_user_by_id(user_id: str) -> Optional[dict]:
    for user in _load_users():
        if user.get("user_id") == user_id:
            return user
    return None


def create_user(email: str, password: str) -> dict:
    email = normalize_email(email)
    users = _load_users()
    if any(u.get("email") == email for u in users):
        raise ValueError("An account with this email already exists.")
    salt, password_hash = _hash_password(password)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    user = {
        "user_id": uuid.uuid4().hex,
        "email": email,
        "password_salt": salt,
        "password_hash": password_hash,
        "created_at": now,
    }
    users.append(user)
    _save_users(users)
    return user


def _sign(value: str) -> str:
    return hmac.new(AUTH_SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(user_id: str) -> str:
    issued = str(int(time.time()))
    nonce = secrets.token_urlsafe(12)
    payload = f"{user_id}.{issued}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def read_session_token(token: str) -> Optional[dict]:
    parts = str(token or "").split(".")
    if len(parts) != 4:
        return None
    user_id, issued, nonce, signature = parts
    payload = f"{user_id}.{issued}.{nonce}"
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        issued_ts = int(issued)
    except ValueError:
        return None
    if time.time() - issued_ts > SESSION_MAX_AGE_SECONDS:
        return None
    return get_user_by_id(user_id)


def get_current_user_optional(request: Request) -> Optional[dict]:
    cached = getattr(request.state, "user", None)
    if cached:
        return cached
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    user = read_session_token(token)
    request.state.user = user
    return user


def require_user(request: Request) -> dict:
    user = get_current_user_optional(request)
    if not user:
        raise PermissionError("Authentication required.")
    return user


def _e(value) -> str:
    return html.escape(str(value or ""))


def _auth_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0e0e1a;--surface:#16213e;--surface2:#1a2a4a;--border:#1e3a6e;--accent:#4fc3f7;--accent2:#e94560;--text:#d0dce8;--muted:#8aa0bd;--success:#4caf50}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:radial-gradient(circle at top,#17244a 0,#0e0e1a 52%);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;line-height:1.5}}
.card{{width:100%;max-width:430px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px;box-shadow:0 20px 80px rgba(0,0,0,.25)}}
h1{{font-size:24px;font-weight:600;margin-bottom:6px}}p{{color:var(--muted);font-size:13px;margin-bottom:18px}}
label{{display:block;font-size:12px;color:var(--muted);margin:14px 0 5px}}
input{{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:7px;font-size:14px;outline:none}}
input:focus{{border-color:var(--accent)}}
button,.btn{{width:100%;padding:10px 14px;margin-top:18px;border-radius:7px;border:1px solid var(--accent);background:var(--accent);color:#0a1a2e;font-weight:700;cursor:pointer;text-align:center;text-decoration:none;display:block}}
.link{{color:var(--accent);text-decoration:none}}.link:hover{{text-decoration:underline}}
.flash-err{{background:#2a0a10;border:1px solid var(--accent2);border-radius:8px;padding:10px 12px;color:#ffaaaa;font-size:13px;margin:0 0 14px}}
.small{{font-size:12px;color:var(--muted);margin-top:16px;text-align:center}}
</style>
</head><body>{body}</body></html>""")


def _login_form(error: str = "", email: str = "") -> HTMLResponse:
    err = f'<div class="flash-err">{_e(error)}</div>' if error else ""
    return _auth_page("Login – BIMPruef", f"""
<div class="card">
  <h1>BIMPruef Login</h1>
  <p>Sign in with your email and password to access your projects.</p>
  {err}
  <form method="POST" action="/auth/login" autocomplete="on">
    <label>Email</label>
    <input type="email" name="email" value="{_e(email)}" required autocomplete="email">
    <label>Password</label>
    <input type="password" name="password" required autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
  <div class="small">No account yet? <a class="link" href="/auth/signup">Create account</a></div>
</div>""")


def _signup_form(error: str = "", email: str = "") -> HTMLResponse:
    err = f'<div class="flash-err">{_e(error)}</div>' if error else ""
    return _auth_page("Create account – BIMPruef", f"""
<div class="card">
  <h1>Create account</h1>
  <p>Use a valid email address and a strong password.</p>
  {err}
  <form method="POST" action="/auth/signup" autocomplete="on">
    <label>Email</label>
    <input type="email" name="email" value="{_e(email)}" required autocomplete="email">
    <label>Password</label>
    <input type="password" name="password" required autocomplete="new-password">
    <button type="submit">Create account</button>
  </form>
  <div class="small">Already registered? <a class="link" href="/auth/login">Sign in</a></div>
</div>""")


@auth_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if get_current_user_optional(request):
        return RedirectResponse("/", status_code=302)
    return _login_form(error)


@auth_router.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    email = normalize_email(email)
    user = get_user_by_email(email)
    if not user or not _verify_password(password, user):
        return _login_form("Invalid email or password.", email)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_token(user["user_id"]),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=os.environ.get("AUTH_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"},
        samesite="lax",
        path="/",
    )
    return response


@auth_router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, error: str = ""):
    if get_current_user_optional(request):
        return RedirectResponse("/", status_code=302)
    return _signup_form(error)


@auth_router.post("/signup")
async def signup_post(email: str = Form(...), password: str = Form(...)):
    email = normalize_email(email)
    email_error = validate_email(email)
    if email_error:
        return _signup_form(email_error, email)
    password_error = validate_password(password)
    if password_error:
        return _signup_form(password_error, email)
    try:
        user = create_user(email, password)
    except ValueError as exc:
        return _signup_form(str(exc), email)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_token(user["user_id"]),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=os.environ.get("AUTH_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"},
        samesite="lax",
        path="/",
    )
    return response


@auth_router.post("/logout")
async def logout_post():
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response
