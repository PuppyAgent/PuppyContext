#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# PuppyOne CLI Integration Tests
#
# Prerequisites:
#   1. Backend running at localhost:9090
#   2. CLI logged in:  node bin/puppyone.js auth login -e <email> -p <pass>
#   3. Active project:  node bin/puppyone.js project use <name>
#
# Usage:
#   bash cli/tests/run.sh            # run all tests
#   bash cli/tests/run.sh access     # run only access tests
#   bash cli/tests/run.sh --verbose  # show command output
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLI="node $CLI_DIR/bin/puppyone.js"

VERBOSE=false
FILTER=""
PASSED=0
FAILED=0
SKIPPED=0
FAILURES=""

for arg in "$@"; do
  case "$arg" in
    --verbose|-v) VERBOSE=true ;;
    *) FILTER="$arg" ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

run() {
  if $VERBOSE; then
    "$@" 2>&1
  else
    "$@" > /dev/null 2>&1
  fi
}

assert_exit() {
  local expected=$1; shift
  local desc="$1"; shift
  local actual=0
  "$@" > /tmp/puppyone_test_out 2>&1 || actual=$?
  if [ "$actual" -eq "$expected" ]; then
    echo -e "  ${GREEN}✓${NC} $desc"
    PASSED=$((PASSED + 1))
  else
    echo -e "  ${RED}✗${NC} $desc (expected exit=$expected, got=$actual)"
    if $VERBOSE; then
      cat /tmp/puppyone_test_out | head -5 | sed 's/^/    /'
    fi
    FAILED=$((FAILED + 1))
    FAILURES="$FAILURES\n  - $desc"
  fi
}

assert_output_contains() {
  local pattern="$1"; shift
  local desc="$1"; shift
  local output
  output=$("$@" 2>&1) || true
  if echo "$output" | grep -qi "$pattern"; then
    echo -e "  ${GREEN}✓${NC} $desc"
    PASSED=$((PASSED + 1))
  else
    echo -e "  ${RED}✗${NC} $desc (output missing: \"$pattern\")"
    if $VERBOSE; then
      echo "$output" | head -5 | sed 's/^/    /'
    fi
    FAILED=$((FAILED + 1))
    FAILURES="$FAILURES\n  - $desc"
  fi
}

assert_json_success() {
  local desc="$1"; shift
  local output
  output=$("$@" --json 2>&1) || true
  if echo "$output" | grep -q '"success": true'; then
    echo -e "  ${GREEN}✓${NC} $desc"
    PASSED=$((PASSED + 1))
  else
    echo -e "  ${RED}✗${NC} $desc (JSON response not success)"
    if $VERBOSE; then
      echo "$output" | head -10 | sed 's/^/    /'
    fi
    FAILED=$((FAILED + 1))
    FAILURES="$FAILURES\n  - $desc"
  fi
}

skip() {
  local desc="$1"
  echo -e "  ${YELLOW}○${NC} $desc (skipped)"
  SKIPPED=$((SKIPPED + 1))
}

section() {
  local name="$1"
  if [ -n "$FILTER" ] && [ "$FILTER" != "$name" ]; then
    return 1
  fi
  echo -e "\n${CYAN}━━━ $name ━━━${NC}"
  return 0
}

# ── Pre-flight checks ───────────────────────────────────────

echo -e "${CYAN}PuppyOne CLI Integration Tests${NC}"
echo "CLI: $CLI"

# Check backend is reachable (health may return 503 if MCP remote is down, but that's OK)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:9090/health 2>/dev/null || true)
if [ -z "$HTTP_CODE" ] || [ "$HTTP_CODE" = "000" ]; then
  echo -e "${RED}ERROR: Backend not running at localhost:9090${NC}"
  echo "Start it: cd backend && uv run uvicorn src.main:app --host 0.0.0.0 --port 9090 --reload"
  exit 1
fi
echo -e "${GREEN}✓${NC} Backend reachable (HTTP $HTTP_CODE)"

