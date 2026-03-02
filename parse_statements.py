#!/usr/bin/env python3
"""
Credit card statement parser: Wells Fargo + Bank of America PDFs → SQLite + HTML.
"""

import re
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import date, timedelta, datetime

# ─── Configuration ────────────────────────────────────────────────────────────
STATEMENTS_DIR = Path("statements")
DB_PATH = Path("transactions.db")
HTML_PATH = Path("index.html")
LARGE_TRANSACTION_THRESHOLD = 200.0  # highlight rows at or above this amount

# ─── Database ─────────────────────────────────────────────────────────────────

def setup_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS imported_files (
            id INTEGER PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            bank TEXT NOT NULL,
            transaction_date TEXT NOT NULL,
            post_date TEXT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            transaction_type TEXT NOT NULL,
            reference_number TEXT
        );
    """)
    conn.commit()


def already_imported(conn, filename):
    row = conn.execute(
        "SELECT 1 FROM imported_files WHERE filename = ?", (filename,)
    ).fetchone()
    return row is not None


def insert_transactions(conn, filename, rows):
    conn.executemany(
        """INSERT INTO transactions
           (filename, bank, transaction_date, post_date, description,
            amount, transaction_type, reference_number)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.execute(
        "INSERT INTO imported_files (filename, imported_at) VALUES (?, ?)",
        (filename, datetime.now().isoformat()),
    )
    conn.commit()


# ─── PDF text extraction ───────────────────────────────────────────────────────

def extract_text(pdf_path):
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


# ─── Year inference ────────────────────────────────────────────────────────────

def assign_year(month_day, period_start, period_end):
    """Assign a full date to an MM/DD string within the statement period."""
    m, d = int(month_day[:2]), int(month_day[3:])
    for year in [period_end.year, period_start.year]:
        try:
            candidate = date(year, m, d)
            if period_start - timedelta(5) <= candidate <= period_end + timedelta(5):
                return candidate
        except ValueError:
            continue
    # Fallback: use end year
    try:
        return date(period_end.year, m, d)
    except ValueError:
        return date(period_end.year, m, 1)  # last resort


# ─── Wells Fargo parser ────────────────────────────────────────────────────────

# Matches lines with a 4-digit card number prefix (purchases, refunds)
WF_ROW_WITH_CARD = re.compile(
    r'^\s*(\d{4})\s+'           # card last 4
    r'(\d{2}/\d{2})\s+'         # trans date
    r'(\d{2}/\d{2})\s+'         # post date
    r'(\S+)\s+'                 # reference number
    r'(.+?)\s+'                 # description
    r'([\d,]+\.\d{2})\s*$'      # amount (always positive in PDF)
)

# Matches payment/credit lines without a card number prefix
WF_ROW_NO_CARD = re.compile(
    r'^\s{5,}'                  # significant leading whitespace (no card num)
    r'(\d{2}/\d{2})\s+'         # trans date
    r'(\d{2}/\d{2})\s+'         # post date
    r'(\S+)\s+'                 # reference number
    r'(.+?)\s+'                 # description
    r'([\d,]+\.\d{2})\s*$'      # amount
)

# Special lines in fees/interest sections
WF_INTEREST_FEE_ROW = re.compile(
    r'^\s{20,}'                 # heavily indented (no date columns)
    r'(INTEREST CHARGE ON PURCHASES|INTEREST CHARGE ON CASH ADVANCES|'
    r'LATE FEE|ANNUAL FEE|RETURNED PAYMENT FEE)'
    r'\s+([\d,]+\.\d{2})\s*$'
)

WF_PERIOD_RE = re.compile(r'Statement Period\s+(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})')

# Section header strings and their types
WF_SECTIONS = {
    'Payments': ('payment', -1),
    'Other Credits': ('refund', -1),
    'Purchases, Balance Transfers & Other Charges': ('purchase', 1),
    'Fees Charged': ('fee', 1),
    'Interest Charged': ('interest', 1),
}


