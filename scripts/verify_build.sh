#!/usr/bin/env bash
# Build decay-engine and verify the result is actually installable and usable.
#
# Checks, in order:
#   1. sdist and wheel build cleanly
#   2. the wheel ships the package and py.typed, and no tests or secrets
#   3. the sdist ships tests and docs, and no secrets
#   4. a core-only install has zero third-party dependencies
#   5. the extras are declared, and resolve
#   6. the test suite passes against the INSTALLED package, from a directory
#      that contains no source tree
#
# Usage: ./scripts/verify_build.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PY="${PYTHON:-python3}"
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 1. build
step "1. Building sdist and wheel"
rm -rf dist build ./*.egg-info
"$PY" -m build >"$WORK/build.log" 2>&1 || { cat "$WORK/build.log"; fail "build failed"; }
WHEEL=$(ls dist/*.whl)
SDIST=$(ls dist/*.tar.gz)
WHEEL_ABS="$REPO/$WHEEL"
pass "wheel: $(basename "$WHEEL")"
pass "sdist: $(basename "$SDIST")"

# ------------------------------------------------------------ 2. wheel contents
step "2. Wheel contents"
WHEEL_FILES=$("$PY" -c "import zipfile,sys; print('\n'.join(zipfile.ZipFile(sys.argv[1]).namelist()))" "$WHEEL")
grep -q '^decay_engine/engine.py$'            <<<"$WHEEL_FILES" || fail "package missing from wheel"
grep -q '^decay_engine/adapters/pgvector.py$' <<<"$WHEEL_FILES" || fail "pgvector adapter missing"
grep -q '^decay_engine/adapters/chroma.py$'   <<<"$WHEEL_FILES" || fail "chroma adapter missing"
grep -q '^decay_engine/py.typed$'             <<<"$WHEEL_FILES" || fail "py.typed missing (PEP 561)"
grep -q 'LICENSE'                             <<<"$WHEEL_FILES" || fail "LICENSE missing from wheel"
grep -q '^tests/'   <<<"$WHEEL_FILES" && fail "tests must not be installed by the wheel"
grep -q '\.env'     <<<"$WHEEL_FILES" && fail "SECRET: .env is inside the wheel"
pass "package, py.typed and LICENSE present; no tests, no .env"

# ------------------------------------------------------------ 3. sdist contents
step "3. Sdist contents"
SDIST_FILES=$("$PY" -c "import tarfile,sys; print('\n'.join(tarfile.open(sys.argv[1]).getnames()))" "$SDIST")
grep -q 'tests/test_scope.py'   <<<"$SDIST_FILES" || fail "tests missing from sdist"
grep -q 'docs/access-tracking'  <<<"$SDIST_FILES" || fail "docs missing from sdist"
grep -q 'LICENSE'               <<<"$SDIST_FILES" || fail "LICENSE missing from sdist"
grep -qE '(^|/)\.env$'          <<<"$SDIST_FILES" && fail "SECRET: .env is inside the sdist"
pass "tests, docs and LICENSE present; no .env"

# -------------------------------------------------------- 4. core-only install
step "4. Core-only install is dependency-free"
"$PY" -m venv "$WORK/core"
"$WORK/core/bin/pip" install -q --upgrade pip >/dev/null
"$WORK/core/bin/pip" install -q "$WHEEL"

THIRD_PARTY=$("$WORK/core/bin/pip" list --format=freeze --exclude pip --exclude setuptools \
              --exclude wheel --exclude pkg-resources | grep -iv '^decay.engine==' || true)
[ -z "$THIRD_PARTY" ] || fail "core install pulled dependencies: $THIRD_PARTY"
pass "no third-party packages installed alongside the core"

# Run from $WORK so the repo's source tree cannot satisfy the import.
cd "$WORK"
"$WORK/core/bin/python" - <<'PYCHECK'
import importlib.util as u
assert u.find_spec("chromadb") is None, "chromadb should not be installed"
assert u.find_spec("psycopg") is None, "psycopg should not be installed"

import decay_engine
from decay_engine.adapters.chroma import ChromaAdapter      # importable driverless
from decay_engine.adapters.pgvector import PgVectorAdapter  # importable driverless
from datetime import timedelta
from decay_engine import DecayEngine, DecayPolicy, MemoryRecord, utcnow
from decay_engine.adapters.memory import InMemoryStore

now = utcnow()
store = InMemoryStore([
    MemoryRecord(id="dead", metadata={
        "created_at": (now - timedelta(days=430)).isoformat(),
        "last_accessed_at": (now - timedelta(days=400)).isoformat(),
        "tenant_id": "acme"}),
    MemoryRecord(id="other", metadata={
        "created_at": (now - timedelta(days=430)).isoformat(),
        "last_accessed_at": (now - timedelta(days=400)).isoformat(),
        "tenant_id": "globex"}),
])
report = DecayEngine(store, DecayPolicy(evict_below=0.1)).sweep(
    dry_run=False, now=now, scope={"tenant_id": "acme"})
assert report.evicted_ids == ["dead"], report.evicted_ids
assert "other" in store, "scope leaked outside its subset"
print(f"  decay-engine {decay_engine.__version__}: scoped sweep works from the install")
PYCHECK
pass "core install imports and sweeps (scoped) with no drivers present"
cd "$REPO"

# ---------------------------------------------------------------- 5. extras
step "5. Extras are declared and resolvable"
METADATA=$("$WORK/core/bin/python" -c "
from importlib.metadata import metadata
m = metadata('decay-engine')
print('EXTRAS:', sorted(m.get_all('Provides-Extra') or []))
print('PYTHON:', m['Requires-Python'])
print('LICENSE:', m['License'])
for r in m.get_all('Requires-Dist') or []:
    print('REQ:', r)
")
echo "$METADATA" | sed 's/^/    /'
grep -q "EXTRAS: \['all', 'chroma', 'dev', 'pgvector'\]" <<<"$METADATA" || fail "extras not declared as expected"
grep -q "PYTHON: >=3.9"  <<<"$METADATA" || fail "requires-python not recorded"
grep -q "REQ: chromadb"  <<<"$METADATA" || fail "chroma extra does not require chromadb"
grep -q "REQ: psycopg"   <<<"$METADATA" || fail "pgvector extra does not require psycopg"
pass "chroma, pgvector, all and dev extras present with the right requirements"

for extra in chroma pgvector all; do
  "$WORK/core/bin/pip" install -q --dry-run "${WHEEL_ABS}[$extra]" \
    >"$WORK/resolve-$extra.log" 2>&1 \
      || { sed 's/^/    /' "$WORK/resolve-$extra.log"; fail "[$extra] does not resolve"; }
  pass "[$extra] resolves"
done

# ------------------------------------- 6. tests run against the installed package
step "6. Test suite against the installed package (no source tree present)"
mkdir -p "$WORK/standalone"
cp -r tests "$WORK/standalone/tests"
find "$WORK/standalone" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
"$WORK/core/bin/pip" install -q pytest

cd "$WORK/standalone"
[ -d decay_engine ] && fail "source tree leaked into the standalone directory"
if "$WORK/core/bin/python" -m pytest tests -q >"$WORK/pytest.log" 2>&1; then
  pass "$(grep -oE '[0-9]+ passed(, [0-9]+ skipped)?' "$WORK/pytest.log" | tail -1) against the installed wheel"
else
  tail -30 "$WORK/pytest.log" | sed 's/^/    /'
  fail "installed-package test run failed"
fi
"$WORK/core/bin/python" -c "
import decay_engine, pathlib
here = pathlib.Path('.').resolve()
assert here not in pathlib.Path(decay_engine.__file__).resolve().parents, \
    'imported decay_engine from the working directory, not the install'
print('  imported from:', decay_engine.__file__)
"
pass "tests exercised the installed package, not a local copy"
cd "$REPO"

printf '\n\033[32mAll build and install checks passed.\033[0m\n'
printf 'Artifacts in dist/ (not published):\n'
ls -1sh dist/ | sed 's/^/  /'
