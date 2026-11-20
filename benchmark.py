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
import math
import os
import platform
import random
import re
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
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return rel, fh.read()

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
    t2, rc0 = time.perf_counter(), time.process_time()
    rb = g.rollback(root)
    rollback_s, rollback_cpu = time.perf_counter() - t2, time.process_time() - rc0
    ok = tree_hashes(root) == before_tree
    total, hash_only, gzip_all = baselines(root, files)
    row = dict(n_files=len(files), seed=seed, total_bytes=total, scan_s=scan_s, apply_s=apply_s, rollback_s=rollback_s,
               saved_bytes=manifest["saved_bytes"], actions=len(manifest["actions"]), skipped=len(manifest["skipped"]),
               saved_pct=100 * manifest["saved_bytes"] / total if total else 0,
               hash_only_saved_pct=100 * hash_only["saved"] / total, hash_only_s=hash_only["seconds"],
               gzip_all_saved_pct=100 * gzip_all["saved"] / total, gzip_all_s=gzip_all["seconds"],
               rollback_ok=bool(ok and rb and not rb["problems"]), tree_changed=before_tree != changed_tree,
               cpu_read_hash=meta["phases"]["read_hash"], cpu_duplicates=meta["phases"]["duplicates"],
               cpu_score=meta["phases"]["score_decide"], cpu_apply=manifest["measured"]["cpu_seconds"], cpu_rollback=rollback_cpu,
               stages_wall_s=scan_s + manifest["measured"]["wall_seconds"] + rollback_s,
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
STAGES = [("cpu_read_hash", "Reading files, hashing, sensitive-data check", "#2f9e6d"),
          ("cpu_duplicates", "Finding duplicates", "#3b78b8"),
          ("cpu_score", "Scoring and choosing actions", "#d9902b"),
          ("cpu_apply", "Applying changes (compress or move, verify)", "#8a6bb8"),
          ("cpu_rollback", "Rollback (restore and verify)", "#c4473d")]


def mean(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None]
    return statistics.mean(v) if v else None


def pct(x, d=0):
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def cpu_parts(rows):
    """Share of CPU time per stage, averaged over the given runs."""
    parts = [(label, mean(rows, key) or 0.0, color) for key, label, color in STAGES]
    return parts


def cpu_utilisation(rows):
    """CPU time divided by elapsed time, in percent of one core, and of the whole machine."""
    cpu = sum(sum(r[k] for k, _, _ in STAGES) for r in rows)
    wall = sum(r["stages_wall_s"] for r in rows)
    one = 100 * cpu / wall if wall else 0
    return one, one / (os.cpu_count() or 1)


def pie_svg(parts, title="Where the CPU time went"):
    total = sum(v for _, v, _ in parts) or 1.0
    cx, cy, r = 130, 135, 105
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 270" width="640" height="270" role="img" aria-label="{title}" font-family="Segoe UI, Arial, sans-serif">',
           f'<text x="10" y="20" font-size="15" font-weight="700" fill="#14231c">{title}</text>']
    ang = -math.pi / 2
    live = [(l, v, c) for l, v, c in parts if v > 0]
    for label, v, color in live:
        frac = v / total
        if len(live) == 1:
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
            break
        a2 = ang + 2 * math.pi * frac
        x1, y1, x2, y2 = cx + r * math.cos(ang), cy + r * math.sin(ang), cx + r * math.cos(a2), cy + r * math.sin(a2)
        out.append(f'<path d="M{cx},{cy} L{x1:.2f},{y1:.2f} A{r},{r} 0 {1 if frac > .5 else 0} 1 {x2:.2f},{y2:.2f} Z" fill="{color}" stroke="#fff" stroke-width="2"/>')
        ang = a2
    y = 62
    for label, v, color in parts:
        out.append(f'<rect x="270" y="{y - 11}" width="14" height="14" rx="3" fill="{color}"/>'
                   f'<text x="292" y="{y}" font-size="13" fill="#14231c">{label}</text>'
                   f'<text x="292" y="{y + 16}" font-size="12" fill="#5a6d63">{100 * v / total:.1f}% of CPU time ({v:.2f} s)</text>')
        y += 42
    out.append("</svg>")
    return "\n".join(out)


