# GreenData AI

Scan a folder, see which files are duplicates, stale or sensitive, preview the savings, and apply only what you approve.
Everything is reversible, and the impact (storage, time, energy, CO2, cost) is reported net of the tool's own overhead.

The project has two parts that work together:

- **Frontend:** the dashboard, `greendata_prototype.html`, served by the backend.
- **Backend:** `greendata.py`, a Python tool with a local API and a command line. Standard library only.

## Live demo

Browser-only demo (sample folder, drag and drop analysis, guided tour): https://YOUR-USERNAME.github.io/YOUR-REPO/

The online demo cannot touch your files. Scanning, compressing and rolling back real folders needs the backend,
which runs on your own computer (Quick start below). That is deliberate: a public server must never read your disk.

## Quick start (web app)

1. Install Python 3.9 or newer (tested on 3.11 and 3.12).
2. Double click `run.bat` (Windows), or run `sh run.sh` (Mac/Linux), or run `python greendata.py serve`.
3. Your browser opens the app. Press **Use sample folder** to look around, or type a folder path under
   **Scan a real folder on this computer** and press **Scan folder**.
4. Review the plan, untick anything you do not want, press **Apply approved changes**.
   Press **Roll back** to undo. Files are moved or compressed, never deleted.

Try it first on the demo folder: `python greendata.py demo ./demo_folder`, then scan `./demo_folder`.
Use the **Energy model what-if** box to see the model on a larger folder, clearly labelled as a what-if.

The server listens on 127.0.0.1 only, rejects other host names, and the page carries a random token,
so other websites cannot call it.

The HTML file also opens on its own (double click). Without the backend it shows the sample folder and
analyzes files you drop in inside the browser, with no real changes. The scan-a-folder box appears only when the backend is running.

## Command line

```
python greendata.py serve                                   # web app (UI + backend)
python greendata.py demo ./demo_folder                      # make a messy sample folder
python greendata.py scan ./demo_folder --keywords sales     # dry run: plan + JSON report, nothing changes
python greendata.py apply ./demo_folder --keywords sales    # asks first, then applies (reversible)
python greendata.py rollback ./demo_folder                  # restores every file, verified by SHA-256
python -m unittest -v                                       # run the tests (14)
```

Useful options: `--region India|USA|EU|Norway`, `--runs 4`, `--threshold 35`, `--weights 35,25,25,15`,
`--acc 2`, `--extrapolate N`, `--apply-near`, `--measure`.

Optional: `pip install zstandard codecarbon`. With zstandard, compression uses zstd instead of lzma.
With codecarbon, `apply --measure` also records measured energy.

## What it does

| Step | How |
|---|---|
| Exact duplicates | SHA-256 of every file. The copy kept is the one without copy/backup/final/old in its name, then the most recently used. |
| Near-duplicates | 3-word shingles, MinHash LSH to find candidates, confirmed with exact Jaccard (default 80%). |
| Sensitive data | Regex for email, Indian mobile, Aadhaar-like 12 digits, PAN. These files are never changed. |
| Relevance | Share of your keywords found in the name and content (0.6 if none given). |
| Data Value Score | 0-100 from relevance, access, recency and small size, minus a duplicate penalty. |
| Recency of relevant data | A file that is relevant (0.7 or more) and used within 90 days is never archived or compressed. |
| Compression | Real codec measured on a 1 MB sample. Files that are already compressed are left alone. |
| Apply | Duplicates go to `.greendata/trash`, archived files to `.greendata/archive`, compressed files stay in place with a `.xz` or `.zst` extension. Each is verified by hash before the original is removed, and files changed since the scan are skipped. |
| Rollback | Restores from the manifest and verifies every hash. Every action is written to `.greendata/audit.log`. |

## Honest limits

- Operating systems rarely record how many times a file was opened, so access frequency is assumed (`--acc`, default 2 per month). "Last used" comes from the access and modified times, and scanning does not change them where the system allows it.
- Energy, CO2 and cost use assumed constants (listed in the JSON report). `apply --measure` records measured energy only if CodeCarbon is installed. Without it you get a CPU-time estimate. Carbon intensity values per region are illustrative.
- On small folders the fixed per-run overhead dominates, so the percentages look tiny. The what-if scale shows the model on larger data and says so.
- Near-duplicates are applied only if you tick them in the UI, or pass `--apply-near`.
- Aadhaar-like detection is a pattern match, so some false positives are expected. It errs on the side of protecting files.
- Nothing is deleted permanently. To free the space for good, empty `.greendata/trash` and `.greendata/archive` yourself.
- Tested on Linux with Python 3.12 using the standard library only. The CodeCarbon and zstandard paths, and Windows/macOS behaviour, have not been run here.
