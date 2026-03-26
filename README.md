# 💍 Matrimony AI Agent — Production Edition

> Extract structured matrimonial profile data from PDFs, DOCX, images, and text using Groq LLaMA AI — with Google OAuth, intelligent file routing, and production-grade security.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔐 Google OAuth 2.0 | Secure signup/login, no passwords stored |
| 📂 Multi-format Upload | PDF, DOCX/DOC, Images (OCR), TXT |
| 🤖 Intelligent Routing | Each file type → dedicated AI processor |
| 📊 Export | SQL, CSV, Excel, JSON |
| 💬 AI Chat | Query extracted profiles with natural language |
| 🛡️ Production Security | CSRF, rate limiting, security headers |
| 📝 Rotating Logs | Structured logs with rotation |
| 🐳 Docker Ready | Dockerfile + Compose + Nginx |
| ☁️ S3 Ready | Storage abstraction supports AWS S3 |

---

## 🚀 Quick Start

### 1. Clone and set up environment

```bash
git clone <your-repo>
cd matrimony-ai-agent
cp .env.example .env
```

### 2. Configure `.env`

```env
# Required
FLASK_SECRET_KEY=your-very-long-random-secret-key
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GROQ_API_KEY=gsk_your_groq_key

# Optional (defaults shown)
FLASK_ENV=development
PORT=5000
DATABASE_URL=sqlite:///./data/matrimony.db
```

### 3. Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project → **APIs & Services → Credentials**
3. **Create Credentials → OAuth 2.0 Client ID → Web Application**
4. Add Authorized Redirect URIs:
   - Development: `http://localhost:5000/auth/google/callback`
   - Production: `https://yourdomain.com/auth/google/callback`
5. Copy **Client ID** and **Client Secret** to `.env`

### 4. Install dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Optional: install tesseract for image OCR
# Ubuntu/Debian: sudo apt-get install tesseract-ocr
# macOS:         brew install tesseract
# Windows:       https://github.com/UB-Mannheim/tesseract/wiki
```

### 5. Run

```bash
# Development
python app.py

# Production (gunicorn)
gunicorn -w 4 -b 0.0.0.0:5000 "app:create_app()"
```

Visit [http://localhost:5000](http://localhost:5000)

---

## 🐳 Docker Deployment

```bash
# Build and start all services (Flask + Nginx + Redis)
docker-compose up -d

# View logs
docker-compose logs -f web