def table(headers, body):
    md = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in body]
    html = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead><tbody>" + \
           "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table>"
    return "\n".join(md), html


def build_report(rows, args, env):
    sizes = sorted({r["n_files"] for r in rows})
    big = [r for r in rows if r["n_files"] == sizes[-1]]
    n_runs = len(rows) // len(sizes)
    labelled = "exact_precision" in rows[0]
    mb = lambda rs: statistics.mean(r["total_bytes"] for r in rs) / 1e6
    one, whole = cpu_utilisation(rows)
    parts = cpu_parts(big)
    tot = sum(v for _, v, _ in parts) or 1
    top = max(parts, key=lambda p: p[1])
    md, html = [], []

    def para(text):
        md.append(text + "\n")
        html.append("<p>" + re.sub(r"`([^`]+)`", r"<code>\1</code>", text.replace("**", "")) + "</p>")

    def head(text, level=2):
        md.append("#" * level + " " + text + "\n")
        html.append(f"<h{level}>{text}</h{level}>")

    def bullets(items):
        md.append("\n".join("- " + i for i in items) + "\n")
        html.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")

    def tbl(h, body):
        m, hh = table(h, body)
        md.append(m + "\n")
        html.append(hh)

    head("GreenData AI: results summary", 1)
    para(f"Tested on {len(sizes)} folder sizes ({sizes[0]} to {sizes[-1]} files, {mb([r for r in rows if r['n_files'] == sizes[0]]):.1f} to {mb(big):.0f} MB), "
         f"{n_runs} run(s) each. " + ("The folders are generated with known right answers, so every result can be checked." if labelled else "The folder is a real dataset, so there is no answer key."))
    head("The short version" + (f" ({sizes[-1]} files)" if labelled else ""))
    items = [f"**Storage saved:** {mean(big, 'saved_pct'):.0f}% (simple duplicate removal alone: {mean(big, 'hash_only_saved_pct'):.0f}%, compressing everything: {mean(big, 'gzip_all_saved_pct'):.0f}%)."]
    if labelled:
        items += [f"**Protected files changed:** {mean(big, 'unsafe_changes'):.0f} of {mean(big, 'must_keep_total'):.0f}. Compressing everything changes all {mean(big, 'must_keep_total'):.0f}.",
                  f"**Exact copies found:** {pct(mean(rows, 'exact_recall'))}. **Near-copies found:** {pct(mean(rows, 'near_recall'))}.",
                  f"**Sensitive files found:** {pct(mean(rows, 'pii_recall'))}. Of the files it flagged, {pct(mean(rows, 'pii_precision'))} were really sensitive (decoys are order and tracking numbers that look like phone or Aadhaar numbers; real data will have other look-alikes)."]
    items += [f"**Undo (rollback):** restored every file exactly in {sum(r['rollback_ok'] for r in rows)} of {len(rows)} runs.",
              f"**Speed:** {mean(big, 'scan_s'):.0f} s to scan {sizes[-1]} files.",
              f"**CPU:** busy {one:.0f}% of one core while working (about {whole:.0f}% of this {os.cpu_count()}-CPU machine). Biggest share: {top[0].lower()} ({100 * top[1] / tot:.0f}%)."]
    bullets(items)
    head("Results by folder size")
    tbl(["Files", "Data size", "Storage saved", "Scan time", "CPU time", "Undo worked"],
        [[n, f"{mb([r for r in rows if r['n_files'] == n]):.1f} MB", f"{mean([r for r in rows if r['n_files'] == n], 'saved_pct'):.0f}%",
          f"{mean([r for r in rows if r['n_files'] == n], 'scan_s'):.1f} s", f"{mean([r for r in rows if r['n_files'] == n], 'cpu_s'):.1f} s",
          f"{sum(r['rollback_ok'] for r in rows if r['n_files'] == n)} of {len([r for r in rows if r['n_files'] == n])}"] for n in sizes])
    if labelled:
        head("How well it finds things")
        tbl(["What", "Found (share of the real ones)", "Correct (share of the flagged ones)"],
            [["Exact copies", pct(mean(rows, "exact_recall")), pct(mean(rows, "exact_precision"))],
             ["Near-copies (a few words changed)", pct(mean(rows, "near_recall")), pct(mean(rows, "near_precision"))],
             ["Sensitive files", pct(mean(rows, "pii_recall")), pct(mean(rows, "pii_precision"))]])
    head("Compared with simpler methods" + (f" ({sizes[-1]} files)" if labelled else ""))
    tbl(["Method", "Storage saved", "Protected files changed", "Time"],
        [["GreenData AI", f"{mean(big, 'saved_pct'):.0f}%", f"{mean(big, 'unsafe_changes'):.0f}" if labelled else "n/a", f"{mean(big, 'scan_s') + mean(big, 'apply_s'):.1f} s"],
         ["Remove exact copies only", f"{mean(big, 'hash_only_saved_pct'):.0f}%", "not checked", f"{mean(big, 'hash_only_s'):.2f} s"],
         ["Compress every file", f"{mean(big, 'gzip_all_saved_pct'):.0f}%", f"{mean(big, 'must_keep_total'):.0f} (all of them)" if labelled else "all of them", f"{mean(big, 'gzip_all_s'):.2f} s"]])
    head("CPU usage")
    para(f"CPU utilisation is CPU time divided by elapsed time. The tool runs on one core, so about 100% of one core means it was working the whole time. "
         f"Measured: {one:.0f}% of one core, about {whole:.0f}% of the whole machine ({os.cpu_count()} CPUs). The chart shows where the CPU time went for the largest folder.")
    md.append("![Where the CPU time went](cpu_pie.svg)\n")
    html.append(pie_svg(parts))
    bullets([f"{label}: {100 * v / tot:.0f}% ({v:.1f} s)" for label, v, _ in parts])
    head("Read this first")
    bullets([("The data is synthetic (generated by us). " if labelled else "") + "Numbers depend on this machine: " + f"Python {env['python']}, {env['cpu_count']} CPU(s).",
             "Energy is estimated from CPU time (15 W per busy core)." if not env["codecarbon"] else "Energy was measured with CodeCarbon.",
             "The dashboard's energy, CO2 and cost figures are assumptions, not results.",
             "Full tables with every column: `results_detailed.md`. Raw numbers for every run: `results.csv`."])
    return "\n".join(md), "\n".join(html), parts


