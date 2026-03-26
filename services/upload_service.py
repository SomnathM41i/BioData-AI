"""
services/upload_service.py — Orchestrates the full processing pipeline

Flow:
  receive file → validate → store → create DB record → queue job →
  extract pages (via model_router) → run LLM extraction → save results → update DB
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from models.database import db, Upload
from services.storage import StorageService, StorageError
from services.model_router import model_router
from core.extractor import build_llm, extract_profile, is_valid_profile, FALLBACK_MODELS
from core.sql_generator import to_sql_insert, sql_file_header
from core.logger import make_log_entry

logger = logging.getLogger(__name__)

# In-memory job store (replace with Redis for multi-worker production)
jobs: dict[str, dict] = {}
chat_histories: dict[str, list] = {}


def _log(job: dict, level: str, msg: str):
    entry = make_log_entry(level, msg)
    job["logs"].append(entry)
    logger.info("[%s] %s | %s", entry["time"], level, msg)


# ──────────────────────────────────────────────────────────────────────────────
#  Upload Service
# ──────────────────────────────────────────────────────────────────────────────
class UploadService:

    def __init__(self, config: dict):
        self.config  = config
        self.storage = StorageService(config)

    # ── Save file + create DB record + kick off background job ────────────────

    def handle_upload(self, file_obj, user_id: int, api_key: str,
                      model: str | None = None, delay: float = 2.0) -> dict:
        """
        Main entry point called from the upload route.
        Returns: { job_id, upload_id, file_type }
        Raises: StorageError on validation failure.
        """
        # 1. Validate & persist file
        meta = self.storage.save(file_obj)

        # 2. Persist to DB
        upload = Upload(
            user_id=user_id,
            original_filename=meta["original_filename"],
            stored_filename=meta["stored_filename"],
            file_type=meta["category"],
            file_path=meta["file_path"],
            file_size_bytes=meta["file_size_bytes"],
            status="queued",
            model_used=model or self.config.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        )
        db.session.add(upload)
        db.session.commit()

        # 3. Build job record
        job_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        job = {
            "status":        "queued",
            "file":          meta["original_filename"],
            "file_type":     meta["category"],
            "upload_id":     upload.id,
            "logs":          [],
            "profiles":      [],
            "sql_file":      None,
            "json_file":     None,
            "total_pages":   0,
            "processed":     0,
            "success":       0,
            "started_at":    datetime.now().isoformat(),
            "current_model": model or self.config.get("GROQ_MODEL"),
            "pause_reason":  None,
        }
        jobs[job_id] = job
        chat_histories[job_id] = []

        # Update DB with job_id
        upload.job_id = job_id
        db.session.commit()

        # 4. Start background thread (use Celery/RQ in production)
        cfg = {**self.config, "api_key": api_key,
               "model": model or self.config.get("GROQ_MODEL"),
               "request_delay": delay,
               "output_dir": self.config.get("OUTPUT_FOLDER", "./output")}

        threading.Thread(
            target=self._process_async,
            args=(meta["file_path"], meta["category"], cfg, job_id, upload.id),
            daemon=True,
        ).start()

        return {"job_id": job_id, "upload_id": upload.id, "file_type": meta["category"]}

    # ── Background processing ─────────────────────────────────────────────────

    def _process_async(self, file_path: str, category: str, config: dict,
                       job_id: str, upload_id: int):
        """Runs in a daemon thread. Updates jobs[job_id] in real-time."""
        job = jobs[job_id]
        job["status"] = "running"

        try:
            _log(job, "STEP", f"Processing: {os.path.basename(file_path)} [{category}]")

            # Route to correct extractor
            pages = model_router.extract_pages(
                file_path, category, config.get("MAX_CHARS_PER_PAGE", 5000)
            )
            total = len(pages)
            job["total_pages"] = total

            if total == 0:
                _log(job, "ERROR", "No readable content found in file.")
                job["status"] = "failed"
                self._update_db(upload_id, "failed", 0, None, "No readable content")
                return

            _log(job, "INFO", f"Extracted {total} page(s) for LLM processing")

            delay         = float(config.get("request_delay", 2.0))
            current_model = config["model"]
            llm           = build_llm(config, current_model)
            _log(job, "OK", f"LLM ready — model: {current_model}")

            os.makedirs(config["output_dir"], exist_ok=True)
            base      = os.path.splitext(os.path.basename(file_path))[0]
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            sql_path  = os.path.join(config["output_dir"], f"{base}_{ts}.sql")
            json_path = os.path.join(config["output_dir"], f"{base}_{ts}.json")
            job["sql_file"]  = sql_path
            job["json_file"] = json_path

            profiles, success = [], 0

            with open(sql_path, "w", encoding="utf-8") as sf:
                sf.write(sql_file_header(file_path, total))

                for idx, (page_num, text) in enumerate(pages):
                    job["processed"] = idx + 1
                    _log(job, "STEP", f"Page {page_num}/{total} — {len(text)} chars")

                    if idx > 0:
                        time.sleep(delay)

                    profile, error = extract_profile(
                        llm, text, config.get("MAX_CHARS_PER_PAGE", 5000),
                        api_key=config["api_key"]
                    )

                    # Soft rate limit → switch model
                    if error and error.startswith("RATE_LIMIT|"):
                        parts      = error.split("|")
                        next_model = parts[1]
                        wait_sec   = int(parts[2])
                        _log(job, "WARN", f"Rate limit → switching to {next_model}, waiting {wait_sec}s")
                        job["current_model"] = next_model
                        time.sleep(wait_sec)
                        llm = build_llm(config, next_model)
                        profile, error = extract_profile(
                            llm, text, config.get("MAX_CHARS_PER_PAGE", 5000),
                            api_key=config["api_key"]
                        )

                    # Hard rate limit → pause
                    if error and error.startswith("RATE_LIMIT_HARD|"):
                        wait_mins = int(error.split("|")[1])
                        _log(job, "WARN", f"Daily limit hit, waiting {wait_mins} min")
                        job["status"] = "paused"
                        job["pause_reason"] = f"Rate limit — resuming in {wait_mins} min"
                        time.sleep(wait_mins * 60)
                        job["status"] = "running"
                        job["pause_reason"] = None
                        for m in FALLBACK_MODELS:
                            llm = build_llm(config, m)
                            profile, error = extract_profile(
                                llm, text, config.get("MAX_CHARS_PER_PAGE", 5000),
                                api_key=config["api_key"]
                            )
                            if not error:
                                job["current_model"] = m
                                _log(job, "OK", f"Resumed with {m}")
                                break

                    if error and not profile:
                        _log(job, "ERROR", f"Page {page_num} — {error}")
                        continue

                    if profile and is_valid_profile(profile):
                        sql = to_sql_insert(profile, config.get("DB_TABLE", "register"))
                        sf.write(f"-- Page {page_num}: {profile.get('Name','Unknown')}\n")
                        sf.write(sql + "\n\n")
                        sf.flush()
                        profiles.append(profile)
                        success += 1
                        job["success"]  = success
                        job["profiles"] = profiles
                        _log(job, "OK", f"Page {page_num} ✓ — {profile.get('Name','?')}")
                    else:
                        _log(job, "SKIP", f"Page {page_num} — skipped (no valid profile)")

            # Write JSON
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(profiles, jf, indent=2, ensure_ascii=False)

            job["status"]   = "done"
            job["profiles"] = profiles
            _log(job, "OK", f"COMPLETE — {success}/{total} profiles extracted")

            self._update_db(upload_id, "done", success,
                            json.dumps(profiles, ensure_ascii=False))

        except Exception as exc:
            _log(job, "ERROR", f"Fatal: {exc}")
            job["status"] = "failed"
            self._update_db(upload_id, "failed", 0, None, str(exc))
            logger.exception("Unhandled error in background job %s", job_id)

    # ── DB helper ─────────────────────────────────────────────────────────────

    @staticmethod
    def _update_db(upload_id: int, status: str, profiles_count: int,
                   output_json: str | None = None, error: str | None = None):
        try:
            upload = Upload.query.get(upload_id)
            if upload:
                upload.status           = status
                upload.profiles_count   = profiles_count
                upload.processed_output = output_json
                upload.error_message    = error
                if status in ("done", "failed"):
                    upload.completed_at = datetime.now(timezone.utc)
                db.session.commit()
        except Exception as exc:
            logger.error("DB update failed for upload %d: %s", upload_id, exc)
