"""Strava OAuth + session middleware.

Routes provided:
  GET  /login              kicks off the OAuth dance (redirects to Strava)
  GET  /oauth/callback     handles the redirect back from Strava
  POST /logout             clears the session

Helpers:
  init_app(app)            registers routes + before_request middleware
  current_user             reads g.user (or None if not signed in)
  login_required(view)     decorator that 302s to /login when not signed in
"""
from __future__ import annotations

import os
import secrets
import time
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template,
    request, session, url_for,
)

import storage

SESSION_COOKIE_NAME = "coach_sid"
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"

# Scopes we need from Strava. Note: activity:read_all is essential to see
# private rides; profile:read_all gives us HR/power zones + weight.
STRAVA_SCOPES = "read,activity:read_all,profile:read_all"

bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------
#                          MIDDLEWARE
# ---------------------------------------------------------------------

def _load_user_for_request():
    """before_request hook: attach g.user from the session cookie (if any)."""
    g.user = None
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return
    conn = storage.connect()
    try:
        user = storage.lookup_session(conn, sid)
        g.user = user
    finally:
        conn.close()


def current_user():
    return getattr(g, "user", None)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            # remember where they were headed so we can come back after login
            session["next"] = request.path
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapper


def data_required(view):
    """login_required + redirect to /backfilling if the user's data isn't ready.

    Use this on any page that reads activities/streams. The user can still
    navigate to login/logout/backfilling-status without being bounced.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        u = current_user()
        if u is None:
            session["next"] = request.path
            return redirect(url_for("auth.login"))
        if u.backfill_state in ("pending", "running", "failed"):
            return redirect(url_for("backfilling"))
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------
#                          ROUTES
# ---------------------------------------------------------------------

@bp.route("/login")
def login():
    """Render a small landing page with a Sign in with Strava button.

    The button form-POSTs to `/login/start` so the OAuth redirect always
    originates from a user gesture (cleaner CSRF posture).
    """
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@bp.route("/login/start", methods=["GET", "POST"])
def login_start():
    """Generate state token, store in session, redirect to Strava."""
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    if not client_id:
        abort(500, "STRAVA_CLIENT_ID is not configured on the server.")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    redirect_uri = url_for("auth.oauth_callback", _external=True)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": STRAVA_SCOPES,
        "state": state,
    }
    return redirect(f"{STRAVA_AUTH_URL}?{urlencode(params)}")


@bp.route("/oauth/callback")
def oauth_callback():
    err = request.args.get("error")
    if err:
        return render_template("login_error.html",
                               message=f"Strava 拒绝了授权请求:{err}"), 400

    state = request.args.get("state", "")
    expected = session.pop("oauth_state", None)
    if not state or state != expected:
        return render_template("login_error.html",
                               message="状态校验失败,请重新尝试登录。"), 400

    code = request.args.get("code")
    if not code:
        return render_template("login_error.html",
                               message="未收到授权 code。"), 400

    granted = (request.args.get("scope") or "").split(",")
    if "activity:read_all" not in granted:
        return render_template("login_error.html",
            message="请勾选\"查看私密活动\"权限,否则无法读取你的完整骑行数据。"), 400

    client_id = os.environ["STRAVA_CLIENT_ID"].strip()
    client_secret = os.environ["STRAVA_CLIENT_SECRET"].strip()
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        return render_template("login_error.html",
            message=f"Strava 拒绝了 token 交换 ({resp.status_code})"), 400
    body = resp.json()
    athlete = body.get("athlete") or {}
    if not athlete.get("id"):
        return render_template("login_error.html",
            message="Strava 未返回用户信息。"), 400

    conn = storage.connect()
    try:
        user = storage.upsert_user_from_oauth(
            conn, athlete,
            refresh_token=body["refresh_token"],
            access_token=body["access_token"],
            access_token_expires=int(body["expires_at"]),
        )
        sid = storage.create_session(conn, user.id)
        kick_backfill = user.backfill_state == "pending"
    finally:
        conn.close()

    # Brand-new user → kick off a background full-history backfill. The
    # `backfilling` page will poll for progress and redirect to the dashboard
    # once it's done. Failed-state users retry via an explicit button.
    if kick_backfill:
        import worker
        worker.submit_backfill(user.id)
        next_path = url_for("backfilling")
    else:
        next_path = session.pop("next", None) or url_for("dashboard")
    response = redirect(next_path)
    response.set_cookie(
        SESSION_COOKIE_NAME, sid,
        max_age=storage.SESSION_LIFETIME_S,
        httponly=True, samesite="Lax",
        secure=current_app.config.get("SESSION_COOKIE_SECURE", False),
    )
    return response


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        conn = storage.connect()
        try:
            storage.destroy_session(conn, sid)
        finally:
            conn.close()
    response = redirect(url_for("auth.login"))
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------
#                      APP INTEGRATION
# ---------------------------------------------------------------------

def init_app(app) -> None:
    """Register the blueprint and the before_request hook."""
    app.before_request(_load_user_for_request)
    app.register_blueprint(bp)
    # Make current_user() available in all templates
    app.jinja_env.globals["current_user"] = current_user
