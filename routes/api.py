"""
routes/api.py — All /api/* endpoints

Real-time tracking additions vs original:
  - job dict initialised with ALL fields BEFORE the background thread starts
    so the first poll (which may arrive in <1 s) never returns 0/0 pages
  - total_pages set immediately after PDF/doc parsing, before the LLM loop
  - pause_reason set/cleared per page for rate-limit visibility
  - retries counter incremented on every 429 retry
  - skipped counter incremented for pages with no valid profile
  - logs list always pre-initialised as [] — never None

Endpoints:
  POST   /api/upload              → upload file, start extraction job
  GET    /api/status/<job_id>     → poll job status (owner-only)
  POST   /api/export/<job_id>     → download results as sql/csv/excel/json
  POST   /api/chat                → chat with extracted data via LLM
  GET    /api/uploads             → list current user's upload history
  DELETE /api/uploads/<id>        → delete an upload record
  GET    /api/fields              → default field list
"""

import io
import json
import logging
import os
import tempfile
from datetime import datetime

from flask import (
    Blueprint, request, jsonify, send_file, Response, current_app,
    after_this_request
)
from flask_login import login_required, current_user

from middleware.security import require_rate_limit
from models.database import db, Upload
from services.upload_service import UploadService, jobs, chat_histories
from services.storage import StorageError
from core.exporter import to_sql, to_csv, to_excel, to_json, DEFAULT_FIELDS

logger = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__, url_prefix="/api")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_upload_service() -> UploadService:
    return UploadService(current_app.config, current_app._get_current_object())


def _get_job_for_current_user(job_id: str):
    """
    Return (job, None) if the job exists and belongs to the current user.
    Return (None, error_response) otherwise — caller checks error first.
    Using 404 for both missing and unauthorised to avoid leaking job existence.
    """
    job = jobs.get(job_id)
    if job and job.get("user_id") != current_user.id:
        return None, (jsonify({"error": "Job not found"}), 404)
    if job:
        return job, None

    upload = Upload.query.filter_by(job_id=job_id, user_id=current_user.id).first()
    if not upload:
        return None, (jsonify({"error": "Job not found"}), 404)

    job = _job_from_upload(upload)
    jobs[job_id] = job
    return job, None


def _now_ts() -> str:
    """Current time as HH:MM:SS string — matches frontend ts() format."""
    return datetime.now().strftime("%H:%M:%S")


