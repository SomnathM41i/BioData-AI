"""
app.py — Flask application factory
Run:
  Development : python app.py
  Production  : gunicorn -w 4 -b 0.0.0.0:5000 "app:create_app()"
"""
import os
import logging
from flask import Flask, jsonify, render_template
from flask_login import LoginManager
from flask_migrate import Migrate

from config.settings import get_config
from models.database import db, User
from auth.google_oauth import init_oauth
from middleware.security import init_security
from core.logger import setup_logging

logger = logging.getLogger(__name__)


def create_app(config_class=None):
    app = Flask(__name__)

    # ── Load config ──────────────────────────────────────────────────────────
    cfg = config_class or get_config()
    app.config.from_object(cfg)

    # ── Logging ──────────────────────────────────────────────────────────────
    setup_logging(app.config["LOG_DIR"], app.config["LOG_LEVEL"])
    logger.info("Starting Matrimony AI Agent [env=%s]", os.getenv("FLASK_ENV", "development"))

    # ── Extensions ───────────────────────────────────────────────────────────
    db.init_app(app)
    Migrate(app, db)
    init_oauth(app)
    init_security(app)

    # ── Flask-Login ──────────────────────────────────────────────────────────
    login_manager = LoginManager(app)
    login_manager.login_view       = "main.login"
    login_manager.login_message    = "Please sign in to continue."
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.query.get(int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        # Return JSON for API requests, redirect for browser requests
        from flask import request, redirect, url_for
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("main.login", next=request.path))

    # ── Blueprints ───────────────────────────────────────────────────────────
    from routes.auth import auth_bp
    from routes.api  import api_bp
    from routes.main import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(main_bp)

    # ── Error handlers ────────────────────────────────────────────────────────
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": "Bad request", "detail": str(e)}), 400

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify({"error": "Forbidden"}), 403

    @app.errorhandler(404)
    def not_found(e):
        from flask import request
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def request_entity_too_large(e):
        mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return jsonify({"error": f"File too large. Maximum size is {mb} MB."}), 413

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception("Unhandled 500 error")
        return jsonify({"error": "Internal server error"}), 500

    # ── DB init ───────────────────────────────────────────────────────────────
    with app.app_context():
        os.makedirs(app.config["UPLOAD_FOLDER"],  exist_ok=True)
        os.makedirs(app.config["OUTPUT_FOLDER"],   exist_ok=True)
        os.makedirs(app.config["LOG_DIR"],          exist_ok=True)
        os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)
        db.create_all()
        logger.info("Database tables verified/created.")

    return app


# ── Dev entrypoint ────────────────────────────────────────────────────────────
app = create_app()

if __name__ == "__main__":
    print("\n🚀  Matrimony AI Agent  →  http://localhost:5000\n")
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_ENV") == "development",
    )
