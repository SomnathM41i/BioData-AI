"""
routes/auth.py — /auth/* endpoints
  GET  /auth/google          → redirect to Google consent screen
  GET  /auth/google/callback → OAuth callback, login + redirect
  GET  /auth/logout          → clear session, redirect home
  GET  /auth/me              → JSON: current user info (API use)
"""
import logging
from flask import Blueprint, redirect, url_for, flash, jsonify, request, session
from flask_login import login_user, logout_user, login_required, current_user
from auth.google_oauth import oauth, handle_google_callback

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/google")
def google_login():
    """Initiate Google OAuth flow."""
    from flask import current_app
    redirect_uri = current_app.config["GOOGLE_REDIRECT_URI"]
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/google/callback")
def google_callback():
    """Handle Google's redirect back; create/update user; start session."""
    user = handle_google_callback()

    if not user:
        flash("Google authentication failed. Please try again.", "error")
        return redirect(url_for("main.index"))

    if not user.is_active:
        flash("Your account has been deactivated.", "error")
        return redirect(url_for("main.index"))

    login_user(user, remember=True)
    logger.info("User %s authenticated via Google.", user.email)

    # Honour "next" param to send user where they were going
    next_page = request.args.get("next") or session.pop("next_url", None)
    if next_page and next_page.startswith("/"):   # SSRF guard: relative only
        return redirect(next_page)
    return redirect(url_for("main.dashboard"))


@auth_bp.route("/logout")
@login_required
def logout():
    logger.info("User %s logged out.", current_user.email)
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.index"))


@auth_bp.route("/me")
@login_required
def me():
    """JSON endpoint: return current user profile."""
    return jsonify(current_user.to_dict())
