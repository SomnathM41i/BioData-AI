"""
services/storage.py — File storage abstraction (local | S3)

Usage:
    storage = StorageService(app.config)
    file_path, stored_name = storage.save(file_obj, "pdf")
    storage.delete(file_path)
    url = storage.url(file_path)
"""
import os
import logging
import uuid
from datetime import datetime
from pathlib import Path
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

# Allowed MIME types per category
MIME_WHITELIST = {
    "image": {"image/jpeg", "image/png", "image/gif", "image/webp"},
    "pdf":   {"application/pdf"},
    "docx":  {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    },
    "txt":   {"text/plain"},
}

EXT_TO_CATEGORY = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image",
    "pdf": "pdf",
    "docx": "docx", "doc": "docx",
    "txt": "txt",
}

MAX_FILE_BYTES = {
    "image": 10 * 1024 * 1024,   # 10 MB
    "pdf":   50 * 1024 * 1024,   # 50 MB
    "docx":  20 * 1024 * 1024,   # 20 MB
    "txt":   5  * 1024 * 1024,   #  5 MB
}


class StorageError(Exception):
    pass


def detect_file_category(filename: str) -> str | None:
    """Return category string or None if unsupported."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return EXT_TO_CATEGORY.get(ext)


class StorageService:
    """
    Thin abstraction over local disk.
    Replace `_save_local` / `_delete_local` / `_url_local` with S3
    equivalents when STORAGE_BACKEND=s3.
    """

    def __init__(self, config: dict):
        self.backend      = config.get("STORAGE_BACKEND", "local")
        self.upload_dir   = Path(config.get("UPLOAD_FOLDER", "./input"))
        self.output_dir   = Path(config.get("OUTPUT_FOLDER", "./output"))
        self.s3_bucket    = config.get("AWS_S3_BUCKET", "")
        self.s3_region    = config.get("AWS_S3_REGION", "us-east-1")
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def validate(self, file: FileStorage) -> tuple[str, str]:
        """
        Validate file type and size.
        Returns (category, extension) on success; raises StorageError on failure.
        """
        filename = file.filename or ""
        if not filename or "." not in filename:
            raise StorageError("File has no extension.")

        ext = filename.rsplit(".", 1)[-1].lower()
        category = EXT_TO_CATEGORY.get(ext)
        if not category:
            raise StorageError(f"File type '.{ext}' is not allowed.")

        # Read first 512 bytes to get size without loading entire file
        file.stream.seek(0, 2)          # seek to end
        size = file.stream.tell()
        file.stream.seek(0)             # rewind

        max_bytes = MAX_FILE_BYTES.get(category, 10 * 1024 * 1024)
        if size > max_bytes:
            mb = max_bytes // (1024 * 1024)
            raise StorageError(f"File too large. Max {mb} MB for {category} files.")

        return category, ext

    def save(self, file: FileStorage) -> dict:
        """
        Validate, then persist the file.
        Returns metadata dict:
          { category, extension, original_filename, stored_filename,
            file_path, file_size_bytes }
        """
        category, ext = self.validate(file)

        original_name  = secure_filename(file.filename)
        unique_id      = uuid.uuid4().hex[:12]
        timestamp      = datetime.now().strftime("%Y%m%d_%H%M%S")
        stored_name    = f"{timestamp}_{unique_id}.{ext}"

        # Organise into sub-folders by category
        dest_dir = self.upload_dir / category
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / stored_name

        if self.backend == "s3":
            file_path = self._save_s3(file, f"uploads/{category}/{stored_name}")
        else:
            file_path = self._save_local(file, dest_path)

        file.stream.seek(0, 2)
        size = file.stream.tell()
        file.stream.seek(0)

        logger.info("Saved %s → %s (%d bytes)", original_name, file_path, size)

        return {
            "category":          category,
            "extension":         ext,
            "original_filename": original_name,
            "stored_filename":   stored_name,
            "file_path":         str(file_path),
            "file_size_bytes":   size,
        }

    def delete(self, file_path: str):
        if self.backend == "s3":
            self._delete_s3(file_path)
        else:
            self._delete_local(file_path)

    def url(self, file_path: str) -> str:
        if self.backend == "s3":
            return f"https://{self.s3_bucket}.s3.{self.s3_region}.amazonaws.com/{file_path}"
        return f"/uploads/{Path(file_path).name}"

    # ── Local backend ─────────────────────────────────────────────────────────

    def _save_local(self, file: FileStorage, dest: Path) -> Path:
        file.save(dest)
        return dest

    def _delete_local(self, file_path: str):
        p = Path(file_path)
        if p.exists():
            p.unlink()
            logger.info("Deleted local file: %s", file_path)

    # ── S3 backend (stub — wire up when ready) ────────────────────────────────

    def _save_s3(self, file: FileStorage, s3_key: str) -> str:
        try:
            import boto3
            s3 = boto3.client("s3")
            s3.upload_fileobj(file.stream, self.s3_bucket, s3_key)
            return s3_key
        except Exception as exc:
            raise StorageError(f"S3 upload failed: {exc}") from exc

    def _delete_s3(self, s3_key: str):
        try:
            import boto3
            s3 = boto3.client("s3")
            s3.delete_object(Bucket=self.s3_bucket, Key=s3_key)
        except Exception as exc:
            logger.error("S3 delete failed: %s", exc)