def parse_wells_fargo(text, filename):
    # Find statement period
    m = WF_PERIOD_RE.search(text)
    if not m:
        print(f"  WARNING: Could not find statement period in {filename}")
        return []

    period_start = datetime.strptime(m.group(1), "%m/%d/%Y").date()
    period_end = datetime.strptime(m.group(2), "%m/%d/%Y").date()

    rows = []
    current_type = None
    current_sign = 1

    for line in text.splitlines():
        # Section detection
        stripped = line.strip()

        # End of section
        if re.search(r'TOTAL .+ FOR THIS PERIOD', stripped, re.IGNORECASE):
            current_type = None
            continue

        # Skip noise
        if re.search(r'NOTICE:|Page \d+ of \d+|^\d{4}\s+YKG\s+', stripped):
            current_type = None
            continue

        # Check section headers (order matters: check longest matches first)
        matched_section = False
        for section_key, (stype, ssign) in WF_SECTIONS.items():
            if stripped == section_key:
                current_type = stype
                current_sign = ssign
                matched_section = True
                break
        if matched_section:
            continue

        if current_type is None:
            continue

        # Handle interest/fee sub-lines
        if current_type in ('fee', 'interest'):
            mi = WF_INTEREST_FEE_ROW.match(line)
            if mi:
                desc = mi.group(1).title()
                amt_str = mi.group(2).replace(',', '')
                amount = float(amt_str) * current_sign
                if amount != 0.0:
                    rows.append((
                        filename, 'Wells Fargo',
                        period_end.isoformat(), None,
                        desc, amount, current_type, None
                    ))
                continue

        # Try with card number first
        mr = WF_ROW_WITH_CARD.match(line)
        if mr:
            _card, trans_md, post_md, ref, desc, amt_str = mr.groups()
            trans_date = assign_year(trans_md, period_start, period_end)
            post_date = assign_year(post_md, period_start, period_end)
            amount = float(amt_str.replace(',', '')) * current_sign
            rows.append((
                filename, 'Wells Fargo',
                trans_date.isoformat(), post_date.isoformat(),
                desc.strip(), amount, current_type, ref
            ))
            continue

        # Try without card number (payments)
        mn = WF_ROW_NO_CARD.match(line)
        if mn:
            trans_md, post_md, ref, desc, amt_str = mn.groups()
            trans_date = assign_year(trans_md, period_start, period_end)
            post_date = assign_year(post_md, period_start, period_end)
            amount = float(amt_str.replace(',', '')) * current_sign
            rows.append((
                filename, 'Wells Fargo',
                trans_date.isoformat(), post_date.isoformat(),
                desc.strip(), amount, current_type, ref
            ))

    return rows


# ─── Bank of America parser ────────────────────────────────────────────────────

BOA_ROW = re.compile(
    r'^\s*(\d{2}/\d{2})\s+'     # trans date
    r'(\d{2}/\d{2})\s+'         # post date
    r'(.+?)\s+'                 # description (lazy)
    r'(\d{4})\s+'               # reference number (last 4 digits)
    r'(\d{4})\s+'               # account last 4
    r'(-?[\d,]+\.\d{2})\s*$'    # amount (signed in PDF)
)

BOA_PERIOD_RE = re.compile(
    r'(\w+)\s+(\d{1,2})\s*[-–]\s*(\w+)\s+(\d{1,2}),\s*(\d{4})'
)

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12
}

BOA_SECTIONS = {
    'Payments and Other Credits': ('payment', -1),
    'Purchases and Adjustments': ('purchase', 1),
    'Fees Charged': ('fee', 1),
    'Interest Charged': ('interest', 1),
}

PAYMENT_KEYWORDS = re.compile(
    r'PAYMENT|AUTOPAY|ONLINE PMT|MOBILE PMT|BILL PMT', re.IGNORECASE
)
# Bank transfer refs start with many digits (e.g., "000001180004735 M0 12100035PAUL WILCOX")
BANK_TRANSFER_RE = re.compile(r'^\d{8,}')


