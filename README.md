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

## Requirements

- Python 3 (stdlib only)
- `pdftotext` from poppler-utils

```bash
sudo apt install poppler-utils
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