CSS = ("body{font:15px/1.55 Segoe UI,Arial,sans-serif;max-width:860px;margin:30px auto;padding:0 18px;color:#14231c}"
       "h1{font-size:26px}h2{font-size:19px;margin-top:28px}table{border-collapse:collapse;width:100%;margin:8px 0}"
       "th,td{border-bottom:1px solid #dde6e0;padding:7px 8px;text-align:left}th{color:#5a6d63;font-weight:600}"
       "code{background:#eef4f0;padding:1px 5px;border-radius:4px}svg{max-width:100%;height:auto}")


def detailed_md(rows, args, env):
    sizes = sorted({r["n_files"] for r in rows})
    ms = lambda rs, key, d=3: ("n/a" if not [r[key] for r in rs if r.get(key) is not None] else
                               (f"{statistics.mean(r[key] for r in rs):.{d}f} ± {statistics.pstdev([r[key] for r in rs]):.{d}f}" if len(rs) > 1 else f"{rs[0][key]:.{d}f}"))
    md = ["# Detailed tables", "", "Mean ± standard deviation over the runs. P = precision (flagged files that were right), R = recall (real ones that were found).",
          f"Environment: Python {env['python']}, {env['os']}, {env['cpu_count']} CPUs, codec {env['codec']}, "
          f"CodeCarbon {'installed' if env['codecarbon'] else 'not installed (energy is a CPU-time estimate)'}, {env['date']}.", ""]
    if "exact_precision" in rows[0]:
        md += ["## Detection quality", "", "| Files | Exact dup. P | R | Near dup. P | R | Sensitive P | R | Look-alike numbers flagged |", "|---|---|---|---|---|---|---|---|"]
        for n in sizes:
            s = [r for r in rows if r["n_files"] == n]
            md.append(f"| {n} | {ms(s, 'exact_precision')} | {ms(s, 'exact_recall')} | {ms(s, 'near_precision')} | {ms(s, 'near_recall')} | {ms(s, 'pii_precision')} | {ms(s, 'pii_recall')} | {ms(s, 'decoys_flagged', 1)} of {s[0]['decoys_total']} |")
        md += ["", "## Safety", "", "| Files | Protected files | GreenData changed | Compress-everything changed | Different files merged | Stale files archived | Incompressible files wasted |", "|---|---|---|---|---|---|---|"]
        for n in sizes:
            s = [r for r in rows if r["n_files"] == n]
            md.append(f"| {n} | {ms(s, 'must_keep_total', 1)} | {ms(s, 'unsafe_changes', 1)} | {ms(s, 'gzip_all_unsafe_changes', 1)} | {ms(s, 'near_false_on_different', 1)} | {ms(s, 'stale_actioned', 1)} of {s[0]['stale_total']} | {ms(s, 'binary_wasted', 1)} |")
    md += ["", "## Storage and time", "", "| Files | MB | GreenData saved % | Exact-copies-only saved % | Compress-all saved % | Scan s | Apply s | Exact-copies-only s | Compress-all s |", "|---|---|---|---|---|---|---|---|---|"]
    for n in sizes:
        s = [r for r in rows if r["n_files"] == n]
        md.append(f"| {n} | {statistics.mean(r['total_bytes'] for r in s) / 1e6:.1f} | {ms(s, 'saved_pct', 1)} | {ms(s, 'hash_only_saved_pct', 1)} | {ms(s, 'gzip_all_saved_pct', 1)} | {ms(s, 'scan_s', 2)} | {ms(s, 'apply_s', 2)} | {ms(s, 'hash_only_s', 2)} | {ms(s, 'gzip_all_s', 2)} |")
    md += ["", "## CPU time per stage (seconds) and energy", "", "| Files | Read+hash | Duplicates | Scoring | Apply | Rollback | Energy kWh (estimated from CPU time) | Energy kWh (CodeCarbon) | Rollback restored every byte |", "|---|---|---|---|---|---|---|---|---|"]
    for n in sizes:
        s = [r for r in rows if r["n_files"] == n]
        md.append(f"| {n} | {ms(s, 'cpu_read_hash', 2)} | {ms(s, 'cpu_duplicates', 2)} | {ms(s, 'cpu_score', 2)} | {ms(s, 'cpu_apply', 2)} | {ms(s, 'cpu_rollback', 2)} | {ms(s, 'energy_kwh_estimated', 6)} | {ms(s, 'energy_kwh_measured', 6)} | {sum(r['rollback_ok'] for r in s)} of {len(s)} |")
    md += ["", "Notes: the two baselines are our own simple implementations, not third-party tools. Energy from CPU time assumes 15 W per busy core. "
           "`model_energy_saved_kwh_month` in results.csv is an assumption-based figure and must not be reported as measured.", ""]
    return "\n".join(md)


