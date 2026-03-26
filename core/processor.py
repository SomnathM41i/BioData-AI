"""
core/processor.py - with rate limit handling, delay, model fallback
"""
import os, json, time, re
from datetime import datetime
from core.reader       import get_pages
from core.extractor    import build_llm, extract_profile, is_valid_profile, FALLBACK_MODELS
from core.sql_generator import to_sql_insert, sql_file_header
from core.logger       import make_log_entry


def _log(job, level, msg):
    entry = make_log_entry(level, msg)
    job["logs"].append(entry)
    print(f"[{entry['time']}] {level:<5} | {msg}")


def process_file_streaming(path, config, job_id, jobs):
    job = jobs[job_id]
    job["status"] = "running"
    job["current_model"] = config["model"]

    try:
        _log(job, "STEP", f"Starting: {os.path.basename(path)}")

        pages, total_pdf_pages = get_pages(path)
        total = len(pages)
        job["total_pages"]     = total
        job["total_pdf_pages"] = total_pdf_pages

        _log(job, "INFO", f"Found {total} non-empty pages")

        if total == 0:
            _log(job, "ERROR", "No readable pages found")
            job["status"] = "failed"
            return

        delay        = float(config.get("request_delay", 1.2))
        current_model = config["model"]
        llm          = build_llm(config, current_model)
        _log(job, "OK", f"Connected to Groq — model: {current_model}")

        os.makedirs(config["output_dir"], exist_ok=True)
        base      = os.path.splitext(os.path.basename(path))[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sql_path  = os.path.join(config["output_dir"], f"{base}_{timestamp}.sql")
        json_path = os.path.join(config["output_dir"], f"{base}_{timestamp}.json")
        job["sql_file"]  = sql_path
        job["json_file"] = json_path

        profiles, sqls = [], []
        success = 0

        with open(sql_path, "w", encoding="utf-8") as sf:
            sf.write(sql_file_header(path, total))

            for idx, (page_num, text) in enumerate(pages):
                job["processed"] = idx + 1
                _log(job, "STEP", f"Page {page_num}/{total_pdf_pages} — {len(text)} chars")

                # Polite delay between requests
                if idx > 0:
                    time.sleep(delay)

                profile, error = extract_profile(
                    llm, text, config["max_chars"],
                    api_key=config["api_key"]
                )

                # Handle rate limit with model fallback
                if error and error.startswith("RATE_LIMIT|"):
                    parts = error.split("|")
                    next_model = parts[1]
                    wait_sec   = int(parts[2])
                    _log(job, "WARN", f"Rate limit hit → switching to {next_model}, waiting {wait_sec}s...")
                    job["current_model"] = next_model
                    time.sleep(wait_sec)
                    llm = build_llm(config, next_model)
                    current_model = next_model
                    # Retry this page
                    profile, error = extract_profile(llm, text, config["max_chars"], api_key=config["api_key"])

                # Hard rate limit — need to wait long
                if error and error.startswith("RATE_LIMIT_HARD|"):
                    wait_mins = int(error.split("|")[1])
                    _log(job, "WARN", f"Daily token limit reached. Waiting {wait_mins} min before resuming...")
                    job["status"] = "paused"
                    job["pause_reason"] = f"Rate limit — resuming in {wait_mins} min"
                    time.sleep(wait_mins * 60)
                    job["status"] = "running"
                    job["pause_reason"] = None
                    # Try all fallback models
                    for m in FALLBACK_MODELS:
                        llm = build_llm(config, m)
                        current_model = m
                        job["current_model"] = m
                        profile, error = extract_profile(llm, text, config["max_chars"], api_key=config["api_key"])
                        if not error:
                            _log(job, "OK", f"Resumed with model: {m}")
                            break

                if error and not profile:
                    _log(job, "ERROR", f"Page {page_num} — {error}")
                    continue

                if profile and is_valid_profile(profile):
                    sql = to_sql_insert(profile, config["table_name"])
                    sf.write(f"-- Page {page_num}: {profile.get('Name','Unknown')}\n")
                    sf.write(sql + "\n\n")
                    sf.flush()
                    profiles.append(profile)
                    sqls.append(sql)
                    success += 1
                    job["success"]  = success
                    job["profiles"] = profiles
                    found = [k for k,v in profile.items() if v is not None]
                    _log(job, "OK", f"Page {page_num} ✓ — {len(found)} fields: {profile.get('Name','?')}")
                else:
                    _log(job, "SKIP", f"Page {page_num} — not a valid profile")

        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(profiles, jf, indent=2, ensure_ascii=False)

        job["status"]   = "done"
        job["profiles"] = profiles
        _log(job, "OK", f"COMPLETE — {success}/{total} profiles extracted")

    except Exception as e:
        _log(job, "ERROR", f"Fatal: {e}")
        job["status"] = "failed"


def process_file(path, config, page_range=None):
    """CLI mode"""
    pages, _ = get_pages(path, page_range)
    llm      = build_llm(config)
    os.makedirs(config["output_dir"], exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    sql_path  = os.path.join(config["output_dir"], f"{base}_{ts}.sql")
    json_path = os.path.join(config["output_dir"], f"{base}_{ts}.json")
    profiles, success = [], 0
    with open(sql_path, "w", encoding="utf-8") as sf:
        sf.write(sql_file_header(path, len(pages)))
        for page_num, text in pages:
            time.sleep(1.5)
            profile, error = extract_profile(llm, text, config["max_chars"], api_key=config["api_key"])
            if profile and is_valid_profile(profile):
                sf.write(to_sql_insert(profile, config["table_name"]) + "\n\n")
                sf.flush()
                profiles.append(profile)
                success += 1
                print(f"✅ Page {page_num} → {profile.get('Name','?')}")
            else:
                print(f"⏭ Page {page_num} skipped ({error or 'no profile'})")
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(profiles, jf, indent=2, ensure_ascii=False)
    print(f"\n✅ Done! {success}/{len(pages)} profiles → {sql_path}")