# Check CLI can execute
if ! $CLI --version > /dev/null 2>&1; then
  echo -e "${RED}ERROR: CLI cannot execute${NC}"
  exit 1
fi
echo -e "${GREEN}✓${NC} CLI executable (v$($CLI --version 2>&1))"

# Check auth state. `auth whoami` intentionally prints cached local identity
# even when the saved token is expired, so the harness checks token freshness
# directly before deciding whether auth-required integration tests can run.
AUTH_OUT=$($CLI auth whoami 2>&1) || true
SESSION_EXPIRED=false
if echo "$AUTH_OUT" | grep -q "Not logged in"; then
  LOGGED_IN=false
  echo -e "${YELLOW}!${NC} Not logged in — auth-required tests will be skipped"
else
  TOKEN_STATE=$(cd "$CLI_DIR" && node --input-type=module -e '
    import { loadConfig } from "./src/config.js";
    const config = loadConfig();
    if (!config.api_key) {
      console.log("missing");
    } else if (config.token_expires_at && Date.now() / 1000 > config.token_expires_at) {
      console.log("expired");
    } else {
      console.log("valid");
    }
  ' 2>/dev/null || echo "unknown")
  if [ "$TOKEN_STATE" = "expired" ]; then
    LOGGED_IN=false
    SESSION_EXPIRED=true
    echo -e "${YELLOW}!${NC} Saved session expired — auth-required tests will be skipped"
  elif [ "$TOKEN_STATE" = "missing" ]; then
    LOGGED_IN=false
    echo -e "${YELLOW}!${NC} Not logged in — auth-required tests will be skipped"
  else
    LOGGED_IN=true
    echo -e "${GREEN}✓${NC} Authenticated"
  fi
fi

# ═══════════════════════════════════════════════════════════
# Test Suites
# ═══════════════════════════════════════════════════════════

# ── 1. Basic CLI structure ──────────────────────────────────
if section "basic"; then
  assert_exit 0 "puppyone --version" $CLI --version
  assert_exit 0 "puppyone --help" $CLI --help
  assert_output_contains "Usage" "help output contains Usage" $CLI --help

  assert_exit 0 "puppyone auth --help" $CLI auth --help
  assert_exit 0 "puppyone project --help" $CLI project --help
  assert_exit 0 "puppyone template --help" $CLI template --help
  assert_exit 0 "puppyone org --help" $CLI org --help
  assert_exit 0 "puppyone access --help" $CLI access --help
  assert_exit 0 "puppyone chat --help" $CLI chat --help
  assert_exit 0 "puppyone config --help" $CLI config --help
  assert_exit 0 "puppyone fs --help" $CLI fs --help
  assert_exit 0 "puppyone fs ls --help" $CLI fs ls --help
  assert_output_contains "recursive" "fs ls help advertises recursive listing" $CLI fs ls --help
  assert_output_contains "names begin with" "fs ls help advertises dot entries" $CLI fs ls --help
  assert_output_contains "one entry per line" "fs ls help advertises one-column output" $CLI fs ls --help
  assert_output_contains "paths" "fs ls help advertises multi-path support" $CLI fs ls --help
  assert_output_contains "human-readable" "fs ls help advertises human-readable sizes" $CLI fs ls --help
  assert_output_contains "sort by modification time" "fs ls help advertises time sort" $CLI fs ls --help
  assert_output_contains "list directories themselves" "fs ls help advertises directory mode" $CLI fs ls --help
  assert_output_contains "classify" "fs ls help advertises classify mode" $CLI fs ls --help
  assert_exit 0 "puppyone fs cat --help" $CLI fs cat --help
  assert_output_contains "paths" "fs cat help advertises multi-path support" $CLI fs cat --help
  assert_exit 0 "puppyone fs head --help" $CLI fs head --help
  assert_output_contains "first part" "fs head help describes head output" $CLI fs head --help
  assert_output_contains "lines" "fs head help advertises line count" $CLI fs head --help
  assert_exit 0 "puppyone fs tail --help" $CLI fs tail --help
  assert_output_contains "last part" "fs tail help describes tail output" $CLI fs tail --help
  assert_output_contains "bytes" "fs tail help advertises byte count" $CLI fs tail --help
  assert_exit 0 "puppyone fs tree --help" $CLI fs tree --help
  assert_output_contains "Unix tree compatibility" "fs tree help advertises level alias" $CLI fs tree --help
  assert_output_contains "directories only" "fs tree help advertises directory-only mode" $CLI fs tree --help
  assert_output_contains "level <n>" "fs tree help advertises -L level option" $CLI fs tree --help
  assert_exit 0 "puppyone fs find --help" $CLI fs find --help
  assert_output_contains "name <pattern>" "fs find help advertises name expression" $CLI fs find --help
  assert_output_contains "iname" "fs find help advertises case-insensitive name expression" $CLI fs find --help
  assert_output_contains "mindepth" "fs find help advertises mindepth expression" $CLI fs find --help
  assert_output_contains "maxdepth" "fs find help advertises maxdepth expression" $CLI fs find --help
  assert_exit 0 "puppyone fs grep --help" $CLI fs grep --help
  assert_output_contains "regular expression" "fs grep help advertises regular-expression default" $CLI fs grep --help
  assert_output_contains "fixed string" "fs grep help advertises fixed-string mode" $CLI fs grep --help
  assert_output_contains "max-files" "fs grep help advertises file cap" $CLI fs grep --help
  assert_output_contains "files-with-matches" "fs grep help advertises Unix file-list mode" $CLI fs grep --help
  assert_exit 0 "puppyone fs mkdir --help" $CLI fs mkdir --help
  assert_output_contains "parents" "fs mkdir help advertises parents option" $CLI fs mkdir --help
  assert_output_contains "paths" "fs mkdir help advertises multi-path support" $CLI fs mkdir --help
  assert_exit 0 "puppyone fs touch --help" $CLI fs touch --help
  assert_output_contains "empty file" "fs touch help advertises empty-file create" $CLI fs touch --help
  assert_exit 0 "puppyone fs upload --help" $CLI fs upload --help
  assert_output_contains "local source" "fs upload help advertises local source" $CLI fs upload --help
  assert_output_contains "max-depth" "fs upload help advertises recursive max-depth" $CLI fs upload --help
  assert_output_contains "limit" "fs upload help advertises recursive limit" $CLI fs upload --help
  assert_exit 0 "puppyone fs download --help" $CLI fs download --help
  assert_output_contains "local destination" "fs download help advertises local destination" $CLI fs download --help
  assert_output_contains "max-depth" "fs download help advertises recursive max-depth" $CLI fs download --help
  assert_output_contains "limit" "fs download help advertises recursive limit" $CLI fs download --help
  assert_exit 0 "puppyone fs cp --help" $CLI fs cp --help
  assert_output_contains "recursive" "fs cp help advertises recursive copy" $CLI fs cp --help
  assert_output_contains "no-clobber" "fs cp help advertises no-clobber option" $CLI fs cp --help
  assert_output_contains "target-directory" "fs cp help advertises target-directory option" $CLI fs cp --help
  assert_exit 0 "puppyone fs mv --help" $CLI fs mv --help
  assert_output_contains "no-clobber" "fs mv help advertises no-clobber option" $CLI fs mv --help
  assert_output_contains "source path(s)" "fs mv help advertises multi-source support" $CLI fs mv --help
  assert_output_contains "no-target-directory" "fs mv help advertises no-target-directory option" $CLI fs mv --help
  assert_exit 0 "puppyone fs rm --help" $CLI fs rm --help
  assert_output_contains "paths" "fs rm help advertises multi-path support" $CLI fs rm --help
  assert_exit 0 "puppyone fs rmdir --help" $CLI fs rmdir --help
  assert_output_contains "empty directories" "fs rmdir help describes empty-directory removal" $CLI fs rmdir --help
  assert_output_contains "parents" "fs rmdir help advertises parents option" $CLI fs rmdir --help
  assert_exit 0 "fs golden output contract" node "$CLI_DIR/tests/fs_golden.test.mjs"
  assert_exit 1 "puppyone data is not registered" $CLI data ls
  assert_output_contains "unknown command 'data'" "data behaves like an unknown command" $CLI data ls
fi

# ── 2. Auth ──────────────────────────────────────────────────
if section "auth"; then
  if $LOGGED_IN; then
    assert_exit 0 "auth whoami succeeds when logged in" $CLI auth whoami
    assert_output_contains "User" "whoami shows user identity" $CLI auth whoami
  elif $SESSION_EXPIRED; then
    skip "auth whoami live validation (saved session expired)"
  else
    assert_exit 1 "auth whoami fails when not logged in" $CLI auth whoami
    assert_output_contains "Not logged in" "whoami shows login hint" $CLI auth whoami
  fi
fi

# ── 3. Access ─────────────────────────────────────────────────
if section "access"; then
  assert_exit 0 "access providers --help" $CLI access providers --help
  assert_exit 0 "access add --help" $CLI access add --help
  assert_output_contains "agent.*mcp.*sandbox" "access add help describes surface kinds" $CLI access add --help
  assert_exit 0 "import create --help" $CLI import create --help
  assert_exit 0 "synchronize add --help" $CLI synchronize add --help

  if $LOGGED_IN; then
    assert_exit 0 "access providers lists providers" $CLI access providers
    assert_output_contains "agent\|mcp\|sandbox" "access providers contains surface kinds, not sources" $CLI access providers
    assert_exit 0 "Import provider discovery matches server" $CLI import providers
    assert_exit 0 "Synchronize provider discovery matches server" $CLI synchronize providers

    assert_exit 0 "access auth-status gmail does not crash" $CLI access auth-status gmail

    assert_exit 0 "access ls does not crash" $CLI access ls
  else
    skip "access providers (not logged in)"
    skip "access auth-status (not logged in)"
    skip "access ls (not logged in)"
  fi
fi

# ── 4. Project ───────────────────────────────────────────────
if section "project"; then
  if $LOGGED_IN; then
    assert_exit 0 "project ls does not crash" $CLI project ls
    assert_exit 0 "project current does not crash" $CLI project current
  else
    skip "project ls (not logged in)"
    skip "project current (not logged in)"
  fi
fi

# ── 5. Error handling ────────────────────────────────────────
if section "errors"; then
  assert_exit 1 "unknown command exits non-zero" $CLI this-does-not-exist
  assert_exit 1 "access add without args exits non-zero" $CLI access add

  if $LOGGED_IN; then
    assert_exit 1 "access info with bad ID exits non-zero" $CLI access info nonexistent-id-12345
    assert_exit 1 "access rm with bad ID exits non-zero" $CLI access rm nonexistent-id-12345
  else
    skip "access info error handling (not logged in)"
    skip "access rm error handling (not logged in)"
  fi
fi

# ═══════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════

echo ""
echo -e "${CYAN}━━━ Results ━━━${NC}"
echo -e "  ${GREEN}Passed:  $PASSED${NC}"
if [ "$FAILED" -gt 0 ]; then
  echo -e "  ${RED}Failed:  $FAILED${NC}"
  echo -e "${RED}Failures:${NC}$FAILURES"
fi
if [ "$SKIPPED" -gt 0 ]; then
  echo -e "  ${YELLOW}Skipped: $SKIPPED${NC}"
fi
echo ""

if [ "$FAILED" -gt 0 ]; then
  echo -e "${RED}FAIL${NC}"
  exit 1
else
  echo -e "${GREEN}PASS${NC}"
  exit 0
fi
