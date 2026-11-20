# GreenData AI

**Keep the data that earns its place.**

[![tests](https://github.com/vanshitachoudhary/greendata-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/vanshitachoudhary/greendata-ai/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

GreenData AI scans a folder, finds duplicates, near-duplicates, stale files and sensitive files, scores every file,
and recommends safe, reversible optimizations. It reports the storage, time, energy, CO2 and cost impact, net of the
tool's own overhead. Nothing changes until you approve it, and everything can be rolled back.

**[Live demo](https://vanshitachoudhary.github.io/greendata-ai/)** (runs in your browser with a sample folder and a guided tour, Apply is simulated).
**Live server demo with real Apply and Rollback:** `LIVE_SERVER_LINK_HERE` (see [DEPLOY.md](DEPLOY.md)). It works on a temporary sandbox on the server, never on your computer.
To scan and change your own real folders, run it on your own computer, see [Quick start](#quick-start).

## Features

- **Duplicate detection:** exact copies by SHA-256, near-copies by MinHash LSH confirmed with exact Jaccard similarity.
- **Sensitive data protection:** email, Indian mobile numbers, Aadhaar (checksum validated) and PAN. Detection uses context, so order, invoice and tracking numbers are not mistaken for phone or Aadhaar numbers. These files are never changed.
- **Data Value Score (0-100):** relevance, access, recency and size, with a duplicate penalty. Relevant data used in the last 90 days is never archived.
- **Real optimization:** duplicates are moved aside, stale files are archived, large files are compressed (lzma, or zstd if installed). Files that are already compressed are left alone.
- **Safe by design:** dry run first, per-file approval, hash verification before any original is removed, full rollback, audit log.
- **Impact report:** storage, time, energy, CO2 and cost before and after, per region, net of the scan's own energy.
- **Explainable:** every recommendation comes with a reason, and each score can be opened to see how it was built.
- **Web dashboard and command line**, plus a benchmark harness for measured experiments.
- **Compact dashboard:** four tabs (Overview, Impact, Files, Apply and log) instead of one very long page.
- **CPU utilization:** the dashboard shows CPU time saved per month (modelled), and, with the local backend, the measured CPU use of each scan, split by phase.
- **Adjustable scoring:** pick a scoring focus (balanced, relevance, recency, size) or set the four weights yourself, in the dashboard or with `--weights`.

## How it works

```
scan -> fingerprint (SHA-256) -> exact + near duplicates -> sensitive-data check -> Data Value Score
     -> decision -> dry run -> apply (verified, reversible) -> before/after impact -> rollback if needed
```

| Condition | Action |
|---|---|
| Contains sensitive data | Protect, never changed automatically |
| Exact copy of another file | Deduplicate (moved to `.greendata/trash`) |
| Near-copy of another file | Recommend merge (applied only if you tick it) |
| Low score, not relevant-and-recent | Archive (compressed into `.greendata/archive`) |
| Large and rarely opened | Compress in place |
| Everything else | Keep |

## Quick start

Requires Python 3.9 or newer. No packages to install.

```
python greendata.py serve
```

Your browser opens the dashboard. Use **Use sample folder** to look around, or type a folder path under
**Scan a real folder on this computer**. Review the plan, untick anything you do not want, and press
**Apply approved changes**. **Roll back** restores everything.

On Windows you can also double click `run.bat`. To try it on safe data first:

```
python greendata.py demo demo_folder
```

### Command line

```
python greendata.py scan  ./folder --keywords sales,invoice    # dry run, writes a JSON report
python greendata.py apply ./folder --keywords sales,invoice    # asks first, then applies (reversible)
python greendata.py rollback ./folder                          # restores every file, verified by SHA-256
python -m unittest -v                                          # 47 tests
```

Options: `--region India|USA|EU|Norway`, `--runs 4`, `--threshold 35`, `--weights 35,25,25,15`, `--acc 2`,
`--extrapolate N`, `--apply-near`, `--measure`. Optional extras: `pip install zstandard codecarbon`
(zstd compression, and measured energy with `apply --measure`).

## Safety model

- Dry run by default. Apply is a separate, explicit step with per-file approval.
- Before an original is removed, the compressed copy is decompressed and its SHA-256 is checked. Files that changed since the scan are skipped.
- A file is never overwritten. If `report.csv.xz` already exists next to `report.csv`, that file is skipped.
- Nothing is permanently deleted. Moved and archived files live in `.greendata/` until you remove them.
- Every change is written to a journal before it happens. If the program is killed half way, rollback still finds every file.
- Rollback restores each file from what is really on disk, verifies its SHA-256, and puts back the original modification time and permissions. If one file is blocked (for example a new file now sits at its path) the rest are restored and you can run rollback again later.
- Every run has its own id and its own record. Only one apply or rollback runs at a time per folder, so two clicks cannot corrupt each other.
- The result is reported honestly: compression and archiving free disk space immediately. Exact and near copies are moved to `.greendata/trash` and stay on disk until you delete that folder (after that, rollback cannot bring them back). The apply result shows both numbers.
- Every action is written to `.greendata/audit.log`.
- The local server listens on 127.0.0.1 only, rejects other host names, and the page carries a random token, so other websites cannot call it.

## Evaluation

`benchmark.py` runs measured experiments on labelled synthetic data (known copies, near-copies, sensitive files, decoys,
stale and important files) or on a real folder, next to two simple baselines. `PAPER_GUIDE.md` explains how to use it for a paper.

```
python benchmark.py synthetic --sizes 100,500,2000 --runs 3 --out results
python benchmark.py real --dir path/to/dataset --sample-audit 50
```

The benchmark writes `report.md` / `report.html` (short summary with a CPU usage chart), `results_detailed.md` (all tables) and raw `results.csv` / `results.json`.

Preliminary run on synthetic data (Linux, 1 CPU, Python 3.12, mean over 3 seeds, 2000 files, 12 MB). Rerun on your own machine for your own numbers.

| Metric | GreenData AI | Hash-only dedup | Compress-all |
|---|---|---|---|
| Storage saved | 22.0% | 9.3% | 28.6% |
| Protected files changed (of 240) | 0 | not protected | 240 |
| Exact duplicates, precision / recall | 1.00 / 1.00 | | |
| Near-duplicates, precision / recall | 0.985 / 0.985 | | |
| CPU use while scanning and applying | about 99% of one core (finding duplicates is 52% of the CPU time) | | |
| Sensitive files, precision / recall | 1.00 / 1.00 (on our decoys) | | |
| Rollback restored every byte | 3 of 3 runs (9 of 9 over all sizes) | | |

Honest reading: compress-all saves more space but changes every file, including protected ones. Sensitive-file precision was 0.61 in our first run, because order and tracking numbers look like phone and Aadhaar numbers. After adding context rules it is 1.00 on the same decoys (recall stays 1.00). The decoys were written by us, so real data will have other look-alikes and the real precision will be lower. The tool is still slower than the baselines (about 9 s to scan 2000 files, down from about 15 s after switching to a bottom-k MinHash sketch). See the limitations below.
Energy, CO2 and cost shown in the dashboard come from assumed constants and are estimates, not measurements.
Full tables are in [`results_example/`](results_example/).

## Project structure

```
greendata.py                 backend: scanner, scoring, apply, rollback, local web server, CLI
greendata_prototype.html     dashboard with tabs and CPU card (served by the backend, also works standalone)
hosted_server.py             public server mode: same dashboard, per-visitor sandbox, real Apply and Rollback
hosted_ui.html               extra buttons and notice injected by hosted_server.py (create demo folder, upload small files)
Dockerfile, render.yaml      one-step deployment (see DEPLOY.md)
test_hosted.py               9 tests for the hosted mode (isolation, path safety, real apply and rollback)
benchmark.py                 measured experiments
test_greendata.py            18 tests (unit, API, security, CPU and weights, benchmark smoke test)
test_pii.py                  8 tests for context-aware phone, Aadhaar and PAN detection
test_apply_rollback.py       12 tests that try to break apply and rollback (hard crash, parallel runs, existing files, retry, exact restore)
PAPER_GUIDE.md               how to produce and report results
docs/index.html              copy of the dashboard for GitHub Pages
results_example/             example benchmark output (report, detailed tables, CSV, JSON, CPU chart)
.github/workflows/tests.yml  CI on Linux and Windows
```

## Limitations

- Access frequency is assumed (`--acc`, default 2 opens per month) because file systems do not record it. "Last used" comes from file times.
- Energy, CO2 and cost use assumed constants and illustrative regional carbon intensities. `--measure` records measured energy only if CodeCarbon is installed.
- CPU time per month in the demo dashboard is modelled (one busy core per run). Measured CPU use is shown only when the local backend scans a real folder. Whole-machine CPU needs `psutil`; without it the 1-minute load average is used (not available on Windows).
- Scoring weights are set by the user (presets or sliders). They are not learned from the task. There is no trained machine learning model: "AI" here means automated, explainable decision rules (hashing, MinHash similarity, pattern checks and a weighted score).
- Sensitive-data detection is rule based, not a trained model. A phone number needs context (a phone word before it, a phone column in the header, a +91 or 98765 43210 format, or a list of at least 5 numbers). A single bare 10 digit number with no context is not flagged, so it can be missed. Names, addresses and free-text personal details are not detected.
- The folder lock works inside one program. Two separate programs (for example the command line and the web app) working on the same folder at the same moment are not coordinated.
- Near-duplicate search is pure Python and slow on large folders. Relevance is keyword based.
- Near-duplicates are applied only if you tick them. Small folders show tiny percentages because fixed per-run overhead dominates; `--extrapolate` gives a labelled what-if.
- Tested on Linux with the standard library only. CodeCarbon, zstandard and macOS paths have not been run.

## Roadmap

- Faster near-duplicate search and embedding-based relevance
- Caching and incremental processing
- Cloud storage connectors
- A downstream energy experiment (train on original vs optimized data, measured with CodeCarbon)

## License

MIT, see [LICENSE](LICENSE).
