"""
templates.py – BIMPruef unified HTML template system

Drop-in replacements for the inline _base_styles(), _footer(), _build_page(),
and _render_error() helpers in main.py.

All visual logic lives in bimpruef.css.  This module only generates structural
HTML; it does not contain any inline styles.  Import and use instead of the
old helpers in main.py and in every router that generates HTMLResponse pages.
"""

import html as _html
from fastapi.responses import HTMLResponse

# ─── Google Fonts (Inter) ──────────────────────────────────────────────────
_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&'
    'family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">'
)

_CSS_LINK = '<link rel="stylesheet" href="/static/bimpruef.css">'


# ─── Navigation ───────────────────────────────────────────────────────────

def _nav(user: dict | None = None, active: str = "") -> str:
    """Render the top navigation bar.

    Args:
        user:   Optional user dict with 'email' key (from get_current_user_optional).
        active: Nav link key to mark active ('projects', 'docs', etc.).
    """
    links = [
        ("projects", "/", "Projekte"),
        ("docs", "/viewer/docs/", "Dokumentation"),
    ]

    links_html = "".join(
        f'<a href="{href}" class="bp-nav__link'
        f'{" bp-nav__link--active" if key == active else ""}">'
        f"{label}</a>"
        for key, href, label in links
    )

    user_html = ""
    if user:
        email = _html.escape(str(user.get("email", "")))
        initials = "".join(p[0].upper() for p in email.split("@")[0].split(".")[:2]) or "?"
        user_html = (
            f'<div class="bp-nav__user">'
            f'<div class="bp-nav__avatar" title="{email}">{initials}</div>'
            f'<span style="font-size:0.8125rem;color:rgba(255,255,255,0.6)">{email}</span>'
            f'<a href="/auth/logout" class="bp-btn bp-btn--ghost bp-btn--sm" '
            f'style="color:rgba(255,255,255,0.5);font-size:0.8125rem">Abmelden</a>'
            f"</div>"
        )

    return (
        f'<nav class="bp-nav">'
        f'<div class="bp-nav__inner">'
        f'<a href="/" class="bp-nav__logo">'
        f'<div class="bp-nav__logo-mark">BP</div>'
        f"BIMPruef"
        f"</a>"
        f'<div class="bp-nav__links">{links_html}</div>'
        f"{user_html}"
        f"</div>"
        f"</nav>"
    )


# ─── Footer ───────────────────────────────────────────────────────────────

def _footer() -> str:
    return (
        '<footer class="bp-footer">'
        '<div class="bp-footer__brand">'
        "BIMPruef Platform · <strong>Foad Amini</strong> · "
        '<a href="mailto:amini.foad@gmail.com" style="color:inherit">amini.foad@gmail.com</a>'
        "</div>"
        '<div class="bp-footer__links">'
        '<a href="/impressum">Impressum</a>'
        '<a href="/datenschutz">Datenschutz</a>'
        "</div>"
        "</footer>"
    )


# ─── Page skeleton ────────────────────────────────────────────────────────

def _head(title: str, extra_head: str = "") -> str:
    safe_title = _html.escape(title)
    return (
        f"<head>"
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title} – BIMPruef</title>"
        f"{_FONTS}"
        f"{_CSS_LINK}"
        f"{extra_head}"
        f"</head>"
    )


def build_page(
    title: str,
    body_html: str,
    *,
    user: dict | None = None,
    active_nav: str = "",
    container: bool = True,
    extra_head: str = "",
) -> HTMLResponse:
    """
    Build a full HTML page with nav, content, and footer.

    Args:
        title:       Page <title> and h1 context.
        body_html:   Inner HTML for the main content area.
        user:        Current user dict (for nav user display).
        active_nav:  Nav link to mark active.
        container:   Whether to wrap body in .bp-container.
        extra_head:  Additional HTML to inject into <head>.
    """
    wrap_open  = '<div class="bp-container">' if container else ""
    wrap_close = "</div>" if container else ""

    page = (
        f"<!doctype html>"
        f"<html lang='de'>"
        f"{_head(title, extra_head)}"
        f"<body>"
        f'<div class="bp-page">'
        f"{_nav(user=user, active=active_nav)}"
        f'<main class="bp-main">'
        f"{wrap_open}"
        f"{body_html}"
        f"{wrap_close}"
        f"</main>"
        f"{_footer()}"
        f"</div>"
        f"</body>"
        f"</html>"
    )
    return HTMLResponse(page)


def render_error(
    title: str,
    message: str,
    back_url: str = "/",
    back_label: str = "Zurück",
) -> HTMLResponse:
    """Render a clean error page."""
    safe_title   = _html.escape(title)
    safe_message = _html.escape(message)
    safe_back    = _html.escape(back_url)
    safe_label   = _html.escape(back_label)

    body = (
        f'<div class="bp-page-header">'
        f'<div class="bp-page-header__meta">'
        f'<h1 class="bp-page-header__title bp-text-danger">{safe_title}</h1>'
        f"</div>"
        f"</div>"
        f'<div class="bp-card bp-mb-lg">'
        f'<div class="bp-alert bp-alert--danger">'
        f'<svg class="bp-alert__icon" width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f'<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
        f'<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
        f'<div>'
        f'<div class="bp-alert__title">{safe_title}</div>'
        f'<p style="margin:4px 0 0;font-size:0.875rem">{safe_message}</p>'
        f"</div>"
        f"</div>"
        f"</div>"
        f'<a class="bp-btn bp-btn--secondary" href="{safe_back}">'
        f'<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f'<path d="M19 12H5M12 5l-7 7 7 7"/></svg>'
        f"{safe_label}"
        f"</a>"
    )
    return build_page(title, body)


