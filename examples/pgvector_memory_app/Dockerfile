# Image for the decay-engine pgvector demo.
#
# Build context is the REPOSITORY ROOT, so the local library is installed from
# source rather than from an index -- the example always runs against the
# working tree, not a published release.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install the library first so this layer caches independently of the example.
# ".[pgvector]" brings psycopg; the core itself has no dependencies.
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY decay_engine ./decay_engine
RUN pip install --no-cache-dir ".[pgvector]"

# The example's own code. No dependencies beyond the library.
COPY examples/pgvector_memory_app ./demo

WORKDIR /app/demo
ENTRYPOINT ["python", "app.py"]
CMD ["demo"]
