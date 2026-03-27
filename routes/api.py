"""
routes/api.py — All /api/* endpoints

Endpoints:
  POST /api/upload          → upload file, start job
  GET  /api/status/<job_id> → poll job status
  POST /api/export/<job_id> → download results (sql/csv/excel/json)
  POST /api/chat            → chat with extracted data
  GET  /api/uploads         → list current user's upload history
  GET  /api/fields          → default field list
"""
import os
import json
import logging
from datetime import datetime

from flask import (Blueprint, request, jsonify, send_file,
                   Response, current_app)
from flask_login import login_required, current_user

from middleware.security import require_rate_limit
from models.database import db, Upload
from services.upload_service import UploadService, jobs, chat_histories
from services.storage import StorageError
from core.exporter import to_sql, to_csv, to_excel, to_json, DEFAULT_FIELDS

logger = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__, url_prefix="/api")


def _get_upload_service() -> UploadService:
    return UploadService(current_app.config)


# ── Fields ───────────────────────────────────────────────────────────────────

@api_bp.route("/fields")
def get_fields():
    return jsonify(DEFAULT_FIELDS)


# ── Upload ────────────────────────────────────────────────────────────────────

@api_bp.route("/upload", methods=["POST"])
@login_required
@require_rate_limit
def upload():
    """
    Accepts: multipart/form-data
      file        — the uploaded file
      api_key     — Groq API key (overrides env)
      model       — LLM model name (optional)
      request_delay — seconds between LLM calls (default 2.0)
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file    = request.files["file"]
    # API key: prefer form value (user override), else use from .env config
    api_key = request.form.get("api_key", "").strip() or current_app.config.get("GROQ_API_KEY", "")
    delay   = float(request.form.get("request_delay", 2.0))
    model   = request.form.get("model", "").strip() or None

    if not file.filename:
        return jsonify({"error": "No file selected"}), 400
    if not api_key:
        return jsonify({"error": "Groq API key not configured. Set GROQ_API_KEY in .env"}), 400

    try:
        svc    = _get_upload_service()
        result = svc.handle_upload(
            file_obj=file,
            user_id=current_user.id,
            api_key=api_key,
            model=model,
            delay=delay,
        )
        return jsonify(result), 202

    except StorageError as exc:
        logger.warning("Upload validation failed: %s", exc)
        return jsonify({"error": str(exc)}), 422

    except Exception as exc:
        logger.exception("Unexpected upload error")
        return jsonify({"error": "Internal server error"}), 500


# ── Status ────────────────────────────────────────────────────────────────────

@api_bp.route("/status/<job_id>")
@login_required
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ── Export ────────────────────────────────────────────────────────────────────

@api_bp.route("/export/<job_id>", methods=["POST"])
@login_required
def export(job_id: str):
    """
    Body JSON:
      format   : "sql" | "csv" | "excel" | "json"
      table    : SQL table name (default "register")
      fields   : null | [str] | [{from, to}] | {out: src}
      filename : output filename without extension
    """
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    profiles = job.get("profiles", [])
    if not profiles:
        return jsonify({"error": "No profiles extracted yet"}), 400

    data     = request.json or {}
    fmt      = data.get("format", "sql").lower()
    table    = data.get("table", "register")
    fields   = data.get("fields", None)
    filename = data.get("filename", "")

    base = filename or f"profiles_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(current_app.config.get("OUTPUT_FOLDER", "./output"), exist_ok=True)
    out_dir = current_app.config.get("OUTPUT_FOLDER", "./output")

    try:
        if fmt == "sql":
            content = to_sql(profiles, table=table, fields=fields)
            path = os.path.join(out_dir, f"{base}.sql")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return send_file(path, as_attachment=True,
                             download_name=f"{base}.sql", mimetype="text/plain")

        elif fmt == "csv":
            return Response(
                to_csv(profiles, fields=fields),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename={base}.csv"},
            )

        elif fmt == "excel":
            path = os.path.join(out_dir, f"{base}.xlsx")
            to_excel(profiles, fields=fields, output_path=path)
            return send_file(path, as_attachment=True,
                             download_name=f"{base}.xlsx",
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        elif fmt == "json":
            return Response(
                to_json(profiles, fields=fields),
                mimetype="application/json",
                headers={"Content-Disposition": f"attachment; filename={base}.json"},
            )

        return jsonify({"error": f"Unknown format: {fmt}"}), 400

    except Exception as exc:
        logger.exception("Export failed for job %s", job_id)
        return jsonify({"error": str(exc)}), 500


# ── Chat ──────────────────────────────────────────────────────────────────────

@api_bp.route("/chat", methods=["POST"])
@login_required
@require_rate_limit
def chat():
    data    = request.json or {}
    job_id  = data.get("job_id")
    message = (data.get("message") or "").strip()
    api_key = (data.get("api_key") or "").strip() or current_app.config.get("GROQ_API_KEY", "")

    if not message:
        return jsonify({"error": "Message is required"}), 400
    if not api_key:
        return jsonify({"error": "API key is required"}), 400

    job      = jobs.get(job_id)
    profiles = job.get("profiles", []) if job else []
    history  = chat_histories.get(job_id, [])

    profile_ctx = ""
    if profiles:
        profile_ctx = f"\n\nYou have {len(profiles)} extracted matrimonial profiles:\n"
        for i, p in enumerate(profiles[:10]):
            profile_ctx += f"\nProfile {i+1}: {json.dumps({k: v for k, v in p.items() if v}, ensure_ascii=False)}"

    system = (
        "You are a helpful matrimonial data assistant. "
        "Help users analyse profiles, find matches, summarise data, or write SQL/CSV queries."
        f"{profile_ctx}\n\nBe concise and clear. Use markdown tables when listing profiles."
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
        chat_histories[job_id] = history
        return jsonify({"reply": reply})

    except Exception as exc:
        logger.error("Chat error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── Upload history ────────────────────────────────────────────────────────────

@api_bp.route("/uploads")
@login_required
def list_uploads():
    """Return current user's upload history."""
    uploads = (Upload.query
               .filter_by(user_id=current_user.id)
               .order_by(Upload.created_at.desc())
               .limit(50)
               .all())
    return jsonify([u.to_dict() for u in uploads])
