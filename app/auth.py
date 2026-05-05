"""
auth.py – BIMPruef authentication module
"""

import hashlib
import hmac
import html
import os
import re
import secrets
import time
import uuid
from email.utils import parseaddr
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from passlib.context import CryptContext
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, init_db
from app.models import User

init_db()

AUTH_COOKIE_NAME = "bimpruef_auth"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("AUTH_SESSION_MAX_AGE_SECONDS", str(60 * 60 * 12)))
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "dev-change-this-secret-key")
SIGNUP_INVITE_CODE = os.environ.get("SIGNUP_INVITE_CODE", "16880")

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

pwd_context = CryptContext(
    schemes=["bcrypt_sha256"],
    deprecated="auto",
)

auth_router = APIRouter(prefix="/auth")


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

    if len(password) < 6:
        return "Password must contain at least 6 characters."

    checks = [
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ]

    if sum(checks) < 3:
        return (
            "Password must include at least three of: "
            "lowercase, uppercase, number, special character."
        )

    return None


def validate_invite_code(invite_code: str) -> Optional[str]:
    if str(invite_code or "").strip() != SIGNUP_INVITE_CODE:
        return "Invalid invitation code."

    return None


def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False

    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def _user_to_dict(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else "",
    }


def get_user_by_email(email: str) -> Optional[dict]:
    email = normalize_email(email)

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        return _user_to_dict(user) if user else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    user_id = str(user_id or "").strip()

    if not user_id:
        return None

    with SessionLocal() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        return _user_to_dict(user) if user else None


def create_user(email: str, password: str) -> dict:
    email = normalize_email(email)

    with SessionLocal() as db:
        existing = db.query(User).filter(User.email == email).first()

        if existing:
            raise ValueError("An account with this email already exists.")

        user = User(
            user_id=uuid.uuid4().hex,
            email=email,
            password_hash=_hash_password(password),
        )

        db.add(user)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ValueError("An account with this email already exists.")

        db.refresh(user)
        return _user_to_dict(user)


def authenticate_user(email: str, password: str) -> Optional[dict]:
    email = normalize_email(email)

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()

        if not user:
            return None

        if not _verify_password(password, user.password_hash):
            return None

        return _user_to_dict(user)