# Stop
docker-compose down
```

---

## 🗂️ Project Structure

```
matrimony-ai-agent/
├── app.py                      ← Flask application factory
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── .env / .env.example
│
├── auth/
│   └── google_oauth.py         ← Google OAuth 2.0 handler
│
├── config/
│   └── settings.py             ← Dev / Prod / Test configs from .env
│
├── core/                       ← Original AI processing (unchanged)
│   ├── extractor.py            ← Groq LLM extraction
│   ├── exporter.py             ← SQL/CSV/Excel/JSON export
│   ├── processor.py            ← Background job processor
│   ├── reader.py               ← PDF/DOCX/TXT readers
│   ├── sql_generator.py        ← SQL INSERT builder
│   └── logger.py               ← Structured rotating logger
│
├── middleware/
│   └── security.py             ← CSRF, rate limiting, security headers
│
├── migrations/
│   └── schema.sql              ← Raw SQL schema
│
├── models/
│   └── database.py             ← SQLAlchemy: User + Upload models
│
├── routes/
│   ├── api.py                  ← /api/* endpoints
│   ├── auth.py                 ← /auth/* OAuth endpoints
│   └── main.py                 ← Page routes
│
├── services/
│   ├── model_router.py         ← Intelligent file-type → processor routing
│   ├── storage.py              ← Local + S3-ready file storage
│   └── upload_service.py       ← Full pipeline orchestrator
│
├── templates/
│   ├── landing.html            ← Login / landing page
│   ├── index.html              ← Main dashboard
│   └── errors/404.html
│
├── static/                     ← CSS, JS, images
├── input/                      ← Uploaded files (by category)
│   ├── pdf/ image/ docx/ txt/
├── output/                     ← Exported results
├── logs/                       ← app.log, error.log
└── data/                       ← SQLite DB (dev)
```

---

## 🤖 Intelligent Model Routing

| File Type | Processor | Method |
|---|---|---|
| `.pdf` | `PdfProcessor` | PyMuPDF text extraction |
| `.docx` / `.doc` | `DocxProcessor` | python-docx paragraph extraction |
| `.txt` | `TxtProcessor` | Direct file read |
| `.jpg` `.png` etc. | `ImageProcessor` | Tesseract OCR → LLM |

**Adding a new file type** (e.g., spreadsheet):
```python
# In services/model_router.py
class XlsxProcessor(BaseProcessor):
    display_name = "Excel Processor"
    def extract_pages(self, file_path, max_chars=5000):
        # read xlsx, return [(1, text)]
        ...

ROUTER_MAP["xlsx"] = XlsxProcessor
```

---

## 🗄️ Database Schema

### `users`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `google_id` | VARCHAR(128) | Unique Google sub |
| `email` | VARCHAR(256) | Unique |
| `name` | VARCHAR(256) | Display name |
| `profile_image` | TEXT | Google profile URL |
| `is_verified` | BOOLEAN | Email verified |
| `is_active` | BOOLEAN | Account active |
| `created_at` | DATETIME | |
| `last_login` | DATETIME | Updated on each login |

### `uploads`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | FK → users | |
| `original_filename` | VARCHAR(512) | |
| `stored_filename` | VARCHAR(512) | UUID-based safe name |
| `file_type` | VARCHAR(16) | image/pdf/docx/txt |
| `file_path` | TEXT | Local path or S3 key |
| `job_id` | VARCHAR(64) | Links to in-memory job |
| `status` | VARCHAR(32) | pending/running/done/failed |
| `model_used` | VARCHAR(128) | Groq model name |
| `processed_output` | TEXT | JSON blob of profiles |
| `profiles_count` | INTEGER | |
| `created_at` | DATETIME | |
| `completed_at` | DATETIME | |

---

## 🛡️ Security Features

- **Google OAuth only** — no passwords stored
- **CSRF protection** on all POST routes via Flask-WTF
- **Rate limiting** — 60 req/min per IP (in-memory; swap for Redis)
- **File validation** — extension + size limits per type
- **Secure filenames** — `werkzeug.utils.secure_filename` + UUID prefix
- **Security headers** — CSP, X-Frame-Options, HSTS-ready
- **Input sanitization** — null-byte stripping on all form inputs
- **Relative redirect guard** — prevents open redirect on OAuth callback

---

## ☁️ Switching to S3 Storage

```env
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_S3_BUCKET=your-bucket-name
AWS_S3_REGION=us-east-1
```

No code changes needed — `StorageService` handles routing automatically.

---

## 📦 Production Checklist

- [ ] Set `FLASK_ENV=production` in `.env`
- [ ] Set a strong `FLASK_SECRET_KEY` (32+ random chars)
- [ ] Set `SESSION_COOKIE_SECURE=true` (HTTPS only)
- [ ] Add your domain to Google OAuth Redirect URIs
- [ ] Use PostgreSQL instead of SQLite (`DATABASE_URL=postgresql://...`)
- [ ] Set up Nginx with SSL (Let's Encrypt)
- [ ] Configure log rotation (logrotate)
- [ ] Replace in-memory job store with Redis + Celery for multi-worker

---

## 🔑 API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | — | Landing / redirect to dashboard |
| GET | `/auth/google` | — | Start Google OAuth flow |
| GET | `/auth/google/callback` | — | OAuth callback |
| GET | `/auth/logout` | ✓ | Sign out |
| GET | `/auth/me` | ✓ | Current user JSON |
| POST | `/api/upload` | ✓ | Upload file, start job |
| GET | `/api/status/<job_id>` | ✓ | Poll job status |
| POST | `/api/export/<job_id>` | ✓ | Download results |
| POST | `/api/chat` | ✓ | Chat with AI |
| GET | `/api/uploads` | ✓ | Upload history |
| GET | `/api/fields` | — | Default field list |