def write_outputs(out, rows, args):
    os.makedirs(out, exist_ok=True)
    env = dict(python=platform.python_version(), os=platform.platform(), machine=platform.processor() or platform.machine(),
               cpu_count=os.cpu_count(), codec=g.CODEC_EXT, codecarbon=bool(g.EmissionsTracker), date=time.strftime("%Y-%m-%d %H:%M"))
    with open(os.path.join(out, "results.json"), "w") as fh:
        json.dump(dict(environment=env, args=vars(args), runs=rows), fh, indent=2)
    with open(os.path.join(out, "results.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    md, html, parts = build_report(rows, args, env)
    sizes = sorted({r["n_files"] for r in rows})
    with open(os.path.join(out, "cpu_pie.svg"), "w", encoding="utf-8") as fh:
        fh.write(pie_svg(cpu_parts([r for r in rows if r["n_files"] == sizes[-1]])))
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as fh:
        fh.write(f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GreenData AI results</title><style>{CSS}</style><body>{html}</body></html>')
    with open(os.path.join(out, "results_detailed.md"), "w", encoding="utf-8") as fh:
        fh.write(detailed_md(rows, args, env))
    return md


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
    print(f"\nFiles written to {a.out}/ (report.md, report.html, cpu_pie.svg, results_detailed.md, results.csv, results.json)")


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