def parse_boa_period(text):
    """Extract statement period from BoA text."""
    for line in text.splitlines():
        stripped = line.strip()
        m = BOA_PERIOD_RE.search(stripped)
        if m:
            start_mon_str, start_day, end_mon_str, end_day, end_year = m.groups()
            start_mon = MONTHS.get(start_mon_str.lower())
            end_mon = MONTHS.get(end_mon_str.lower())
            if start_mon and end_mon:
                end_year_int = int(end_year)
                end_date = date(end_year_int, end_mon, int(end_day))
                # Start year: if start month > end month, it's the previous year
                start_year = end_year_int if start_mon <= end_mon else end_year_int - 1
                start_date = date(start_year, start_mon, int(start_day))
                return start_date, end_date
    return None, None


def parse_bank_of_america(text, filename):
    period_start, period_end = parse_boa_period(text)
    if period_start is None:
        print(f"  WARNING: Could not find statement period in {filename}")
        return []

    rows = []
    current_type = None
    current_sign = 1
    in_transactions = False

    for line in text.splitlines():
        stripped = line.strip()

        # Start of transactions section
        if stripped.startswith('Transactions') and ('Continued' in stripped or stripped == 'Transactions'):
            in_transactions = True
            continue

        # End of transactions section markers
        if stripped.startswith('Interest Charge Calculation') or \
           stripped.startswith('© ') or \
           stripped.startswith('PAYING INTEREST') or \
           stripped.startswith('IMPORTANT INFORMATION'):
            in_transactions = False
            current_type = None
            continue

        # "continued on next page..." is not a section end
        if 'continued on next page' in stripped.lower():
            continue

        # Skip TOTAL lines (end of section)
        if re.search(r'TOTAL .+ FOR THIS PERIOD', stripped, re.IGNORECASE):
            current_type = None
            continue

        # Section detection
        matched_section = False
        for section_key, (stype, ssign) in BOA_SECTIONS.items():
            if stripped == section_key:
                current_type = stype
                current_sign = ssign
                in_transactions = True
                matched_section = True
                break
        if matched_section:
            continue

        if not in_transactions or current_type is None:
            continue

        # Skip header rows and continuation lines without amounts
        if stripped.startswith('Transaction') or stripped.startswith('Date'):
            continue

        mr = BOA_ROW.match(line)
        if not mr:
            continue  # continuation line (e.g., "ARRIVAL DATE...")

        trans_md, post_md, desc, ref, _acct, amt_str = mr.groups()
        trans_date = assign_year(trans_md, period_start, period_end)
        post_date = assign_year(post_md, period_start, period_end)

        raw_amount = float(amt_str.replace(',', ''))

        # Determine sign and type
        if current_type == 'payment':
            # BoA marks payments/credits as negative already
            # Distinguish payment vs refund
            if PAYMENT_KEYWORDS.search(desc) or BANK_TRANSFER_RE.match(desc):
                tx_type = 'payment'
            else:
                tx_type = 'refund'
            # Normalize: credits should be negative in our schema
            amount = -abs(raw_amount)
        elif current_type == 'purchase':
            tx_type = 'purchase'
            # Purchases are positive; but BoA can list adjustments (negative) here too
            amount = raw_amount  # already positive for purchases, negative for adjustments
        else:
            tx_type = current_type
            amount = abs(raw_amount) * current_sign

        rows.append((
            filename, 'Bank of America',
            trans_date.isoformat(), post_date.isoformat(),
            desc.strip(), amount, tx_type, ref
        ))

    return rows


# ─── Website generation ────────────────────────────────────────────────────────

