# Setup

## Requirements

- Python 3.11 or newer
- Docker and Docker Compose (optional, for the Postgres path)
- No Node.js — the frontend is plain HTML, CSS and JavaScript with no build step

## Local install

```bash
git clone https://github.com/mohdabrarbaloch-arch/day-28-barq.git
cd day-28-barq

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev]"
```

## Configuration

```bash
cp .env.example .env
```

Generate a real secret. The app refuses to start in production with the
placeholder value, and warns loudly in development:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))" >> .env
```

The defaults work for local development. `DATABASE_URL` falls back to a SQLite
file in the project root, which is fine for a single process and wrong for
anything else.

## Seed the demo grid

```bash
python -m scripts.seed
```

This creates:

- an admin account, `aisha@barq-example.com` / `barq-demo-2026`
- the `KHI-SOUTH-1` feeder
- six generating units and one battery
- one telemetry reading so the dashboard has a starting point

The script is idempotent. Running it twice replaces nothing and prints
`demo feeder already exists`.

## Run

```bash
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Interactive API docs are at
<http://127.0.0.1:8000/docs>.

## Docker

```bash
cp .env.example .env
# put a real SECRET_KEY in .env
docker compose up --build
```

Postgres 16 comes up alongside the app. The app waits for the health check
before starting, so there is no race on first boot.

To reset the database:

```bash
docker compose down -v
```

## Tests

```bash
pytest -q
```

77 tests, no network access, about four seconds. Each test gets its own SQLite
file in a temporary directory, so nothing leaks between them.

## Linting

```bash
ruff check .
ruff format --check .
```

## Troubleshooting

**`SECRET_KEY must be set`** — you have not created `.env`, or the value is
still the placeholder. Generate one with the command above.

**`email-validator is not installed`** — Pydantic's `EmailStr` needs it. It is
in the `dev` extra; run `pip install -e ".[dev]"` rather than `pip install -e .`.

**Port 8000 already in use** — `uvicorn app.main:app --port 8001`.

**The dashboard shows dashes** — the frontend fetches on load. If the API is
unreachable the KPIs stay empty; check the browser console for the failing
request.
