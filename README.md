# Credit Card Statement Parser

Parses Wells Fargo and Bank of America PDF statements into a SQLite database and generates a searchable, sortable static HTML viewer.

## Features

- Parses all transaction types: purchases, payments, refunds, fees, interest
- Idempotent: re-running skips already-imported files
- Signed amounts: positive = charge, negative = credit/payment
- Self-contained `index.html` with:
  - Sortable columns
  - Real-time text search
  - Amount range filter
  - Bank and transaction type filters
  - Monthly summary table
  - Large transaction highlight (≥ $200)
  - CSV export of visible rows

## Install

**1. Install system dependency**

```bash
sudo apt install poppler-utils
```

**2. Create and activate a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

The script uses only stdlib, so no `pip install` step is needed.

## Verify the install

Run the test script against the included sample statements:

```bash
bash test_install.sh
```

Expected output:

```
=== Checking dependencies ===
  PASS: python3 found
  PASS: pdftotext found

=== Running parser on test_statements/ ===
  PASS: parse_statements.py exited cleanly
  PASS: 152 transactions imported from test_statements/
  PASS: index.html generated (45186 bytes)

=== Results: 5 passed, 0 failed ===
```

## Usage

Place PDF statements in a `statements/` directory:

- Wells Fargo: `MMDDYY_WellsFargo.pdf`
- Bank of America: `eStmt_YYYY-MM-DD.pdf`

Then run:

```bash
python3 parse_statements.py
```

This creates `transactions.db` and `index.html`. Open `index.html` in any browser.

## Output

- `transactions.db` — SQLite database with all transactions
- `index.html` — self-contained viewer (no server needed)