def generate_website(conn):
    transactions = conn.execute("""
        SELECT transaction_date, post_date, bank, description,
               amount, transaction_type, reference_number, filename
        FROM transactions
        ORDER BY transaction_date DESC, id DESC
    """).fetchall()

    # Build JSON data
    tx_list = []
    for row in transactions:
        tx_list.append({
            "date": row[0],
            "post_date": row[1] or "",
            "bank": row[2],
            "description": row[3],
            "amount": row[4],
            "type": row[5],
            "ref": row[6] or "",
            "file": row[7],
        })

    # Monthly summary
    monthly = {}
    for tx in tx_list:
        month_key = tx["date"][:7]  # YYYY-MM
        bank = tx["bank"]
        key = (month_key, bank)
        if key not in monthly:
            monthly[key] = {"purchases": 0.0, "credits": 0.0}
        if tx["amount"] >= 0:
            monthly[key]["purchases"] += tx["amount"]
        else:
            monthly[key]["credits"] += tx["amount"]

    monthly_list = sorted(
        [{"month": k[0], "bank": k[1], **v} for k, v in monthly.items()],
        key=lambda x: (x["month"], x["bank"]),
        reverse=True
    )

    tx_json = json.dumps(tx_list)
    monthly_json = json.dumps(monthly_list)
    threshold = LARGE_TRANSACTION_THRESHOLD

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Credit Card Transactions</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; font-size: 14px; color: #222; background: #f5f5f5; }}
  h1, h2 {{ padding: 12px 16px; background: #1a1a2e; color: #fff; }}
  h2 {{ font-size: 15px; background: #16213e; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 16px; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; align-items: center; }}
  .controls input, .controls select {{ padding: 7px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }}
  .controls input[type=number] {{ width: 110px; }}
  .controls input[type=text] {{ flex: 1; min-width: 200px; }}
  button {{ padding: 7px 14px; background: #0f3460; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }}
  button:hover {{ background: #1a5276; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  th {{ background: #0f3460; color: #fff; padding: 9px 10px; text-align: left; cursor: pointer; white-space: nowrap; user-select: none; }}
  th:hover {{ background: #1a5276; }}
  th .arrow {{ margin-left: 4px; opacity: .6; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #e8e8e8; }}
  tr:hover td {{ background: #f0f4ff; }}
  tr.large-tx td {{ background: #fffde7; }}
  tr.large-tx:hover td {{ background: #fff9c4; }}
  .charge {{ color: #c0392b; font-weight: 500; }}
  .credit {{ color: #27ae60; font-weight: 500; }}
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
  .badge-purchase {{ background: #fce4e4; color: #c0392b; }}
  .badge-payment {{ background: #e4f5e9; color: #1e8449; }}
  .badge-refund {{ background: #e4f0fb; color: #1a5276; }}
  .badge-fee {{ background: #fef5e7; color: #784212; }}
  .badge-interest {{ background: #f9ebea; color: #922b21; }}
  .summary-table {{ width: 100%; border-collapse: collapse; background: #fff; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .summary-table th {{ background: #0f3460; color: #fff; padding: 8px 12px; text-align: left; }}
  .summary-table td {{ padding: 7px 12px; border-bottom: 1px solid #e8e8e8; }}
  .summary-table tr:hover td {{ background: #f0f4ff; }}
  .stat-bar {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }}
  .stat {{ background: #fff; padding: 12px 18px; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .stat-label {{ font-size: 11px; color: #666; text-transform: uppercase; }}
  .stat-value {{ font-size: 20px; font-weight: 700; margin-top: 2px; }}
  #status {{ font-size: 13px; color: #555; padding: 4px 0; }}
  .section {{ margin-bottom: 24px; }}
</style>
</head>
<body>
<h1>Credit Card Transactions</h1>
<div class="container">

<div class="section">
<h2>Monthly Summary</h2>
<table class="summary-table" id="summary-table">
<thead><tr>
  <th>Month</th><th>Bank</th>
  <th>Charges ($)</th><th>Credits ($)</th><th>Net ($)</th>
</tr></thead>
<tbody id="summary-body"></tbody>
</table>
</div>

<div class="section">
<div class="stat-bar" id="stat-bar"></div>
<div class="controls">
  <input type="text" id="search" placeholder="Search description, bank, type..." oninput="applyFilters()">
  <label>Min $<input type="number" id="min-amt" placeholder="0" step="0.01" oninput="applyFilters()"></label>
  <label>Max $<input type="number" id="max-amt" placeholder="" step="0.01" oninput="applyFilters()"></label>
  <select id="bank-filter" onchange="applyFilters()">
    <option value="">All Banks</option>
    <option value="Wells Fargo">Wells Fargo</option>
    <option value="Bank of America">Bank of America</option>
  </select>
  <select id="type-filter" onchange="applyFilters()">
    <option value="">All Types</option>
    <option value="purchase">Purchase</option>
    <option value="payment">Payment</option>
    <option value="refund">Refund</option>
    <option value="fee">Fee</option>
    <option value="interest">Interest</option>
  </select>
  <button onclick="exportCSV()">Export CSV</button>
  <button onclick="clearFilters()">Clear Filters</button>
</div>
<div id="status"></div>
<table id="tx-table">
<thead><tr>
  <th onclick="sortBy('date')" data-col="date">Date <span class="arrow" id="arrow-date">↕</span></th>
  <th onclick="sortBy('bank')" data-col="bank">Bank <span class="arrow" id="arrow-bank">↕</span></th>
  <th onclick="sortBy('description')" data-col="description">Description <span class="arrow" id="arrow-description">↕</span></th>
  <th onclick="sortBy('amount')" data-col="amount">Amount <span class="arrow" id="arrow-amount">↕</span></th>
  <th onclick="sortBy('type')" data-col="type">Type <span class="arrow" id="arrow-type">↕</span></th>
</tr></thead>
<tbody id="tx-body"></tbody>
</table>
</div>

</div>

<script>
const ALL_TX = {tx_json};
const MONTHLY = {monthly_json};
const THRESHOLD = {threshold};

let filtered = [...ALL_TX];
let sortCol = 'date';
let sortAsc = false;

// ── Rendering ──────────────────────────────────────────────────────────────

function fmtAmt(v) {{
  const cls = v >= 0 ? 'charge' : 'credit';
  const sign = v < 0 ? '-' : '';
  return `<span class="${{cls}}">${{sign}}${{Math.abs(v).toFixed(2)}}</span>`;
}}

function badge(type) {{
  return `<span class="badge badge-${{type}}">${{type}}</span>`;
}}

function renderSummary() {{
  const tb = document.getElementById('summary-body');
  tb.innerHTML = MONTHLY.map(r => {{
    const net = r.purchases + r.credits;
    const netClass = net >= 0 ? 'charge' : 'credit';
    return `<tr>
      <td>${{r.month}}</td>
      <td>${{r.bank}}</td>
      <td class="charge">${{r.purchases.toFixed(2)}}</td>
      <td class="credit">(${{Math.abs(r.credits).toFixed(2)}})</td>
      <td class="${{netClass}}">${{net >= 0 ? '' : '-'}}${{Math.abs(net).toFixed(2)}}</td>
    </tr>`;
  }}).join('');
}}

function renderStats() {{
  const totalCharge = filtered.filter(t => t.amount > 0).reduce((s, t) => s + t.amount, 0);
  const totalCredit = filtered.filter(t => t.amount < 0).reduce((s, t) => s + t.amount, 0);
  const net = totalCharge + totalCredit;
  document.getElementById('stat-bar').innerHTML = `
    <div class="stat"><div class="stat-label">Visible Transactions</div><div class="stat-value">${{filtered.length}}</div></div>
    <div class="stat"><div class="stat-label">Total Charges</div><div class="stat-value charge">$${{totalCharge.toFixed(2)}}</div></div>
    <div class="stat"><div class="stat-label">Total Credits</div><div class="stat-value credit">(${{Math.abs(totalCredit).toFixed(2)}})</div></div>
    <div class="stat"><div class="stat-label">Net</div><div class="stat-value ${{net >= 0 ? 'charge' : 'credit'}}">${{net >= 0 ? '' : '-'}}$${{Math.abs(net).toFixed(2)}}</div></div>
  `;
}}

function renderTable() {{
  const tb = document.getElementById('tx-body');
  if (filtered.length === 0) {{
    tb.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:20px;color:#888">No transactions match filters</td></tr>';
    return;
  }}
  tb.innerHTML = filtered.map(t => {{
    const large = Math.abs(t.amount) >= THRESHOLD ? 'large-tx' : '';
    return `<tr class="${{large}}" title="Ref: ${{t.ref || 'N/A'}} | File: ${{t.file}}">
      <td>${{t.date}}</td>
      <td>${{t.bank}}</td>
      <td>${{t.description}}</td>
      <td>${{fmtAmt(t.amount)}}</td>
      <td>${{badge(t.type)}}</td>
    </tr>`;
  }}).join('');
  document.getElementById('status').textContent =
    `Showing ${{filtered.length.toLocaleString()}} of ${{ALL_TX.length.toLocaleString()}} transactions`;
  renderStats();
}}

// ── Sorting ────────────────────────────────────────────────────────────────

function sortBy(col) {{
  if (sortCol === col) {{ sortAsc = !sortAsc; }}
  else {{ sortCol = col; sortAsc = col === 'description'; }}
  document.querySelectorAll('.arrow').forEach(el => el.textContent = '↕');
  document.getElementById('arrow-' + col).textContent = sortAsc ? '↑' : '↓';
  applyFilters();
}}

function sortData(arr) {{
  return [...arr].sort((a, b) => {{
    let va = a[sortCol], vb = b[sortCol];
    if (typeof va === 'string') va = va.toLowerCase(), vb = vb.toLowerCase();
    if (va < vb) return sortAsc ? -1 : 1;
    if (va > vb) return sortAsc ? 1 : -1;
    return 0;
  }});
}}

// ── Filtering ──────────────────────────────────────────────────────────────

function applyFilters() {{
  const search = document.getElementById('search').value.toLowerCase();
  const minAmt = parseFloat(document.getElementById('min-amt').value);
  const maxAmt = parseFloat(document.getElementById('max-amt').value);
  const bank = document.getElementById('bank-filter').value;
  const type = document.getElementById('type-filter').value;

  filtered = ALL_TX.filter(t => {{
    if (search && !t.description.toLowerCase().includes(search) &&
        !t.bank.toLowerCase().includes(search) &&
        !t.type.toLowerCase().includes(search)) return false;
    if (!isNaN(minAmt) && Math.abs(t.amount) < minAmt) return false;
    if (!isNaN(maxAmt) && Math.abs(t.amount) > maxAmt) return false;
    if (bank && t.bank !== bank) return false;
    if (type && t.type !== type) return false;
    return true;
  }});
  filtered = sortData(filtered);
  renderTable();
}}

function clearFilters() {{
  document.getElementById('search').value = '';
  document.getElementById('min-amt').value = '';
  document.getElementById('max-amt').value = '';
  document.getElementById('bank-filter').value = '';
  document.getElementById('type-filter').value = '';
  applyFilters();
}}

// ── CSV Export ─────────────────────────────────────────────────────────────

function exportCSV() {{
  const header = ['Date','Bank','Description','Amount','Type','Reference'];
  const rows = filtered.map(t =>
    [t.date, t.bank, '"' + t.description.replace(/"/g,'""') + '"',
     t.amount.toFixed(2), t.type, t.ref].join(',')
  );
  const csv = [header.join(','), ...rows].join('\\n');
  const blob = new Blob([csv], {{type: 'text/csv'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'transactions.csv';
  a.click();
}}

// ── Init ───────────────────────────────────────────────────────────────────

renderSummary();
applyFilters();
</script>
</body>
</html>
"""

    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"Website written to {HTML_PATH}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)

    pdf_files = sorted(STATEMENTS_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in {STATEMENTS_DIR}/")
        return

    for pdf_path in pdf_files:
        filename = pdf_path.name

        if already_imported(conn, filename):
            print(f"Skipping {filename} (already imported)")
            continue

        print(f"Processing {filename}...", end=" ", flush=True)
        try:
            text = extract_text(pdf_path)
        except subprocess.CalledProcessError as e:
            print(f"ERROR extracting text: {e}")
            continue

        if "WellsFargo" in filename:
            rows = parse_wells_fargo(text, filename)
        elif filename.startswith("eStmt_"):
            rows = parse_bank_of_america(text, filename)
        else:
            print(f"Unknown format, skipping")
            continue

        insert_transactions(conn, filename, rows)
        print(f"{len(rows)} transactions imported")

    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"\nTotal transactions in database: {total}")

    generate_website(conn)
    conn.close()
    print("Done. Open index.html in a browser.")


if __name__ == "__main__":
    main()
