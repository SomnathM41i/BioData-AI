"""
services/upload_service.py — Orchestrates the full processing pipeline

Flow:
  receive file → validate → store → create DB record → queue job →
  extract pages (via model_router) → run LLM extraction → save results → update DB

Key fixes vs previous version:
  1. user_id stored in job dict  → fixes 404 on every poll
  2. total_pages set BEFORE the LLM loop  → fixes 0/0 pages display
  3. retries / skipped counters  → real-time rate-limit visibility
  4. pause_reason set/cleared per retry   → frontend banner works
  5. processed incremented after every page regardless of outcome
  6. status starts as "processing" (not "queued") so frontend
     poll logic (status === 'processing') matches immediately
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from models.database import db, Upload
from services.storage import StorageService, StorageError
from services.model_router import model_router
from core.extractor import build_llm, extract_profile, is_valid_profile, FALLBACK_MODELS
from core.sql_generator import to_sql_insert, sql_file_header
from core.logger import make_log_entry

logger = logging.getLogger(__name__)

# ── In-memory job store ───────────────────────────────────────────────────────
# Imported by api.py:  from services.upload_service import jobs, chat_histories
# Both modules share the SAME dict objects — never re-assign these at module level.
jobs: dict[str, dict] = {}
chat_histories: dict[str, list] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(job: dict, level: str, msg: str) -> None:
    entry = make_log_entry(level, msg)
    job["logs"].append(entry)
    log_fn = {
        "OK":    logger.info,
        "STEP":  logger.info,
        "SKIP":  logger.info,
        "WARN":  logger.warning,
        "ERROR": logger.error,
        "INFO":  logger.info,
    }.get(level, logger.info)
    log_fn("[%s] %s | %s", entry["time"], level, msg)


# ── UploadService ─────────────────────────────────────────────────────────────

class UploadService:

    def __init__(self, config: dict):
        self.config  = config
        self.storage = StorageService(config)

    # ── Public entry point ────────────────────────────────────────────────────

    def handle_upload(self, file_obj, user_id: int, api_key: str,
                      model: str | None = None, delay: float = 2.0) -> dict:
        """
        Called from the Flask upload route (on the request thread).

        Steps run synchronously so the first poll always returns valid data:
          1. Validate & save file to disk
          2. Create DB Upload record
          3. Generate job_id (one variable — key AND response value)
          4. Register jobs[job_id] with user_id BEFORE thread starts
          5. Parse file to get total_pages — stored in job dict here
          6. Spawn background thread
          7. Return {job_id, upload_id, file_type}

        Returns: { job_id, upload_id, file_type }
        Raises:  StorageError on validation failure
        """
        # ── 1. Save file ──────────────────────────────────────────────────────
        meta = self.storage.save(file_obj)

        # ── 2. DB record ──────────────────────────────────────────────────────
        effective_model = model or self.config.get("GROQ_MODEL", "llama-3.3-70b-versatile")

        upload = Upload(
            user_id=user_id,
            original_filename=meta["original_filename"],
            stored_filename=meta["stored_filename"],
            file_type=meta["category"],
            file_path=meta["file_path"],
            file_size_bytes=meta["file_size_bytes"],
            status="processing",
            model_used=effective_model,
        )
        db.session.add(upload)
        db.session.commit()

        # ── 3. Generate job_id ────────────────────────────────────────────────
        # Single variable — used as jobs{} key AND returned to client.
        # YYYYMMDDHHMMSS + microseconds (6 digits) = 20-char unique string.
        job_id = datetime.now().strftime("%Y%m%d%H%M%S%f")

        # ── 4. Build and register job dict BEFORE spawning thread ─────────────
        #
        # ROOT CAUSE OF 404 BUG:
        # api.py._get_job_for_current_user() does:
        #     if job.get("user_id") != current_user.id: return 404
        # The old code never stored user_id, so every poll returned 404.
        #
        job = {
            # Identity — user_id REQUIRED for ownership check in api.py
            "job_id":        job_id,
            "user_id":       user_id,          # ← THE FIX
            "upload_id":     upload.id,
            "file":          meta["original_filename"],
            "file_type":     meta["category"],
            # Status — "processing" so JS `status === 'processing'` matches
            "status":        "processing",
            "pause_reason":  None,
            # Progress counters — all 0, worker updates in real time
            "total_pages":   0,    # set below BEFORE thread starts
            "processed":     0,    # +1 per page (success OR skip)
            "success":       0,    # +1 on valid profile extracted
            "skipped":       0,    # +1 on page with no valid profile
            "retries":       0,    # +1 on every 429 retry attempt
            # Model
            "current_model": effective_model,
            # Output
            "profiles":      [],
            "sql_file":      None,
            "json_file":     None,
            "started_at":    datetime.now().isoformat(),
            # Logs — must be a list from the start, never None
            "logs":          [],
        }
        jobs[job_id]           = job
        chat_histories[job_id] = []

        # Persist job_id to DB so history panel can reference it
        upload.job_id = job_id
        db.session.commit()

        _log(job, "STEP", f"Processing: {meta['original_filename']} [{meta['category']}]")

        # ── 5. Parse pages SYNCHRONOUSLY to set total_pages ──────────────────
        #
        # FIX: Setting total_pages before returning means the very first poll
        # (which arrives ~200ms after upload) shows "N/56" not "0/0".
        #
        try:
            pages = model_router.extract_pages(
                meta["file_path"],
                meta["category"],
                self.config.get("MAX_CHARS_PER_PAGE", 5000)
            )
            job["total_pages"] = len(pages)
            _log(job, "INFO", f"Extracted {len(pages)} page(s) for LLM processing")

        except Exception as exc:
            _log(job, "ERROR", f"Page extraction failed: {exc}")
            job["status"] = "failed"
            self._update_db(upload.id, "failed", 0, None, str(exc))
            raise StorageError(f"Could not read file: {exc}") from exc

        if not pages:
            _log(job, "ERROR", "No readable content found in file.")
            job["status"] = "failed"
            self._update_db(upload.id, "failed", 0, None, "No readable content")
            return {"job_id": job_id, "upload_id": upload.id, "file_type": meta["category"]}

        # ── 6. Spawn background worker ────────────────────────────────────────
        cfg = {
            **self.config,
            "api_key":       api_key,
            "model":         effective_model,
            "request_delay": delay,
            "output_dir":    self.config.get("OUTPUT_FOLDER", "./output"),
        }

        threading.Thread(
            target=self._process_async,
            args=(pages, cfg, job_id, upload.id),
            daemon=True,
            name=f"worker-{job_id}",
        ).start()

        # ── 7. Return SAME job_id that is stored in jobs{} ───────────────────
        return {"job_id": job_id, "upload_id": upload.id, "file_type": meta["category"]}

    # ── Background worker ─────────────────────────────────────────────────────

    def _process_async(self, pages: list, config: dict,
                       job_id: str, upload_id: int) -> None:
        """
        Runs in a daemon thread. Mutates jobs[job_id] in-place.
        Frontend polls /api/status/<job_id> every second and reads
        these fields to drive all UI updates.
        """
        job = jobs[job_id]

        try:
            total         = job["total_pages"]
            delay         = float(config.get("request_delay", 2.0))
            current_model = config["model"]
            llm           = build_llm(config, current_model)

            _log(job, "OK", f"LLM ready — model: {current_model}")

            # Prepare output files
            os.makedirs(config["output_dir"], exist_ok=True)
            base      = Path(job["file"]).stem
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            sql_path  = os.path.join(config["output_dir"], f"{base}_{ts}.sql")
            json_path = os.path.join(config["output_dir"], f"{base}_{ts}.json")
            job["sql_file"]  = sql_path
            job["json_file"] = json_path

            profiles = []

            with open(sql_path, "w", encoding="utf-8") as sf:
                sf.write(sql_file_header(job["file"], total))

                for idx, (page_num, text) in enumerate(pages):

                    _log(job, "STEP", f"Page {page_num}/{total} — {len(text)} chars")

                    # Respect per-request delay (skip first page)
                    if idx > 0:
                        time.sleep(delay)

                    # ── LLM call with retry tracking ──────────────────────────
                    profile, error = self._call_llm_with_retry(
                        job, config, text, page_num, total
                    )

                    # Sync local model var if worker switched it
                    current_model = job["current_model"]

                    # Always clear pause after attempt
                    job["pause_reason"] = None

                    # Always increment processed (success + skip + error all count)
                    job["processed"] += 1

                    # ── Classify outcome ──────────────────────────────────────
                    if profile and is_valid_profile(profile):
                        name = profile.get("Name", "?")
                        sql  = to_sql_insert(profile, config.get("DB_TABLE", "register"))
                        sf.write(f"-- Page {page_num}: {name}\n{sql}\n\n")
                        sf.flush()
                        profiles.append(profile)
                        job["success"]  += 1
                        job["profiles"]  = list(profiles)  # copy so poll sees update
                        _log(job, "OK",   f"Page {page_num} ✓ — {name}")
                    else:
                        job["skipped"] += 1
                        reason = error or "no valid profile"
                        _log(job, "SKIP", f"Page {page_num} — skipped ({reason})")

            # Write JSON
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(profiles, jf, indent=2, ensure_ascii=False)

            job["status"]   = "done"
            job["profiles"] = profiles
            _log(job, "OK",
                 f"COMPLETE — {job['success']}/{total} profiles extracted "
                 f"({job['skipped']} skipped, {job['retries']} retries)")

            self._update_db(upload_id, "done", job["success"],
                            json.dumps(profiles, ensure_ascii=False))

        except Exception as exc:
            _log(job, "ERROR", f"Fatal: {exc}")
            job["status"]       = "failed"
            job["pause_reason"] = None
            self._update_db(upload_id, "failed", 0, None, str(exc))
            logger.exception("Unhandled error in background job %s", job_id)

    # ── LLM call with real-time retry/pause tracking ──────────────────────────

    def _call_llm_with_retry(self, job: dict, config: dict,
                              text: str, page_num: int, total: int):
        """
        Calls extract_profile() and handles rate-limit errors in real time,
        updating job fields so every poll reflects current state.

        Soft rate-limit  (RATE_LIMIT|model|seconds):
          - Sets pause_reason → frontend shows blue banner
          - Increments retries counter
          - Switches model and retries once

        Hard rate-limit  (RATE_LIMIT_HARD|minutes):
          - Tries every FALLBACK_MODEL in sequence
          - Falls back to timed wait if all exhausted
        """
        llm     = build_llm(config, job["current_model"])
        profile, error = extract_profile(
            llm, text, config.get("MAX_CHARS_PER_PAGE", 5000),
            api_key=config["api_key"]
        )

        # ── Soft rate-limit ───────────────────────────────────────────────────
        if error and error.startswith("RATE_LIMIT|"):
            parts      = error.split("|")
            next_model = parts[1] if len(parts) > 1 else job["current_model"]
            wait_sec   = int(parts[2]) if len(parts) > 2 else 10

            job["retries"]      += 1
            job["current_model"] = next_model
            job["pause_reason"]  = (
                f"Rate limit — switching to {next_model}, "
                f"waiting {wait_sec}s (page {page_num}/{total})"
            )
            _log(job, "WARN",
                 f"Rate limit → switching to {next_model}, waiting {wait_sec}s")

            time.sleep(wait_sec)
            job["pause_reason"] = None

            llm_new = build_llm(config, next_model)
            profile, error = extract_profile(
                llm_new, text, config.get("MAX_CHARS_PER_PAGE", 5000),
                api_key=config["api_key"]
            )

            if not error:
                return profile, error

        # ── Hard rate-limit ───────────────────────────────────────────────────
        if error and error.startswith("RATE_LIMIT_HARD|"):
            wait_mins = int(error.split("|")[1]) if "|" in error else 1
            job["retries"]     += 1
            job["pause_reason"] = (
                f"Daily limit hit — trying fallback models "
                f"(page {page_num}/{total})"
            )
            _log(job, "WARN",
                 f"Hard rate limit — trying {len(FALLBACK_MODELS)} fallback model(s)")

            for fallback in FALLBACK_MODELS:
                if fallback == job["current_model"]:
                    continue

                job["retries"]     += 1
                job["pause_reason"] = (
                    f"Trying fallback: {fallback} (page {page_num}/{total})"
                )
                _log(job, "WARN", f"Trying fallback model: {fallback}")

                try:
                    llm_fb = build_llm(config, fallback)
                    profile, error = extract_profile(
                        llm_fb, text, config.get("MAX_CHARS_PER_PAGE", 5000),
                        api_key=config["api_key"]
                    )
                    if not error:
                        job["current_model"] = fallback
                        job["pause_reason"]  = None
                        _log(job, "OK", f"Fallback succeeded: {fallback}")
                        return profile, error
                except Exception as fb_exc:
                    _log(job, "WARN", f"Fallback {fallback} failed: {fb_exc}")

            # All fallbacks exhausted — timed wait then retry original
            job["pause_reason"] = (
                f"All fallbacks exhausted — waiting {wait_mins}m "
                f"(page {page_num}/{total})"
            )
            _log(job, "WARN", f"All fallbacks exhausted, waiting {wait_mins} min")
            time.sleep(wait_mins * 60)
            job["pause_reason"] = None

            llm_orig = build_llm(config, config["model"])
            profile, error = extract_profile(
                llm_orig, text, config.get("MAX_CHARS_PER_PAGE", 5000),
                api_key=config["api_key"]
            )

        return profile, error

    # ── DB helper ─────────────────────────────────────────────────────────────

    @staticmethod
    def _update_db(upload_id: int, status: str, profiles_count: int,
                   output_json: str | None = None,
                   error: str | None = None) -> None:
        try:
            upload = db.session.get(Upload, upload_id)   # SQLAlchemy 2.x
            if not upload:
                return
            upload.status           = status
            upload.profiles_count   = profiles_count
            upload.processed_output = output_json
            upload.error_message    = error
            if status in ("done", "failed"):
                upload.completed_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception as exc:
            logger.error("DB update failed for upload %d: %s", upload_id, exc)
            db.session.rollback()