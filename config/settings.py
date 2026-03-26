"""
config/settings.py — Centralised configuration, loaded from .env
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
# print(f"Loaded environment variables from {BASE_DIR / '.env'}")
# exit(0)

# Ensure essential folders exist
for folder in ["data", "logs", "input", "output"]:
    (BASE_DIR / folder).mkdir(parents=True, exist_ok=True)

def _required(key: str) -> str:
    val = os.getenv(key, "")
    if not val:
        raise EnvironmentError(f"Required env variable '{key}' is not set.")
    return val


class BaseConfig:
    # ── Flask core
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")
    DEBUG = False
    TESTING = False

    # ── Session / cookies
    SESSION_COOKIE_HTTPONLY = os.getenv("SESSION_COOKIE_HTTPONLY", "true").lower() == "true"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 3600 * 24 * 7  # 7 days

    # ── CSRF
    WTF_CSRF_TIME_LIMIT = int(os.getenv("CSRF_TIME_LIMIT", "3600"))

    # ── Database
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/matrimony.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Google OAuth
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/google/callback")
    GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

    # ── File uploads
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50")) * 1024 * 1024
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "input"))
    OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", str(BASE_DIR / "output"))
    ALLOWED_EXTENSIONS = set(os.getenv("ALLOWED_EXTENSIONS", "pdf,docx,doc,txt,jpg,jpeg,png").split(","))

    # ── Storage backend
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")  # "local" | "s3"
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "")
    AWS_S3_REGION = os.getenv("AWS_S3_REGION", "us-east-1")

    # ── Groq AI
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    MAX_CHARS_PER_PAGE = int(os.getenv("MAX_CHARS_PER_PAGE", "5000"))
    REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "2.0"))
    DB_TABLE = os.getenv("DB_TABLE", "register")

    # ── Logging
    LOG_DIR = os.getenv("LOG_DIR", str(BASE_DIR / "logs"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    LOG_LEVEL = "WARNING"


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


def get_config():
    env = os.getenv("FLASK_ENV", "development")
    mapping = {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
        "testing": TestingConfig,
    }
    return mapping.get(env, DevelopmentConfig)


def load_config(api_key=None):
    """Legacy helper kept for backward compatibility with core/processor.py."""
    cfg = get_config()
    return {
        "api_key": api_key or cfg.GROQ_API_KEY,
        "model": cfg.GROQ_MODEL,
        "max_chars": cfg.MAX_CHARS_PER_PAGE,
        "output_dir": cfg.OUTPUT_FOLDER,
        "log_dir": cfg.LOG_DIR,
        "table_name": cfg.DB_TABLE,
        "request_delay": cfg.REQUEST_DELAY,
    }
