# filing_monitor

Tracks bill filings for the Texas Legislature's 90th Regular Session and sorts them into policy categories.

## How it works

`monitor.py` (standard library only) reads the Legislature's public FTP server, `ftp.legis.state.tx.us/bills/90R/`:

- `billhistory/` gives each bill's caption, authors, filed date, last action and any TLO-assigned subjects.
- `billtext/html/` gives the introduced text, once it is posted.

Each bill is categorized from its full text when the text exists and from its caption when it doesn't. A bill first seen with only a caption is re-scored on the run where its text appears. Files are skipped when their FTP timestamp and size haven't changed, so routine runs only fetch what is new.

The classifier scores every bill against the taxonomy in `CATEGORIES` at the top of `monitor.py`. Three signals feed the score: which statutory codes the text amends (Election Code, Penal Code, Water Code and so on), phrase matches in the body, and phrase matches in the caption, which count triple. The top category is the primary label, close runners-up are kept as secondary labels, and each record stores the matched terms as `evidence`. After editing the taxonomy, bump `CLASSIFIER_VERSION` and every bill is re-scored on the next run.

## Output

- `docs/data/bills.json` one record per bill
- `docs/data/meta.json` totals and category counts
- `docs/data/changes.json` rolling log of new filings, text postings and caption changes
- `docs/index.html` static dashboard reading those files

## Running

    python monitor.py                 # live
    python monitor.py --all-types     # also HCR/SCR/HR/SR
    python monitor.py --source tests/fixtures --out /tmp/out   # offline, synthetic fixtures

The GitHub Action in `.github/workflows/monitor.yml` runs every 15 minutes and commits any changes. To see the dashboard, enable GitHub Pages for the `docs/` folder on `main`.
