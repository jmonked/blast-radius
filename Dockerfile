# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the organisers labeled every commit in, so the
# coverage map built below sees the same Python, SQLite and packages.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

# Same packages the tests were labeled with, plus coverage for the import pass.
COPY requirements.lock /build/requirements.lock
RUN pip install -q -r /build/requirements.lock coverage==7.10.7

# sqlglot at the pinned commit (GitHub archive of that exact commit).
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY tools /build/tools
RUN python /build/tools/fetch_sqlglot.py ${SQLGLOT_COMMIT} /opt/sqlglot

# Build the test-impact map: run the whole suite once with tools/mapper_plugin.py
# recording, per test, the lines it executes, the files it opens and the
# sqlglot classes it instantiates. If anything here fails or runs too long,
# the map built the same way outside Docker (tools/fallback-map.marshal) is used.
RUN mkdir -p /build/out /app && cd /opt/sqlglot \
 && (SKIP_INTEGRATION=0 MAP_OUT=/build/out SQLGLOT_ROOT=/opt/sqlglot \
     PYTHONPATH=/build/tools:/opt/sqlglot \
     timeout 420 python -m pytest -p mapper_plugin -p no:cacheprovider -q \
       -n 4 --dist loadfile tests/ > /build/pytest.log 2>&1; \
     tail -n 3 /build/pytest.log) \
 && ((python /build/tools/import_pass.py /opt/sqlglot /build/out/import.json \
      && python /build/tools/build_map.py /opt/sqlglot /build/out /app/map.marshal) \
     || (echo "map build failed: using the committed fallback map" \
         && cp /build/tools/fallback-map.marshal /app/map.marshal)) \
 && rm -rf /build/out \
 && find /opt/sqlglot -name __pycache__ -prune -exec rm -rf {} + \
 && chmod -R a+rX /opt/sqlglot /app

COPY selector.py pack.py /app/
RUN python -m compileall -q /app && chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