def _job_from_upload(upload: Upload) -> dict:
    """Rehydrate a completed/failed job from persisted upload history.

    Priority order for profile data:
      1. DB processed_output  (always up-to-date if worker completed)
      2. json_file_path on disk  (output folder — fallback after server restart)
      3. Empty list  (job failed before any profiles were extracted)
    """
    profiles = []

    # ── 1. Try DB blob first ──────────────────────────────────────────────────
    if upload.processed_output:
        try:
            parsed = json.loads(upload.processed_output)
            if isinstance(parsed, list):
                profiles = parsed
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid processed_output JSON for upload %s", upload.id)

    # ── 2. Fall back to JSON file on disk if DB blob is empty ─────────────────
    if not profiles and upload.json_file_path:
        try:
            with open(upload.json_file_path, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            if isinstance(disk_data, list) and disk_data:
                profiles = disk_data
                logger.info(
                    "Rehydrated %d profiles from disk for upload %s",
                    len(profiles), upload.id,
                )
                # Back-fill DB so next load is faster
                try:
                    upload.processed_output = json.dumps(profiles, ensure_ascii=False)
                    upload.profiles_count   = len(profiles)
                    from models.database import db as _db
                    _db.session.commit()
                except Exception as db_exc:
                    logger.warning("Could not back-fill DB for upload %s: %s", upload.id, db_exc)
        except FileNotFoundError:
            logger.warning(
                "JSON file not found for upload %s: %s",
                upload.id, upload.json_file_path,
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read JSON file for upload %s: %s",
                upload.id, exc,
            )

    success = upload.profiles_count or len(profiles)
    # Ensure profiles_count in DB matches reality
    if len(profiles) > success:
        success = len(profiles)

    total    = max(success, 1 if upload.status in ("done", "failed") else 0)
    filename = upload.original_filename

    return {
        "job_id":        upload.job_id,
        "user_id":       upload.user_id,
        "upload_id":     upload.id,
        "file":          filename,
        "filename":      filename,
        "file_type":     upload.file_type,
        "status":        upload.status,
        "pause_reason":  None,
        "total_pages":   total,
        "processed":     total if upload.status in ("done", "failed") else 0,
        "success":       success,
        "skipped":       0,
        "retries":       0,
        "current_model": upload.model_used,
        "profiles":      profiles,
        "sql_file":      upload.sql_file_path,
        "json_file":     upload.json_file_path,
        "started_at":    upload.created_at.isoformat() if upload.created_at else None,
        "logs": [{
            "level": "INFO",
            "msg":   f"Loaded from history: {filename} — {success} profile(s)",
            "time":  _now_ts(),
        }],
    }


def _make_job(job_id: str, user_id: int, filename: str,
              file_type: str, model: str) -> dict:
    """
    Build the initial job dictionary with every field pre-populated.
    Setting total_pages=0 here (rather than None) means the frontend
    can show '0/0' immediately and the progress bar starts at 0 % instead
    of dividing by zero.

    Fields the background worker must update in-place:
        total_pages   — set BEFORE the page loop starts
        processed     — incremented once per page (success OR skip)
        success       — incremented on successful LLM extraction
        skipped       — incremented when no valid profile found
        retries       — incremented on every 429 retry attempt
        current_model — updated if the model switches mid-job
        pause_reason  — set to a string while rate-limited, cleared on resume
        profiles      — appended to as extractions succeed
        logs          — appended to throughout
        status        — 'processing' → 'done' | 'failed'
    """
    return {
        # Identity
        "job_id":        job_id,
        "user_id":       user_id,
        "filename":      filename,
        "file_type":     file_type,
        # Status
        "status":        "processing",
        "pause_reason":  None,
        # Progress counters — all initialised to 0 so first poll is meaningful
        "total_pages":   0,
        "processed":     0,
        "success":       0,
        "skipped":       0,
        "retries":       0,
        # Model in use
        "current_model": model or "llama-3.3-70b-versatile",
        # Data
        "profiles":      [],
        # Logs — MUST be a list, never None
        "logs":          [
            {
                "level": "STEP",
                "msg":   f"Job {job_id} initialised [{file_type}]",
                "time":  _now_ts(),
            }
        ],
    }


# ── Fields ────────────────────────────────────────────────────────────────────

@api_bp.route("/fields")
def get_fields():
    return jsonify(DEFAULT_FIELDS)


# ── Output folder sync ────────────────────────────────────────────────────────

@api_bp.route("/sync-output", methods=["POST"])
@login_required
def sync_output():
    """
    Scan the output folder and back-fill json_file_path / processed_output
    for uploads that have a matching JSON file but empty DB fields.

    Called automatically on dashboard load and available as a manual action.
    Returns { synced: N } — number of records updated.
    """
    output_dir = current_app.config.get("OUTPUT_FOLDER", "./output")
    if not os.path.isdir(output_dir):
        return jsonify({"synced": 0, "message": "Output folder not found"})

    uploads = (
        Upload.query
        .filter_by(user_id=current_user.id)
        .filter(Upload.status.in_(["done", "failed"]))
        .all()
    )

    synced = 0
    for upload in uploads:
        # Skip if already has data
        if upload.processed_output and upload.json_file_path:
            continue

        # Try to find a matching JSON file: <stem>_<timestamp>.json
        stem = os.path.splitext(upload.original_filename)[0]
        candidates = []
        try:
            for fname in os.listdir(output_dir):
                if fname.startswith(stem) and fname.endswith(".json"):
                    candidates.append(os.path.join(output_dir, fname))
        except OSError:
            continue

        if not candidates:
            continue

        # Use the most recently modified file
        candidates.sort(key=os.path.getmtime, reverse=True)
        json_path = candidates[0]

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                profiles = json.load(f)
            if not isinstance(profiles, list):
                continue

            upload.json_file_path   = json_path
            upload.processed_output = json.dumps(profiles, ensure_ascii=False)
            upload.profiles_count   = len(profiles)

            # Also find matching SQL file
            sql_path = json_path.replace(".json", ".sql")
            if os.path.exists(sql_path):
                upload.sql_file_path = sql_path

            synced += 1
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("sync_output: could not read %s: %s", json_path, exc)

    if synced:
        try:
            from models.database import db as _db
            _db.session.commit()
            logger.info("sync_output: synced %d upload(s) for user %s", synced, current_user.id)
        except Exception as exc:
            logger.error("sync_output: commit failed: %s", exc)
            from models.database import db as _db
            _db.session.rollback()
            return jsonify({"error": "DB commit failed"}), 500

    return jsonify({"synced": synced})


# ── Upload ────────────────────────────────────────────────────────────────────

@api_bp.route("/upload", methods=["POST"])
@login_required
@require_rate_limit
def upload():
    """
    Accept a file upload, register the job in `jobs` immediately (so the
    first poll returns valid data), then hand off to UploadService in a
    background thread.

    Form fields:
      file          — uploaded file (required)
      api_key       — Groq API key (overrides env; optional if env set)
      model         — LLM model name (optional)
      request_delay — seconds between LLM calls (default 2.0)
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    api_key = (
        request.form.get("api_key", "").strip()
        or current_app.config.get("GROQ_API_KEY", "")
    )
    if not api_key:
        return jsonify({
            "error": "Groq API key not configured. Set GROQ_API_KEY in .env"
        }), 400

    model = request.form.get("model", "").strip() or None

    try:
        delay = float(request.form.get("request_delay", 2.0))
    except (TypeError, ValueError):
        delay = 2.0

    try:
        svc = _get_upload_service()

        # ── CRITICAL: call handle_upload which must:
        #   1. Generate a job_id
        #   2. Store the initial job dict in `jobs` BEFORE returning
        #   3. Start the background extraction thread
        #   4. Return {"job_id": ..., "file_type": ...}
        #
        # UploadService.handle_upload must use _make_job (or equivalent)
        # and call jobs[job_id] = _make_job(...) BEFORE spawning the thread.
        result = svc.handle_upload(
            file_obj=file,
            user_id=current_user.id,
            api_key=api_key,
            model=model,
            delay=delay,
        )
        return jsonify(result), 202

    except StorageError as exc:
        logger.warning("Upload validation failed for user %s: %s", current_user.id, exc)
        return jsonify({"error": str(exc)}), 422

    except Exception:
        logger.exception("Unexpected upload error for user %s", current_user.id)
        return jsonify({"error": "Internal server error"}), 500


# ── Status ─────────────────────────────────────────────────────────────────────

@api_bp.route("/status/<job_id>")
@login_required
def status(job_id: str):
    """
    Poll endpoint — called every second by the frontend.

    Returns the full job dict which includes:
      total_pages, processed, success, skipped, retries,
      current_model, pause_reason, status, profiles, logs.

    The frontend derives ALL display values from this single response —
    no merging with local state — so accuracy depends entirely on the
    background worker keeping these fields current in real time.
    """
    job, err = _get_job_for_current_user(job_id)
    if err:
        return err

    # Return a snapshot; the dict is mutated in-place by the worker thread
    return jsonify(job)


# ── Export ─────────────────────────────────────────────────────────────────────

@api_bp.route("/export/<job_id>", methods=["POST"])
@login_required
@require_rate_limit
def export(job_id: str):
    """
    Body JSON:
      format   : "sql" | "csv" | "excel" | "json"  (required)
      table    : SQL table name (default "register")
      fields   : null | [str] | [{from, to}] | {out: src}
      filename : output filename without extension (optional)
    """
    job, err = _get_job_for_current_user(job_id)
    if err:
        return err

    profiles = job.get("profiles", [])
    if not profiles:
        return jsonify({"error": "No profiles extracted yet"}), 400

    body     = request.get_json(silent=True) or {}
    fmt      = body.get("format", "sql").lower()
    table    = body.get("table", "register") or "register"
    fields   = body.get("fields", None)
    filename = body.get("filename", "").strip()

    base = filename or f"profiles_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        if fmt == "sql":
            content = to_sql(profiles, table=table, fields=fields)
            buf = io.BytesIO(content.encode("utf-8"))
            return send_file(
                buf, as_attachment=True,
                download_name=f"{base}.sql",
                mimetype="text/plain; charset=utf-8",
            )

        elif fmt == "csv":
            return Response(
                to_csv(profiles, fields=fields),
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{base}.csv"'},
            )

        elif fmt == "json":
            return Response(
                to_json(profiles, fields=fields),
                mimetype="application/json; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{base}.json"'},
            )

        elif fmt == "excel":
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp_path = tmp.name
            to_excel(profiles, fields=fields, output_path=tmp_path)

            @after_this_request
            def cleanup_excel_file(response):
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.warning("Could not remove temporary export file: %s", tmp_path)
                return response

            return send_file(
                tmp_path, as_attachment=True,
                download_name=f"{base}.xlsx",
                mimetype=(
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
            )

        return jsonify({"error": f"Unknown format: {fmt}"}), 400

    except Exception:
        logger.exception("Export failed (fmt=%s) for job %s", fmt, job_id)
        return jsonify({"error": "Export failed — internal error"}), 500


# ── Chat ───────────────────────────────────────────────────────────────────────

@api_bp.route("/chat", methods=["POST"])
@login_required
@require_rate_limit
def chat():
    """
    Body JSON:
      job_id  : job to pull profile context from (optional)
      message : user message (required)
      api_key : Groq API key (optional override)
    """
    body    = request.get_json(silent=True) or {}
    job_id  = body.get("job_id")
    message = (body.get("message") or "").strip()
    api_key = (
        (body.get("api_key") or "").strip()
        or current_app.config.get("GROQ_API_KEY", "")
    )

    if not message:
        return jsonify({"error": "Message is required"}), 400
    if not api_key:
        return jsonify({"error": "API key is required"}), 400

    profiles: list = []
    history_key = f"user:{current_user.id}"
    if job_id:
        job, err = _get_job_for_current_user(job_id)
        if err:
            return err
        history_key = job_id
        if job:
            profiles = job.get("profiles", [])

    history = chat_histories.get(history_key, [])

    # Privacy-safe profile context — exclude mobile/email PII from LLM prompt
    EXCLUDED_KEYS = {"Mobile", "Phone", "Email", "mobile", "phone", "email"}
    profile_ctx = ""
    if profiles:
        profile_ctx = f"\n\nYou have {len(profiles)} extracted matrimonial profiles:\n"
        for i, p in enumerate(profiles[:10]):
            safe = {k: v for k, v in p.items() if v and k not in EXCLUDED_KEYS}
            profile_ctx += f"\nProfile {i + 1}: {json.dumps(safe, ensure_ascii=False)}"

    system = (
        "You are a helpful matrimonial data assistant. "
        "Help users analyse profiles, find matches, summarise data, or write SQL/CSV queries."
        f"{profile_ctx}\n\n"
        "Be concise and clear. Use markdown tables when listing multiple profiles."
    )

    messages = [{"role": "system", "content": system}]
    for h in history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        resp   = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            max_tokens=1024,
        )
        reply = resp.choices[0].message.content
        history.append({"role": "user",      "content": message})
        history.append({"role": "assistant", "content": reply})
        chat_histories[history_key] = history
        return jsonify({"reply": reply})

    except Exception as exc:
        logger.error("Chat error for user %s: %s", current_user.id, exc)
        return jsonify({"error": str(exc)}), 500


# ── Upload history ─────────────────────────────────────────────────────────────

@api_bp.route("/uploads")
@login_required
def list_uploads():
    uploads = (
        Upload.query
        .filter_by(user_id=current_user.id)
        .order_by(Upload.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([u.to_dict() for u in uploads])


@api_bp.route("/uploads/<int:upload_id>/profiles")
@login_required
def get_upload_profiles(upload_id: int):
    """
    Return the full profiles list for a specific upload.
    Reads from DB blob first, then falls back to the JSON file on disk.
    Used by the frontend to restore extracted data after a page refresh.
    """
    upload = Upload.query.filter_by(id=upload_id, user_id=current_user.id).first()
    if not upload:
        return jsonify({"error": "Upload not found"}), 404

    job = _job_from_upload(upload)
    return jsonify({
        "profiles":      job["profiles"],
        "profiles_count": len(job["profiles"]),
        "status":        upload.status,
        "job_id":        upload.job_id,
        "filename":      upload.original_filename,
    })


@api_bp.route("/uploads/<int:upload_id>", methods=["DELETE"])
@login_required
def delete_upload(upload_id: int):
    upload = Upload.query.filter_by(id=upload_id, user_id=current_user.id).first()
    if not upload:
        return jsonify({"error": "Upload not found"}), 404

    try:
        db.session.delete(upload)
        db.session.commit()
        return jsonify({"ok": True, "id": upload_id})
    except Exception:
        logger.exception("Failed to delete upload %s for user %s", upload_id, current_user.id)
        db.session.rollback()
        return jsonify({"error": "Failed to delete record"}), 500


# ── upload_service integration notes ─────────────────────────────────────────
#
# Your UploadService.handle_upload must follow this pattern for real-time
# tracking to work correctly. Key requirements:
#
#   def handle_upload(self, file_obj, user_id, api_key, model, delay):
#       job_id  = generate_unique_id()
#       file_type, pages = parse_file(file_obj)       # parse synchronously
#
#       # ❶ Register job BEFORE spawning thread so first poll is valid
#       jobs[job_id] = _make_job(job_id, user_id, file_obj.filename, file_type, model)
#
#       # ❷ Set total_pages BEFORE the extraction loop
#       jobs[job_id]["total_pages"] = len(pages)
#       jobs[job_id]["logs"].append({"level":"STEP",
#           "msg": f"Parsed {len(pages)} pages", "time": _now_ts()})
#
#       # ❸ Spawn background thread
#       t = threading.Thread(target=_extract_pages,
#                            args=(job_id, pages, api_key, model, delay),
#                            daemon=True)
#       t.start()
#
#       return {"job_id": job_id, "file_type": file_type}
#
#
#   def _extract_pages(job_id, pages, api_key, model, delay):
#       for i, page_text in enumerate(pages):
#           # Clear pause before each attempt
#           jobs[job_id]["pause_reason"] = None
#
#           retries = 0
#           while True:
#               try:
#                   result = call_llm(api_key, model, page_text)
#                   break
#               except RateLimitError as e:
#                   retries += 1
#                   jobs[job_id]["retries"] += 1
#                   wait = compute_backoff(retries)
#                   jobs[job_id]["pause_reason"] = f"Rate limit hit — waiting {wait}s…"
#                   jobs[job_id]["logs"].append({
#                       "level": "WARN",
#                       "msg":   f"Page {i+1}: rate limited, retry #{retries} in {wait}s",
#                       "time":  _now_ts(),
#                   })
#                   time.sleep(wait)
#
#           # Clear pause once LLM call succeeds
#           jobs[job_id]["pause_reason"] = None
#
#           # Update counters AFTER each page regardless of outcome
#           jobs[job_id]["processed"] += 1
#
#           if result and result.get("Name"):
#               jobs[job_id]["success"]  += 1
#               jobs[job_id]["profiles"].append(result)
#               jobs[job_id]["logs"].append({
#                   "level": "OK",
#                   "msg":   f"Page {i+1} ✓ — {result['Name']}",
#                   "time":  _now_ts(),
#               })
#           else:
#               jobs[job_id]["skipped"] += 1
#               jobs[job_id]["logs"].append({
#                   "level": "SKIP",
#                   "msg":   f"Page {i+1} — skipped (no valid profile)",
#                   "time":  _now_ts(),
#               })
#
#           time.sleep(delay)
#
#       jobs[job_id]["status"] = "done"
#       jobs[job_id]["logs"].append({
#           "level": "OK",
#           "msg":   f"Done — {jobs[job_id]['success']} profiles extracted",
#           "time":  _now_ts(),
#       })
