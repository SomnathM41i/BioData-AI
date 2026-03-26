"""
auth/google_oauth.py — Google OAuth 2.0 via Authlib
Handles: redirect URL generation, token exchange, user-info fetch,
         upsert (create or update existing user).
"""
import logging
from flask import current_app, url_for
from authlib.integrations.flask_client import OAuth
from models.database import db, User

logger = logging.getLogger(__name__)

# Module-level OAuth object registered in create_app()
oauth = OAuth()


def init_oauth(app):
    """Register Google as an OAuth provider. Call once from create_app()."""
    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        server_metadata_url=app.config["GOOGLE_DISCOVERY_URL"],
        client_kwargs={
            "scope": "openid email profile",
            "prompt": "select_account",   # always show account picker
        },
    )
    logger.info("Google OAuth provider registered.")


def get_google_auth_url(redirect_uri: str | None = None) -> str:
    """Return the URL to redirect the user to for Google login."""
    redirect = redirect_uri or current_app.config["GOOGLE_REDIRECT_URI"]
    return oauth.google.authorize_redirect(redirect)


def handle_google_callback() -> User | None:
    """
    Exchange the auth code for tokens, fetch user info, then upsert the
    user in our database.  Returns the User ORM object or None on failure.
    """
    try:
        token = oauth.google.authorize_access_token()
    except Exception as exc:
        logger.error("OAuth token exchange failed: %s", exc)
        return None

    user_info = token.get("userinfo")
    if not user_info:
        # Fallback: fetch from userinfo endpoint
        try:
            resp = oauth.google.get("https://www.googleapis.com/oauth2/v3/userinfo")
            user_info = resp.json()
        except Exception as exc:
            logger.error("Failed to fetch Google user info: %s", exc)
            return None

    return _upsert_user(user_info)


def _upsert_user(user_info: dict) -> User:
    """
    Find user by google_id or email.
    • Exists  → update profile image + last_login
    • New     → create record
    Returns the saved User.
    """
    google_id     = user_info.get("sub")
    email         = user_info.get("email", "").lower().strip()
    name          = user_info.get("name", email.split("@")[0])
    picture       = user_info.get("picture", "")
    is_verified   = user_info.get("email_verified", False)

    # Try by google_id first, then fall back to email
    user = User.query.filter_by(google_id=google_id).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()

    if user:
        # Update fields that may have changed
        user.google_id     = google_id
        user.profile_image = picture
        user.is_verified   = is_verified
        user.touch_login()
        logger.info("Existing user logged in via Google: %s", email)
    else:
        user = User(
            google_id=google_id,
            email=email,
            name=name,
            profile_image=picture,
            is_verified=is_verified,
        )
        user.touch_login()
        db.session.add(user)
        logger.info("New user registered via Google: %s", email)

    db.session.commit()
    return user