def _sign(value: str) -> str:
    return hmac.new(
        AUTH_SECRET_KEY.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session_token(user_id: str) -> str:
    issued = str(int(time.time()))
    nonce = secrets.token_urlsafe(12)
    payload = f"{user_id}.{issued}.{nonce}"
    signature = _sign(payload)
    return f"{payload}.{signature}"


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
:root{{
  --bg:#0e0e1a;
  --surface:#16213e;
  --surface2:#1a2a4a;
  --border:#1e3a6e;
  --accent:#4fc3f7;
  --accent2:#e94560;
  --text:#d0dce8;
  --muted:#8aa0bd;
}}
body{{
  font-family:'Segoe UI',system-ui,sans-serif;
  background:radial-gradient(circle at top,#17244a 0,#0e0e1a 52%);
  color:var(--text);
  min-height:100vh;
  display:flex;
  align-items:center;
  justify-content:center;
  padding:24px;
  line-height:1.5;
}}
.card{{
  width:100%;
  max-width:430px;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:14px;
  padding:28px;
  box-shadow:0 20px 80px rgba(0,0,0,.25);
}}
h1{{font-size:24px;font-weight:600;margin-bottom:6px}}
p{{color:var(--muted);font-size:13px;margin-bottom:18px}}
label{{display:block;font-size:12px;color:var(--muted);margin:14px 0 5px}}
input{{
  width:100%;
  background:var(--surface2);
  border:1px solid var(--border);
  color:var(--text);
  padding:10px 12px;
  border-radius:7px;
  font-size:14px;
  outline:none;
}}
input:focus{{border-color:var(--accent)}}
.password-wrap{{position:relative}}
.password-wrap input{{padding-right:74px}}
.show-password-btn{{
  position:absolute;
  right:8px;
  top:50%;
  transform:translateY(-50%);
  width:auto;
  margin:0;
  padding:6px 10px;
  border-radius:6px;
  border:1px solid var(--border);
  background:#223a5e;
  color:var(--text);
  font-size:12px;
  font-weight:600;
  cursor:pointer;
}}
button.main-btn,.btn{{
  width:100%;
  padding:10px 14px;
  margin-top:18px;
  border-radius:7px;
  border:1px solid var(--accent);
  background:var(--accent);
  color:#0a1a2e;
  font-weight:700;
  cursor:pointer;
  text-align:center;
  text-decoration:none;
  display:block;
}}
.link{{color:var(--accent);text-decoration:none}}
.link:hover{{text-decoration:underline}}
.flash-err{{
  background:#2a0a10;
  border:1px solid var(--accent2);
  border-radius:8px;
  padding:10px 12px;
  color:#ffaaaa;
  font-size:13px;
  margin:0 0 14px;
}}
.small{{
  font-size:12px;
  color:var(--muted);
  margin-top:16px;
  text-align:center;
}}
.hint{{
  font-size:11px;
  color:var(--muted);
  margin-top:8px;
}}
</style>
<script>
function togglePassword(id, btnId) {{
  const input = document.getElementById(id);
  const btn = document.getElementById(btnId);

  if (!input || !btn) return;

  if (input.type === "password") {{
    input.type = "text";
    btn.textContent = "Hide";
  }} else {{
    input.type = "password";
    btn.textContent = "Show";
  }}
}}
</script>
</head>
<body>
{body}
</body>
</html>""")


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
    <div class="password-wrap">
      <input id="login-password" type="password" name="password" required autocomplete="current-password">
      <button id="login-password-btn" class="show-password-btn" type="button" onclick="togglePassword('login-password','login-password-btn')">Show</button>
    </div>

    <button class="main-btn" type="submit">Sign in</button>
  </form>

  <div class="small">
    No account yet? <a class="link" href="/auth/signup">Create account</a>
  </div>
</div>""")


def _signup_form(error: str = "", email: str = "", invite_code: str = "") -> HTMLResponse:
    err = f'<div class="flash-err">{_e(error)}</div>' if error else ""

    return _auth_page("Create account – BIMPruef", f"""
<div class="card">
  <h1>Create account</h1>
  <p>Use a valid email address, the invitation code, and a strong password.</p>
  {err}
  <form method="POST" action="/auth/signup" autocomplete="on">
    <label>Invitation code</label>
    <input type="text" name="invite_code" value="{_e(invite_code)}" required autocomplete="off">

    <label>Email</label>
    <input type="email" name="email" value="{_e(email)}" required autocomplete="email">

    <label>Password</label>
    <div class="password-wrap">
      <input id="signup-password" type="password" name="password" required autocomplete="new-password">
      <button id="signup-password-btn" class="show-password-btn" type="button" onclick="togglePassword('signup-password','signup-password-btn')">Show</button>
    </div>

    <div class="hint">
      Password must contain at least 6 characters and include at least three of:
      lowercase, uppercase, number, special character.
    </div>

    <button class="main-btn" type="submit">Create account</button>
  </form>

  <div class="small">
    Already have an account? <a class="link" href="/auth/login">Sign in</a>
  </div>
</div>""")


@auth_router.get("/login")
def login_page() -> HTMLResponse:
    return _login_form()


@auth_router.post("/login")
def login_post(
    email: str = Form(...),
    password: str = Form(...),
):
    email = normalize_email(email)
    user = authenticate_user(email, password)

    if not user:
        return _login_form("Invalid email or password.", email=email)

    token = create_session_token(user["user_id"])

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"},
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )

    return response


@auth_router.get("/signup")
def signup_page() -> HTMLResponse:
    return _signup_form()


@auth_router.post("/signup")
def signup_post(
    invite_code: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    email = normalize_email(email)
    invite_code = str(invite_code or "").strip()

    invite_error = validate_invite_code(invite_code)
    if invite_error:
        return _signup_form(invite_error, email=email, invite_code=invite_code)

    email_error = validate_email(email)
    if email_error:
        return _signup_form(email_error, email=email, invite_code=invite_code)

    password_error = validate_password(password)
    if password_error:
        return _signup_form(password_error, email=email, invite_code=invite_code)

    try:
        user = create_user(email, password)
    except ValueError as exc:
        return _signup_form(str(exc), email=email, invite_code=invite_code)
    except Exception as exc:
        return _signup_form(f"Account could not be created. {exc}", email=email, invite_code=invite_code)

    token = create_session_token(user["user_id"])

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"},
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )

    return response


@auth_router.post("/logout")
def logout_post():
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@auth_router.get("/logout")
def logout_get():
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response
