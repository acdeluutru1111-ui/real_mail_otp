# real-mail-otp — Backend

FastAPI backend for the temporary-inbox + pay-per-read service (single credit
wallet, 1 read = 200 VND). v1 runs as a single replica with a RAM cache (no
Redis), an async upstream HTTP client, and PostgreSQL as the source of truth.

## Requirements

- Python 3.11+
- PostgreSQL (Neon in production, or local PostgreSQL for development)

## Quick Start with venv

```bash
# 1. Navigate to backend directory
cd backend

# 2. Create and activate virtual environment
python -m venv .venv

# Windows:
.venv\Scripts\activate

# Unix/macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and configure environment
cp .env.example .env
# Edit .env with your database URL, secrets, etc.

# 5. Run database migrations
alembic upgrade head

# 6. Start the development server
uvicorn app.main:app --reload
```

## Configuration

Copy `.env.example` to `.env` and fill in real values. **Never commit secrets.**

Key configuration options:
- `DATABASE_URL`: PostgreSQL connection string (async: `postgresql+asyncpg://...`)
- `JWT_SECRET`: Secret key for JWT signing (generate a random 32+ char string)
- `ENCRYPTION_KEY`: Key for encrypting inbox credentials at rest
- `CORS_ORIGINS`: Allowed origins (default: `http://localhost:5173` for Vite)
- `TRUSTED_PROXIES`: IP/CIDR list of trusted reverse proxies (for X-Forwarded-For)

## Database Migrations

```bash
# Apply all migrations
alembic upgrade head

# Create a new migration (after model changes)
alembic revision --autogenerate -m "description"

# Roll back one migration
alembic downgrade -1

# Show current revision
alembic current
```

## Health Endpoints

- **Liveness**: `GET /health/live` — Always 200 if process is running
- **Readiness**: `GET /health/ready` — Checks DB connectivity with `SELECT 1` (2s timeout)

## API Documentation

- Interactive docs: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`
- Authored contract: `backend/openapi/openapi.yaml`

## Error Handling

All errors follow the common envelope format:
```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable message",
    "retryable": false
  },
  "request_id": "uuid"
}
```

Rate limit errors (429) include a `Retry-After` header.

## Project Layout

```
app/
  main.py                 # app factory, middleware, health, router mount
  core/
    config.py             # pydantic-settings, get_settings()
    errors.py             # error taxonomy + exception handler + envelope
    logging.py            # structured JSON logging + redaction
    context.py            # request-id contextvar + middleware
    rate_limit.py         # token bucket rate limiter with TTL cleanup
    security.py           # JWT, password hashing, encryption
  api/
    deps.py               # FastAPI dependencies (auth, rate limit, etc.)
    routes/               # API route handlers
  domain/
    models.py             # DTOs
    services.py           # business logic
    policies.py           # business rules
  integrations/
    http_client.py        # async HTTP client with retry
    smailpro.py           # SmailPro adapter
    sonjj.py              # Sonjj adapter
  repositories/           # data access layer
  cache/                  # RAM cache with TTL
  db/
    models.py             # SQLAlchemy ORM models
    session.py            # async engine + session factory
openapi/openapi.yaml      # authored v1 contract
alembic/                  # database migrations
```

## Testing

```bash
# Run all tests
python -m pytest -q

# Run with verbose output
python -m pytest -v

# Run specific test file
python -m pytest tests/contract/test_adapters_contract.py
```
