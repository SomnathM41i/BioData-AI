"""
models/database.py — SQLAlchemy models: User + Upload
"""
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
#  User  (Google OAuth support)
# ──────────────────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    google_id     = db.Column(db.String(128), unique=True, nullable=True, index=True)
    email         = db.Column(db.String(256), unique=True, nullable=False, index=True)
    name          = db.Column(db.String(256), nullable=False)
    profile_image = db.Column(db.Text, nullable=True)       # URL from Google
    is_verified   = db.Column(db.Boolean, default=False)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_login    = db.Column(db.DateTime(timezone=True), nullable=True)

    # Relationship
    uploads = db.relationship("Upload", backref="owner", lazy="dynamic",
                              cascade="all, delete-orphan")

    def touch_login(self):
        self.last_login = utcnow()

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "profile_image": self.profile_image,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<User {self.email}>"


# ──────────────────────────────────────────────────────────────────────────────
#  Upload  (one row per uploaded file)
# ──────────────────────────────────────────────────────────────────────────────
class Upload(db.Model):
    __tablename__ = "uploads"

    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # File metadata
    original_filename = db.Column(db.String(512), nullable=False)
    stored_filename   = db.Column(db.String(512), nullable=False)   # secure_filename'd
    file_type         = db.Column(db.String(16), nullable=False)    # "image"|"pdf"|"docx"|"txt"
    mime_type         = db.Column(db.String(128), nullable=True)
    file_size_bytes   = db.Column(db.Integer, nullable=True)
    file_path         = db.Column(db.Text, nullable=False)          # local path or S3 key

    # Processing
    job_id            = db.Column(db.String(64), nullable=True, index=True)
    status            = db.Column(db.String(32), default="pending")
    # status: pending | queued | running | paused | done | failed

    model_used        = db.Column(db.String(128), nullable=True)
    processed_output  = db.Column(db.Text, nullable=True)  # JSON blob of results
    profiles_count    = db.Column(db.Integer, default=0)
    error_message     = db.Column(db.Text, nullable=True)

    created_at        = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at        = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at      = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "original_filename": self.original_filename,
            "file_type": self.file_type,
            "file_size_bytes": self.file_size_bytes,
            "job_id": self.job_id,
            "status": self.status,
            "model_used": self.model_used,
            "profiles_count": self.profiles_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    def __repr__(self):
        return f"<Upload {self.original_filename} [{self.status}]>"
