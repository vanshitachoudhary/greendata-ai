#!/usr/bin/env python3
"""Measured experiments for GreenData AI. Every number comes from running the real tool.

synthetic  Builds labelled datasets with a known ground truth (exact copies, near-copies, sensitive files,
           stale files, important files, decoys) and measures detection quality, storage saved, time,
           safety, energy and rollback integrity, next to two simple baselines.
real       Runs the tool on a folder you choose (a copy by default) and measures storage saved, time,
           energy and rollback integrity. Ground truth does not exist there, so it can also write a sample
           of flagged files for you to check by hand.

  python benchmark.py synthetic --sizes 100,500,2000 --runs 3 --out results
  python benchmark.py real --dir path/to/dataset --out results_real --sample-audit 50
"""
import argparse
import csv
import gzip
import hashlib
import json
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time

import greendata as g

KEYWORDS = ["sales", "invoice"]
CHANGED = {"dedupe", "near", "archive", "compress"}
FRACTIONS = [("exact_dup", .15), ("near_pos", .08), ("near_neg", .04), ("pii", .06), ("decoy", .04),
             ("stale", .13), ("important", .06), ("binary", .04)]  # the rest are unique originals


# ----------------------------------------------------------------------------- dataset with ground truth
class Gen:
    def __init__(self, seed):
        self.r = random.Random(seed)
        self.vocab = ["".join(self.r.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(self.r.randrange(3, 10)))
                      for _ in range(6000)]

    def w(self):
        return self.r.choice(self.vocab)

    def text(self, kind, n=500, keywords=()):
        r = self.r
        if kind == "csv":
            rows = ["id,name,value,note"] + [f"{r.randrange(10**7)},{self.w()},{r.randrange(10**5)},{self.w()} {self.w()}" for _ in range(n // 4)]
        elif kind == "log":
            rows = [f"2025-{r.randrange(1, 13):02d}-{r.randrange(1, 28):02d} {r.choice(['INFO', 'WARN', 'DEBUG'])} {self.w()} {self.w()} id={r.randrange(10**7)}" for _ in range(n // 6)]
        else:
            rows = [" ".join(self.w() for _ in range(10)) for _ in range(n // 10)]
        for k in keywords:
            rows.insert(r.randrange(len(rows)), f"{k} {k} summary {self.w()}")
        return "\n".join(rows) + "\n"

    def edit(self, text, rate):
        words = text.split(" ")
        for i in range(len(words)):
            if self.r.random() < rate:
                words[i] = self.w()
        out = " ".join(words)
        while out == text:  # a "near copy" must really differ
            words[self.r.randrange(len(words))] = self.w()
            out = " ".join(words)
        return out


def build_dataset(root, n, seed):
    gen, r = Gen(seed), random.Random(seed + 1)
    now = time.time()
    labels, order = {}, []
    counts = {role: int(n * f) for role, f in FRACTIONS}
    counts["original"] = n - sum(counts.values())
    kinds = ["csv", "log", "txt"]

    def put(role, text=None, data=None, days=10, ext=None, ref=None):
        kind = r.choice(kinds)
        ext = ext or {"csv": "csv", "log": "log", "txt": "txt"}[kind]
        sub = {"csv": "data", "log": "logs", "txt": "reports"}[kind] if role != "binary" else "media"
        rel = f"{sub}/f{len(order):05d}.{ext}"
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data if data is not None else text.encode())
        ts = now - days * 86400
        os.utime(p, (ts, ts))
        labels[rel] = dict(role=role, ref=ref)
        order.append(rel)
        return rel, kind

    originals = []
    for _ in range(counts["original"]):
        kind = r.choice(kinds)
        rel, _ = put("original", gen.text(kind), days=r.randrange(5, 250), ext=kind)
        originals.append(rel)
    pool = originals or [None]

    def source():
        rel = r.choice(pool)
        return rel, open(os.path.join(root, rel), encoding="utf-8").read()

    for _ in range(counts["exact_dup"]):
        ref, text = source()
        put("exact_dup", text, days=r.randrange(5, 250), ext=ref.rsplit(".", 1)[1], ref=ref)
    for _ in range(counts["near_pos"]):
        ref, text = source()
        put("near_pos", gen.edit(text, r.choice([.01, .02, .03])), days=r.randrange(5, 250), ext=ref.rsplit(".", 1)[1], ref=ref)
    for _ in range(counts["near_neg"]):
        ref, text = source()
        put("near_neg", gen.edit(text, .10), days=r.randrange(5, 250), ext=ref.rsplit(".", 1)[1], ref=ref)
    for _ in range(counts["pii"]):
        t = gen.text("csv", 200) + "".join(f"{gen.w()},{gen.w()}{r.randrange(100)}@example.com,9{r.randrange(10**8, 10**9 - 1):09d}\n" for _ in range(5))
        put("pii", t, days=r.randrange(5, 200), ext="csv")
    for _ in range(counts["decoy"]):  # numbers that look sensitive but are not
        t = gen.text("csv", 200) + "".join(f"order {r.randrange(1, 9)}{r.randrange(10**8, 10**9)} tracking {r.randrange(10**11, 10**12)}\n" for _ in range(5))
        put("decoy", t, days=r.randrange(5, 200), ext="csv")
    for _ in range(counts["stale"]):
        put("stale", gen.text("log", 1200), days=r.randrange(300, 700), ext="log")
    for _ in range(counts["important"]):
        put("important", gen.text("csv", 600, KEYWORDS), days=r.randrange(1, 60), ext="csv")
    for _ in range(counts["binary"]):
        put("binary", data=os.urandom(r.randrange(20_000, 80_000)), days=r.randrange(30, 600), ext="bin")
    return labels


def tree_hashes(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != g.STATE_DIR]
        for name in fn:
            p = os.path.join(dp, name)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = hashlib.sha256(fh.read()).hexdigest()
            try:  # hashing must not change "last used", or it would distort the experiment
                os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
    return out


# ----------------------------------------------------------------------------- evaluation
def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def evaluate(files, labels):
    by = {f.rel: f for f in files}
    out = {}
    # exact duplicates, at group level (which copy is kept does not matter)
    groups = {}
    for rel, lab in labels.items():
        if lab["role"] == "exact_dup":
            groups.setdefault(lab["ref"], {lab["ref"]}).add(rel)
    in_group = set().union(*groups.values()) if groups else set()
    flagged = {f.rel for f in files if f.dup}
    need = sum(len(m) - 1 for m in groups.values())
    tp = sum(1 for x in flagged if x in in_group)
    out["exact"] = prf(tp, len(flagged) - tp, need - tp)
    # near duplicates, by family: an original and its near copies are all similar to each other
    fam = {r: r for r, l in labels.items() if l["role"] == "original"}
    fam.update({r: l["ref"] for r, l in labels.items() if l["role"] == "near_pos"})
    near_flagged = [f for f in files if f.near]
    tpn = sum(1 for f in near_flagged if f.rel in fam and fam.get(f.of) == fam[f.rel])
    needed = sum(1 for l in labels.values() if l["role"] == "near_pos")
    out["near"] = prf(tpn, len(near_flagged) - tpn, needed - tpn)
    out["near_false_on_different"] = sum(1 for f in near_flagged if labels[f.rel]["role"] == "near_neg")
    # sensitive data
    truth = {r for r, l in labels.items() if l["role"] == "pii"}
    pii = {f.rel for f in files if f.pii}
    out["pii"] = prf(len(pii & truth), len(pii - truth), len(truth - pii))
    decoys = {r for r, l in labels.items() if l["role"] == "decoy"}
    out["decoys_flagged"] = (len(pii & decoys), len(decoys))
    # safety and usefulness
    must = [r for r, l in labels.items() if l["role"] in ("pii", "important")]
    out["unsafe_changes"] = sum(1 for r in must if by[r].action in CHANGED)
    out["must_keep_total"] = len(must)
    stale = [r for r, l in labels.items() if l["role"] == "stale"]
    out["stale_actioned"] = (sum(1 for r in stale if by[r].action in ("archive", "compress")), len(stale))
    binary = [r for r, l in labels.items() if l["role"] == "binary"]
    out["binary_wasted"] = sum(1 for r in binary if by[r].action in ("archive", "compress"))
    return out


def baselines(root, files):
    total = sum(f.size for f in files)
    t0 = time.perf_counter()
    seen, saved = set(), 0
    for f in sorted(files, key=lambda x: x.rel):
        with open(f.abs, "rb") as fh:
            h = hashlib.sha256(fh.read()).hexdigest()
        if h in seen:
            saved += f.size
        seen.add(h)
    hash_only = dict(saved=saved, seconds=time.perf_counter() - t0)
    t0 = time.perf_counter()
    gz = 0
    for f in files:
        with open(f.abs, "rb") as fh:
            gz += max(0, f.size - len(gzip.compress(fh.read(), 6)))
    gzip_all = dict(saved=gz, seconds=time.perf_counter() - t0)
    return total, hash_only, gzip_all


def run_once(root, labels, seed, n):
    cfg = g.Cfg(keywords=KEYWORDS)
    before_tree = tree_hashes(root)
    tracker = None
    if g.EmissionsTracker:
        try:
            tracker = g.EmissionsTracker(project_name="greendata-bench", log_level="error", save_to_file=False)
            tracker.start()
        except Exception:
            tracker = None
    c0, t0 = time.process_time(), time.perf_counter()
    files, meta = g.scan(root, cfg)
    scan_s = time.perf_counter() - t0
    summ = g.summarize(files, meta, cfg)
    ev = evaluate(files, labels) if labels else {}
    t1 = time.perf_counter()
    manifest = g.apply_plan(root, files, cfg)
    apply_s = time.perf_counter() - t1
    cpu = time.process_time() - c0
    measured_kwh = None
    if tracker:
        try:
            tracker.stop()
            measured_kwh = tracker.final_emissions_data.energy_consumed
        except Exception:
            pass
    changed_tree = tree_hashes(root)
    t2 = time.perf_counter()
    rb = g.rollback(root)
    rollback_s = time.perf_counter() - t2
    ok = tree_hashes(root) == before_tree
    total, hash_only, gzip_all = baselines(root, files)
    row = dict(n_files=len(files), seed=seed, total_bytes=total, scan_s=scan_s, apply_s=apply_s, rollback_s=rollback_s,
               saved_bytes=manifest["saved_bytes"], actions=len(manifest["actions"]), skipped=len(manifest["skipped"]),
               saved_pct=100 * manifest["saved_bytes"] / total if total else 0,
               hash_only_saved_pct=100 * hash_only["saved"] / total, hash_only_s=hash_only["seconds"],
               gzip_all_saved_pct=100 * gzip_all["saved"] / total, gzip_all_s=gzip_all["seconds"],
               rollback_ok=bool(ok and rb and not rb["problems"]), tree_changed=before_tree != changed_tree,
               cpu_s=cpu, energy_kwh_estimated=cpu * g.ASSUME["cpu_watts"] / 3.6e6, energy_kwh_measured=measured_kwh,
               near_potential_pct=100 * sum(f.size for f in files if f.action == "near") / total if total else 0,
               model_energy_saved_kwh_month=summ["before"]["kwh"] - summ["after"]["kwh"])
    if labels:
        for k in ("exact", "near", "pii"):
            row[f"{k}_precision"], row[f"{k}_recall"], row[f"{k}_f1"] = ev[k]
        row["near_false_on_different"] = ev["near_false_on_different"]
        row["decoys_flagged"], row["decoys_total"] = ev["decoys_flagged"]
        row["unsafe_changes"], row["must_keep_total"] = ev["unsafe_changes"], ev["must_keep_total"]
        row["stale_actioned"], row["stale_total"] = ev["stale_actioned"]
        row["binary_wasted"] = ev["binary_wasted"]
        row["gzip_all_unsafe_changes"] = ev["must_keep_total"]  # compressing everything touches every protected file
    return row, files


# ----------------------------------------------------------------------------- reporting
def ms(rows, key, d=3):
    v = [r[key] for r in rows if r.get(key) is not None]
    if not v:
        return "n/a"
    return f"{statistics.mean(v):.{d}f} ± {statistics.pstdev(v):.{d}f}" if len(v) > 1 else f"{v[0]:.{d}f}"


def write_outputs(out, rows, args):
    os.makedirs(out, exist_ok=True)
    env = dict(python=platform.python_version(), os=platform.platform(), machine=platform.processor() or platform.machine(),
               cpu_count=os.cpu_count(), codec=g.CODEC_EXT, codecarbon=bool(g.EmissionsTracker), date=time.strftime("%Y-%m-%d %H:%M"))
    with open(os.path.join(out, "results.json"), "w") as fh:
        json.dump(dict(environment=env, args=vars(args), runs=rows), fh, indent=2)
    keys = sorted({k for r in rows for k in r})
    with open(os.path.join(out, "results.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    sizes = sorted({r["n_files"] for r in rows})
    md = ["# Measured results", "", f"Environment: Python {env['python']}, {env['os']}, {env['cpu_count']} CPUs, codec {env['codec']}, "
          f"CodeCarbon {'installed' if env['codecarbon'] else 'not installed (energy is a CPU-time estimate)'}, {env['date']}.",
          f"Each cell is mean ± standard deviation over {args.runs if hasattr(args, 'runs') else 1} run(s) with different random seeds.", ""]
    has_labels = "exact_precision" in rows[0]
    if has_labels:
        md += ["## Table 1. Detection quality (against ground truth)", "", "| Files | Exact dup. P | R | Near dup. P | R | Sensitive P | R | Decoys flagged |", "|---|---|---|---|---|---|---|---|"]
        for n in sizes:
            s = [r for r in rows if r["n_files"] == n]
            md.append(f"| {n} | {ms(s, 'exact_precision')} | {ms(s, 'exact_recall')} | {ms(s, 'near_precision')} | {ms(s, 'near_recall')} | "
                      f"{ms(s, 'pii_precision')} | {ms(s, 'pii_recall')} | {ms(s, 'decoys_flagged', 1)} of {s[0]['decoys_total']} |")
        md += ["", "## Table 2. Safety", "", "| Files | Protected files (sensitive + important) | GreenData changed | Compress-everything baseline changed | Different files merged as near-dup. | Stale files archived | Incompressible files wasted |", "|---|---|---|---|---|---|---|"]
        for n in sizes:
            s = [r for r in rows if r["n_files"] == n]
            md.append(f"| {n} | {ms(s, 'must_keep_total', 1)} | {ms(s, 'unsafe_changes', 1)} | {ms(s, 'gzip_all_unsafe_changes', 1)} | {ms(s, 'near_false_on_different', 1)} | "
                      f"{ms(s, 'stale_actioned', 1)} of {s[0]['stale_total']} | {ms(s, 'binary_wasted', 1)} |")
    md += ["", "## Table 3. Storage saved and time", "", "| Files | Size (MB) | GreenData saved % | Hash-only dedup saved % | Compress-all saved % | GreenData scan s | apply s | Hash-only s | Compress-all s |", "|---|---|---|---|---|---|---|---|---|"]
    for n in sizes:
        s = [r for r in rows if r["n_files"] == n]
        mb = statistics.mean(r["total_bytes"] for r in s) / 1e6
        md.append(f"| {n} | {mb:.1f} | {ms(s, 'saved_pct', 1)} | {ms(s, 'hash_only_saved_pct', 1)} | {ms(s, 'gzip_all_saved_pct', 1)} | "
                  f"{ms(s, 'scan_s', 2)} | {ms(s, 'apply_s', 2)} | {ms(s, 'hash_only_s', 2)} | {ms(s, 'gzip_all_s', 2)} |")
    md += ["", "## Table 4. Energy of running the tool, and rollback", "", "| Files | CPU s | Energy (kWh) estimated from CPU time | Energy (kWh) measured by CodeCarbon | Rollback restored every byte |", "|---|---|---|---|---|"]
    for n in sizes:
        s = [r for r in rows if r["n_files"] == n]
        md.append(f"| {n} | {ms(s, 'cpu_s', 2)} | {ms(s, 'energy_kwh_estimated', 6)} | {ms(s, 'energy_kwh_measured', 6)} | {sum(r['rollback_ok'] for r in s)} of {len(s)} |")
    md += ["", "Notes: hash-only dedup and compress-all are our own simple baselines, not third-party tools. "
           "The 'Compress-everything baseline' changes every file, so it touches every protected file by construction. "
           "Energy from CPU time assumes 15 W per busy core and is an estimate. The tool's modelled monthly energy saving "
           "(an assumption-based figure) is stored in results.csv as model_energy_saved_kwh_month and must not be reported as measured.", ""]
    with open(os.path.join(out, "results.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    return "\n".join(md)


def audit_sample(out, files, k, seed=1):
    cands = [f for f in files if f.near or f.dup or f.pii]
    random.Random(seed).shuffle(cands)
    with open(os.path.join(out, "audit_sample.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "flagged_as", "compared_with_or_reason", "similarity_percent", "correct (y/n) - fill by hand"])
        for f in cands[:k]:
            kind = "exact duplicate" if f.dup else ("near duplicate" if f.near else "sensitive")
            w.writerow([f.rel, kind, f.of or ",".join(f.pii), f.sim or "", ""])


# ----------------------------------------------------------------------------- commands
def cmd_synthetic(a):
    rows = []
    for n in [int(x) for x in a.sizes.split(",")]:
        for i in range(a.runs):
            seed = 1000 + i
            with tempfile.TemporaryDirectory() as tmp:
                labels = build_dataset(tmp, n, seed)
                row, _ = run_once(tmp, labels, seed, n)
            rows.append(row)
            print(f"  n={n} seed={seed}: saved {row['saved_pct']:.1f}%, exact F1 {row['exact_f1']:.2f}, near F1 {row['near_f1']:.2f}, "
                  f"sensitive F1 {row['pii_f1']:.2f}, unsafe changes {row['unsafe_changes']}, rollback ok {row['rollback_ok']}")
    print("\n" + write_outputs(a.out, rows, a))
    print(f"\nFiles written to {a.out}/ (results.md, results.csv, results.json)")


def cmd_real(a):
    src = os.path.abspath(a.dir)
    if not os.path.isdir(src):
        sys.exit("Not a folder: " + src)
    tmp = tempfile.mkdtemp()
    work = src if a.in_place else os.path.join(tmp, "copy")
    try:
        if not a.in_place:
            print("Copying the dataset so the original stays untouched ...")
            shutil.copytree(src, work, ignore=shutil.ignore_patterns(g.STATE_DIR))
        rows = []
        for i in range(a.runs):
            row, files = run_once(work, None, i, 0)
            rows.append(row)
            print(f"  run {i + 1}: {row['n_files']} files, saved {row['saved_pct']:.1f}% ({g.fmt_bytes(row['saved_bytes'])}), "
                  f"scan {row['scan_s']:.1f}s, rollback ok {row['rollback_ok']}")
        os.makedirs(a.out, exist_ok=True)
        if a.sample_audit:
            audit_sample(a.out, g.scan(work, g.Cfg(keywords=a.keywords.split(",") if a.keywords else []))[0], a.sample_audit)
        print("\n" + write_outputs(a.out, rows, a))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synthetic")
    s.add_argument("--sizes", default="100,500,2000")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--out", default="results")
    r = sub.add_parser("real")
    r.add_argument("--dir", required=True)
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--out", default="results_real")
    r.add_argument("--keywords", default="")
    r.add_argument("--in-place", action="store_true", help="work on the original folder instead of a copy")
    r.add_argument("--sample-audit", type=int, default=0, help="write N flagged files to check by hand")
    a = ap.parse_args(argv)
    (cmd_synthetic if a.cmd == "synthetic" else cmd_real)(a)


if __name__ == "__main__":
    main()