# ─── Reusable HTML fragments ──────────────────────────────────────────────

def breadcrumb(*crumbs: tuple[str, str | None]) -> str:
    """
    Render a breadcrumb trail.

    Args:
        crumbs: Pairs of (label, href).  Last item is current (no link needed).

    Example::
        breadcrumb(("Projekte", "/"), ("Mein Projekt", "/projects/abc"), ("Modelle", None))
    """
    parts = []
    for i, (label, href) in enumerate(crumbs):
        safe = _html.escape(label)
        if href and i < len(crumbs) - 1:
            parts.append(f'<a href="{_html.escape(href)}">{safe}</a>')
            parts.append('<span class="bp-breadcrumb__sep">›</span>')
        else:
            parts.append(f'<span class="bp-breadcrumb__current">{safe}</span>')
    return f'<nav class="bp-breadcrumb" aria-label="Breadcrumb">{"".join(parts)}</nav>'


def page_header(
    title: str,
    subtitle: str = "",
    actions_html: str = "",
) -> str:
    """Render the standard page-header block."""
    title_html = f'<h1 class="bp-page-header__title">{_html.escape(title)}</h1>'
    subtitle_html = (
        f'<p class="bp-page-header__subtitle">{_html.escape(subtitle)}</p>'
        if subtitle else ""
    )
    actions = (
        f'<div class="bp-page-header__actions">{actions_html}</div>'
        if actions_html else ""
    )
    return (
        f'<div class="bp-page-header">'
        f'<div class="bp-page-header__meta">{title_html}{subtitle_html}</div>'
        f"{actions}"
        f"</div>"
    )


def stat_card(
    label: str,
    value: str | int,
    variant: str = "",
    delta: str = "",
) -> str:
    """Render a single stat card."""
    cls = f"bp-stat{f' bp-stat--{variant}' if variant else ''}"
    delta_html = f'<div class="bp-stat__delta">{_html.escape(str(delta))}</div>' if delta else ""
    return (
        f'<div class="{cls}">'
        f'<div class="bp-stat__label">{_html.escape(str(label))}</div>'
        f'<div class="bp-stat__value">{_html.escape(str(value))}</div>'
        f"{delta_html}"
        f"</div>"
    )


def badge(text: str, variant: str = "default") -> str:
    """Render an inline badge/tag."""
    return f'<span class="bp-badge bp-badge--{variant}">{_html.escape(text)}</span>'


def alert(
    message: str,
    variant: str = "info",
    title: str = "",
) -> str:
    """Render an alert/banner block."""
    icons = {
        "info":    '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
        "success": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
        "warning": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
        "danger":  '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
    }
    icon_path = icons.get(variant, icons["info"])
    title_html = f'<div class="bp-alert__title">{_html.escape(title)}</div>' if title else ""
    return (
        f'<div class="bp-alert bp-alert--{variant}">'
        f'<svg class="bp-alert__icon" width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f"{icon_path}</svg>"
        f"<div>{title_html}"
        f'<p style="margin:0;font-size:0.875rem">{_html.escape(message)}</p>'
        f"</div>"
        f"</div>"
    )


def empty_state(
    title: str,
    description: str = "",
    action_html: str = "",
    icon_svg: str = "",
) -> str:
    """Render an empty-state block (for empty tables, missing data, etc.)."""
    default_icon = (
        '<svg width="24" height="24" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/>'
        "</svg>"
    )
    return (
        f'<div class="bp-empty">'
        f'<div class="bp-empty__icon">{icon_svg or default_icon}</div>'
        f'<p class="bp-empty__title">{_html.escape(title)}</p>'
        + (f'<p class="bp-empty__desc">{_html.escape(description)}</p>' if description else "")
        + (f"<div>{action_html}</div>" if action_html else "")
        + "</div>"
    )


# ─── Legacy compatibility shims ───────────────────────────────────────────
# These wrap the new functions so existing call-sites in main.py can be
# replaced gradually without breaking the application.

def _base_styles() -> str:
    """Legacy shim: returns a <link> tag instead of inline styles."""
    return _CSS_LINK


def _build_page(title: str, body_html: str) -> HTMLResponse:
    """Legacy shim: wraps build_page() with no user context."""
    return build_page(title, body_html)


def _render_error(title: str, message: str) -> HTMLResponse:
    """Legacy shim: wraps render_error()."""
    return render_error(title, message)


def _footer_html() -> str:
    """Legacy shim: returns footer HTML string."""
    return _footer()
