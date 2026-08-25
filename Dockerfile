# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — LedgerFlow FastAPI Backend
# Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller
#
# Build:
#   docker build -t ledgerflow .
#
# Run:
#   docker run -p 8000:8000 ledgerflow
#
# Run with live mock data mounted from host:
#   docker run -p 8000:8000 -v "$(pwd)/data:/app/data" ledgerflow
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: dependency layer ─────────────────────────────────────────────────
# Separate stage so the heavy pip install is cached independently of code changes.
FROM python:3.12-slim AS deps

WORKDIR /install

# Install build tools needed by some Pandas/NumPy wheels, then clean up
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip --quiet \
    && pip install --no-cache-dir --prefix=/install/packages -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Metadata
LABEL maintainer="LedgerFlow Team"
LABEL description="LedgerFlow reconciliation engine — FastAPI backend"
LABEL version="1.0.0"

# Keeps Python from buffering stdout/stderr (essential for clean Docker logs)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy installed packages from the deps stage
COPY --from=deps /install/packages /usr/local

# Copy only the backend source files (see .dockerignore for exclusions)
COPY requirements.txt        ./
COPY ledgerflow_engine.py    ./
COPY generate_mock_data.py   ./
COPY main.py                 ./

# Pre-create the data directories so the container can write outputs
# without needing a mounted volume on first run
RUN mkdir -p data/mock data/output

# ── Non-root user (security best practice) ────────────────────────────────────
RUN addgroup --system appgroup \
    && adduser  --system --ingroup appgroup --no-create-home appuser \
    && chown -R appuser:appgroup /app

USER appuser

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" \
    || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
# --workers 1  keeps state consistent for the in-memory engine
# --no-access-log reduces noise; remove if you need request logging
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
