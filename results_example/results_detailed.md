# Detailed tables

Mean ± standard deviation over the runs. P = precision (flagged files that were right), R = recall (real ones that were found).
Environment: Python 3.12.3, Linux-6.18.44-fc-v553-x86_64-with-glibc2.39, 1 CPUs, codec .xz, CodeCarbon not installed (energy is a CPU-time estimate), 2026-11-17 07:45.

## Detection quality

| Files | Exact dup. P | R | Near dup. P | R | Sensitive P | R | Look-alike numbers flagged |
|---|---|---|---|---|---|---|---|
| 100 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.963 ± 0.052 | 1.000 ± 0.000 | 0.600 ± 0.000 | 1.000 ± 0.000 | 4.0 ± 0.0 of 4 |
| 500 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.984 ± 0.012 | 0.983 ± 0.012 | 0.612 ± 0.010 | 1.000 ± 0.000 | 19.0 ± 0.8 of 20 |
| 2000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.985 ± 0.006 | 0.985 ± 0.008 | 0.613 ± 0.006 | 1.000 ± 0.000 | 75.7 ± 1.9 of 80 |

## Safety

| Files | Protected files | GreenData changed | Compress-everything changed | Different files merged | Stale files archived | Incompressible files wasted |
|---|---|---|---|---|---|---|
| 100 | 12.0 ± 0.0 | 0.0 ± 0.0 | 12.0 ± 0.0 | 0.3 ± 0.5 | 13.0 ± 0.0 of 13 | 0.0 ± 0.0 |
| 500 | 60.0 ± 0.0 | 0.0 ± 0.0 | 60.0 ± 0.0 | 0.7 ± 0.5 | 65.0 ± 0.0 of 65 | 0.0 ± 0.0 |
| 2000 | 240.0 ± 0.0 | 0.0 ± 0.0 | 240.0 ± 0.0 | 2.3 ± 0.9 | 260.0 ± 0.0 of 260 | 0.0 ± 0.0 |

## Storage and time

| Files | MB | GreenData saved % | Exact-copies-only saved % | Compress-all saved % | Scan s | Apply s | Exact-copies-only s | Compress-all s |
|---|---|---|---|---|---|---|---|---|
| 100 | 0.6 | 21.4 ± 0.9 | 9.0 ± 0.3 | 27.3 ± 0.5 | 0.74 ± 0.03 | 0.08 ± 0.00 | 0.00 ± 0.00 | 0.02 ± 0.00 |
| 500 | 3.0 | 22.1 ± 0.3 | 9.4 ± 0.2 | 28.8 ± 0.6 | 3.61 ± 0.07 | 0.36 ± 0.02 | 0.01 ± 0.00 | 0.07 ± 0.00 |
| 2000 | 12.3 | 22.0 ± 0.2 | 9.3 ± 0.0 | 28.6 ± 0.2 | 14.69 ± 0.49 | 1.50 ± 0.05 | 0.03 ± 0.00 | 0.30 ± 0.01 |

## CPU time per stage (seconds) and energy

| Files | Read+hash | Duplicates | Scoring | Apply | Rollback | Energy kWh (estimated from CPU time) | Energy kWh (CodeCarbon) | Rollback restored every byte |
|---|---|---|---|---|---|---|---|---|
| 100 | 0.12 ± 0.01 | 0.51 ± 0.02 | 0.10 ± 0.01 | 0.07 ± 0.00 | 0.02 ± 0.00 | 0.000003 ± 0.000000 | n/a | 3 of 3 |
| 500 | 0.50 ± 0.06 | 2.67 ± 0.03 | 0.40 ± 0.03 | 0.36 ± 0.02 | 0.05 ± 0.00 | 0.000016 ± 0.000000 | n/a | 3 of 3 |
| 2000 | 1.90 ± 0.03 | 10.89 ± 0.41 | 1.73 ± 0.03 | 1.45 ± 0.02 | 0.25 ± 0.01 | 0.000067 ± 0.000002 | n/a | 3 of 3 |

Notes: the two baselines are our own simple implementations, not third-party tools. Energy from CPU time assumes 15 W per busy core. `model_energy_saved_kwh_month` in results.csv is an assumption-based figure and must not be reported as measured.
