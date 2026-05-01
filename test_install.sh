#!/usr/bin/env bash
set -euo pipefail

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "=== Checking dependencies ==="

command -v python3 >/dev/null 2>&1 && ok "python3 found" || fail "python3 not found"
command -v pdftotext >/dev/null 2>&1 && ok "pdftotext found" || fail "pdftotext not found (install: sudo apt install poppler-utils)"

echo ""
echo "=== Running parser on test_statements/ ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMPDIR=$(mktemp -d /tmp/ccard_test_XXXXXX)
trap "rm -rf $TMPDIR" EXIT

# Set up a temp workspace pointing at test_statements
ln -s "$SCRIPT_DIR/test_statements" "$TMPDIR/statements"
cp "$SCRIPT_DIR/parse_statements.py" "$TMPDIR/"

OUTPUT=$(cd "$TMPDIR" && python3 parse_statements.py 2>&1) && RUN_OK=true || RUN_OK=false

if $RUN_OK; then
    ok "parse_statements.py exited cleanly"
else
    fail "parse_statements.py exited with error"
    echo "$OUTPUT"
fi

TX_COUNT=$(python3 -c "
import sqlite3
try:
    n = sqlite3.connect('$TMPDIR/transactions.db').execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
    print(n)
except Exception as e:
    print(0)
")

if [ "$TX_COUNT" -gt 0 ] 2>/dev/null; then
    ok "$TX_COUNT transactions imported from test_statements/"
else
    fail "No transactions imported"
fi

[ -s "$TMPDIR/index.html" ] && ok "index.html generated ($(wc -c < "$TMPDIR/index.html") bytes)" || fail "index.html is empty or missing"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
