# Measured results

Environment: Python 3.12.3, Linux-6.18.44-fc-v470-x86_64-with-glibc2.39, 1 CPUs, codec .xz, CodeCarbon not installed (energy is a CPU-time estimate), 2026-11-15 06:52.
Each cell is mean ± standard deviation over 3 run(s) with different random seeds.

## Table 1. Detection quality (against ground truth)

| Files | Exact dup. P | R | Near dup. P | R | Sensitive P | R | Decoys flagged |
|---|---|---|---|---|---|---|---|
| 100 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.963 ± 0.052 | 1.000 ± 0.000 | 0.600 ± 0.000 | 1.000 ± 0.000 | 4.0 ± 0.0 of 4 |
| 500 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.984 ± 0.012 | 0.983 ± 0.012 | 0.612 ± 0.010 | 1.000 ± 0.000 | 19.0 ± 0.8 of 20 |
| 2000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.985 ± 0.006 | 0.985 ± 0.008 | 0.613 ± 0.006 | 1.000 ± 0.000 | 75.7 ± 1.9 of 80 |

## Table 2. Safety

| Files | Protected files (sensitive + important) | GreenData changed | Compress-everything baseline changed | Different files merged as near-dup. | Stale files archived | Incompressible files wasted |
|---|---|---|---|---|---|---|
| 100 | 12.0 ± 0.0 | 0.0 ± 0.0 | 12.0 ± 0.0 | 0.3 ± 0.5 | 13.0 ± 0.0 of 13 | 0.0 ± 0.0 |
| 500 | 60.0 ± 0.0 | 0.0 ± 0.0 | 60.0 ± 0.0 | 0.7 ± 0.5 | 65.0 ± 0.0 of 65 | 0.0 ± 0.0 |
| 2000 | 240.0 ± 0.0 | 0.0 ± 0.0 | 240.0 ± 0.0 | 2.3 ± 0.9 | 260.0 ± 0.0 of 260 | 0.0 ± 0.0 |

## Table 3. Storage saved and time

| Files | Size (MB) | GreenData saved % | Hash-only dedup saved % | Compress-all saved % | GreenData scan s | apply s | Hash-only s | Compress-all s |
|---|---|---|---|---|---|---|---|---|
| 100 | 0.6 | 21.4 ± 0.9 | 9.0 ± 0.3 | 27.3 ± 0.5 | 0.70 ± 0.05 | 0.08 ± 0.01 | 0.00 ± 0.00 | 0.02 ± 0.00 |
| 500 | 3.0 | 22.1 ± 0.3 | 9.4 ± 0.2 | 28.8 ± 0.6 | 3.57 ± 0.12 | 0.38 ± 0.04 | 0.01 ± 0.00 | 0.07 ± 0.01 |
| 2000 | 12.3 | 22.0 ± 0.2 | 9.3 ± 0.0 | 28.6 ± 0.2 | 12.34 ± 0.72 | 1.36 ± 0.08 | 0.02 ± 0.00 | 0.22 ± 0.02 |

## Table 4. Energy of running the tool, and rollback

| Files | CPU s | Energy (kWh) estimated from CPU time | Energy (kWh) measured by CodeCarbon | Rollback restored every byte |
|---|---|---|---|---|
| 100 | 0.77 ± 0.06 | 0.000003 ± 0.000000 | n/a | 3 of 3 |
| 500 | 3.90 ± 0.12 | 0.000016 ± 0.000000 | n/a | 3 of 3 |
| 2000 | 13.64 ± 0.64 | 0.000057 ± 0.000003 | n/a | 3 of 3 |

Notes: hash-only dedup and compress-all are our own simple baselines, not third-party tools. The 'Compress-everything baseline' changes every file, so it touches every protected file by construction. Energy from CPU time assumes 15 W per busy core and is an estimate. The tool's modelled monthly energy saving (an assumption-based figure) is stored in results.csv as model_energy_saved_kwh_month and must not be reported as measured.
