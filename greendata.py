#!/usr/bin/env python3
"""GreenData AI: safe, explainable data optimization with energy / CO2 estimates.

Commands
  demo     DIR              create a messy sample folder to try the tool on
  scan     DIR              analyze a folder, print the plan, write a JSON report (nothing is changed)
  apply    DIR              apply the plan (reversible), print real measured savings
  rollback DIR              restore everything from the latest (or a chosen) run

Only the Python standard library is required. If `zstandard` is installed it is used
for compression, and if `codecarbon` is installed `--measure` records measured energy.
"""
import argparse
import bz2  # noqa: F401  (kept for completeness of stdlib codecs)
import hashlib
import itertools
import json
import lzma
import math
import os
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

try:
    import zstandard as zstd
except ImportError:  # optional
    zstd = None
try:
    from codecarbon import EmissionsTracker
except Exception:  # optional
    EmissionsTracker = None

VERSION = "1.0"
STATE_DIR = ".greendata"
CODEC_EXT = ".zst" if zstd else ".xz"
TEXT_EXT = {"txt", "csv", "tsv", "log", "json", "md", "html", "xml", "py", "js", "yaml", "yml", "ini", "sql"}
TEXT_LIMIT = 3_000_000
REGIONS = {"India": 0.71, "USA": 0.37, "EU": 0.25, "Norway": 0.03}  # kg CO2 per kWh, illustrative
ASSUME = dict(proc_kwh_gb=0.05, store_kwh_gb_month=0.01, fixed_kwh_run=0.2, min_per_gb=1.2,
              fixed_min_run=3.0, inr_kwh=8.0, inr_gb_month=2.0, scan_kwh_gb=0.002, cpu_watts=15.0)
PII_RE = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone": re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)"),
    "aadhaar": re.compile(r"(?<!\d)[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\d)"),
    "PAN": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
}
ALREADY_COMPRESSED = {"zip", "gz", "xz", "zst", "bz2", "7z", "rar", "jpg", "jpeg", "png", "gif", "webp", "mp4", "mp3", "docx", "xlsx", "pptx"}


# ----------------------------------------------------------------------------- helpers
_VD = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
       [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
       [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VP = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
       [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(digits):
    """Aadhaar numbers end in a Verhoeff check digit, so random 12-digit numbers rarely pass."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def has_pii(text):
    found = []
    for k, rx in PII_RE.items():
        if k == "aadhaar":
            if any(verhoeff_ok(re.sub(r"\s", "", m.group())) for m in rx.finditer(text)):
                found.append(k)
        elif rx.search(text):
            found.append(k)
    return found


def ftype(ext):
    if ext in ("csv", "tsv"):
        return "csv"
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        return "img"
    if ext in ("doc", "docx"):
        return "doc"
    if ext in ("log", "json", "pdf"):
        return ext
    return "txt" if ext in TEXT_EXT else "bin"


def open_noatime(path):
    """Open for reading without touching the access time, so scanning does not change 'last used'."""
    flags = os.O_RDONLY | getattr(os, "O_NOATIME", 0) | getattr(os, "O_BINARY", 0)
    try:
        return os.fdopen(os.open(path, flags), "rb")
    except PermissionError:
        return open(path, "rb")


def restore_times(f):
    """Reading a file can update its access time (always on Windows). Put the original times back."""
    if f.ns[1]:
        try:
            os.utime(f.abs, ns=f.ns)
        except OSError:
            pass


def fmt_bytes(b):
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= size:
            return f"{b / size:.1f} {unit}"
    return f"{int(b)} B"


def fmt_energy(k):
    return f"{k:.2f} kWh" if k >= 1 else f"{k * 1000:.2f} Wh"


def fmt_co2(k):
    return f"{k:.2f} kg" if k >= 1 else f"{k * 1000:.1f} g"


def fmt_time(m):
    return f"{m / 60:.1f} h" if m >= 60 else (f"{m:.1f} min" if m >= 1 else f"{m * 60:.0f} s")


def ago(d):
    if d < 1:
        return "today"
    if d < 60:
        return f"{round(d)}d ago"
    if d < 730:
        return f"{round(d / 30)}mo ago"
    return f"{round(d / 365)}y ago"


@dataclass
class Cfg:
    keywords: list = field(default_factory=list)
    region: str = "India"
    runs: int = 4
    threshold: int = 35
    weights: tuple = (35, 25, 25, 15)  # relevance, access, recency, small size
    acc: int = 2           # assumed opens per month (file systems rarely record real counts)
    near_thr: float = 0.8
    rr_rel: float = 0.7    # "relevant" means relevance at or above this
    rr_days: int = 90      # "recent" means used within this many days
    include_near: bool = False
    extrapolate: float = 1.0  # what-if: scale GB figures in the energy model (tiny demo folders)


@dataclass
class FileInfo:
    rel: str
    abs: str
    size: int
    mtime: float
    atime: float
    ext: str = ""
    type: str = "bin"
    sha: str = ""
    relevance: float = 0.6
    pii: list = field(default_factory=list)
    dup: bool = False
    near: bool = False
    of: str = ""
    sim: int = 0
    days: float = 0.0
    score: int = 0
    action: str = "keep"
    reason: str = ""
    saved: int = 0          # estimated (scan) bytes saved by the recommended action
    protected_recent: bool = False
    ratio: float = -1.0     # cached compression ratio measured on a 1 MB sample
    ns: tuple = (0, 0)      # original (atime, mtime) in ns, restored after reading
    shingles: set = field(default_factory=set, repr=False)


# ----------------------------------------------------------------------------- scanning
def _h64(s):
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "ignore"), digest_size=8).digest(), "big")


_P = (1 << 61) - 1
_RNG = random.Random(42)
_PERMS = [(_RNG.randrange(1, _P), _RNG.randrange(0, _P)) for _ in range(64)]


def _signature(shingles):
    return [min((a * h + b) % _P for h in shingles) for a, b in _PERMS]


def iter_files(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d != STATE_DIR and not os.path.islink(os.path.join(dp, d)))
        for n in sorted(fn):
            p = os.path.join(dp, n)
            if not os.path.islink(p):
                yield p


def analyze(f, cfg):
    f.ext = f.rel.rsplit(".", 1)[-1].lower() if "." in os.path.basename(f.rel) else ""
    f.type = ftype(f.ext)
    h, buf = hashlib.sha256(), []
    want_text = f.ext in TEXT_EXT and f.size <= TEXT_LIMIT
    with open_noatime(f.abs) as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            if want_text:
                buf.append(chunk)
    f.sha = h.hexdigest()
    text = b"".join(buf).decode("utf-8", "ignore") if buf else ""
    f.pii = has_pii(text) if text else []
    if cfg.keywords:
        hay = (f.rel + " " + text[:200000]).lower()
        f.relevance = 0.15 + 0.85 * sum(k in hay for k in cfg.keywords) / len(cfg.keywords)
    words = text.lower().split()[:6000]
    if len(words) >= 3:
        f.shingles = {_h64(" ".join(words[i:i + 3])) for i in range(len(words) - 2)}
        if len(f.shingles) > 4000:
            f.shingles = set(sorted(f.shingles)[:4000])


MARK = re.compile(r"(copy|backup|bak|old|tmp|temp|final|draft|\(\d+\))", re.I)


def _rank(f):
    """Which copy to keep: names without copy/backup/final words first, then the most recently used."""
    stem = os.path.splitext(os.path.basename(f.rel))[0]
    return (1 if MARK.search(stem) else 0, -max(f.atime, f.mtime), f.rel)


def find_duplicates(files, cfg):
    groups = defaultdict(list)
    for f in files:
        groups[f.sha].append(f)
    for grp in groups.values():
        if len(grp) > 1:
            grp.sort(key=_rank)
            for f in grp[1:]:
                f.dup, f.of, f.sim = True, grp[0].rel, 100
    cands = sorted((f for f in files if not f.dup and len(f.shingles) >= 5), key=_rank)
    buckets = defaultdict(list)
    sigs = {}
    for i, f in enumerate(cands):
        sigs[i] = _signature(f.shingles)
        for b in range(16):  # 16 bands of 4 rows: catches pairs with similarity >= ~0.8
            buckets[(b, tuple(sigs[i][b * 4:(b + 1) * 4]))].append(i)
    seen_pairs = set()
    for ids in buckets.values():
        for i, j in itertools.combinations(sorted(set(ids)), 2):
            if (i, j) in seen_pairs:
                continue
            seen_pairs.add((i, j))
            a, b = cands[i], cands[j]
            if b.near:
                continue
            inter = len(a.shingles & b.shingles)
            jac = inter / (len(a.shingles | b.shingles) or 1)
            if jac >= cfg.near_thr:  # confirmed with the exact Jaccard similarity
                b.near, b.of, b.sim = True, a.rel, min(99, round(jac * 100))


def sample_savings(f):
    """Estimate bytes saved by compression using the real codec on the first 1 MB (measured once, then cached)."""
    if f.ratio < 0:
        with open_noatime(f.abs) as fh:
            sample = fh.read(1 << 20)
        restore_times(f)
        if not sample:
            f.ratio = 0.0
        else:
            c = len(zstd.ZstdCompressor(level=10).compress(sample)) if zstd else len(lzma.compress(sample, preset=6))
            f.ratio = max(0.0, 1 - c / len(sample))
    return int(f.size * f.ratio)


def rescore(files, cfg, total=None):
    """Score and decide an action for every file. Cheap, so the UI can call it again when a slider moves."""
    total = total if total is not None else sum(f.size for f in files)
    now = time.time()
    scale = 500e9 if total > 50e9 else max(total * 0.25, 1)
    W = cfg.weights
    ws = sum(W) or 1
    for f in files:
        f.days = max(0.0, (now - max(f.atime, f.mtime)) / 86400)
        sz = min(f.size / scale, 1)
        s = 100 * (W[0] * f.relevance + W[1] * min(cfg.acc / 20, 1) + W[2] * math.exp(-f.days / 180) + W[3] * (1 - sz)) / ws
        s -= 35 if f.dup else (20 if f.near else 0)
        f.score = round(max(0, min(100, s)))
        decide(f, cfg, total)


def scan(root, cfg):
    t0, c0 = time.time(), time.process_time()
    now = time.time()
    files = []
    for p in iter_files(root):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_size == 0:
            continue
        files.append(FileInfo(os.path.relpath(p, root).replace(os.sep, "/"), p, st.st_size, st.st_mtime, st.st_atime, ns=(st.st_atime_ns, st.st_mtime_ns)))
    files.sort(key=lambda f: (f.mtime, f.rel))
    for f in files:
        try:
            analyze(f, cfg)
            restore_times(f)
        except OSError as e:
            print(f"  skipped {f.rel}: {e}", file=sys.stderr)
    files = [f for f in files if f.sha]
    find_duplicates(files, cfg)
    total = sum(f.size for f in files)
    rescore(files, cfg, total)
    meta = dict(scan_seconds=time.time() - t0, scan_cpu_seconds=time.process_time() - c0, total=total)
    return files, meta


def decide(f, cfg, total):
    f.protected_recent = f.relevance >= cfg.rr_rel and f.days <= cfg.rr_days
    f.action, f.saved = "keep", 0
    f.reason = ("Relevant data used within %d days. Protected from archiving." % cfg.rr_days) if f.protected_recent else "Relevant and recently used."
    if f.pii:
        f.action, f.reason = "protect", "Sensitive data detected (%s). Needs manual approval." % ", ".join(f.pii)
    elif f.dup:
        f.action, f.saved, f.reason = "dedupe", f.size, f"Exact copy of {f.of}."
    elif f.near:
        f.action, f.saved, f.reason = "near", f.size, f"{f.sim}% similar to {f.of}."
    elif f.score < cfg.threshold and not f.protected_recent:
        f.action, f.reason = "archive", f"Low score ({f.score}), last used {round(f.days)} days ago."
    elif (f.size >= 10e9 or f.size >= total * 0.1) and cfg.acc <= 2 and not f.protected_recent:
        f.action, f.reason = "compress", "Large and rarely opened."
    if f.action in ("archive", "compress"):
        f.saved = sample_savings(f)
        if f.saved < 0.05 * f.size:  # already compressed: nothing to gain
            f.action, f.saved, f.reason = "keep", 0, "Already compressed, little to gain."


def metrics(storage, proc, cfg, runs=None):
    runs = runs or cfg.runs
    gb, pg = storage / 1e9 * cfg.extrapolate, proc / 1e9 * cfg.extrapolate
    e = pg * ASSUME["proc_kwh_gb"] * runs + gb * ASSUME["store_kwh_gb_month"] + ASSUME["fixed_kwh_run"] * runs
    return dict(storage=storage, time_min=pg * ASSUME["min_per_gb"] + ASSUME["fixed_min_run"], kwh=e,
                co2=e * REGIONS[cfg.region], cost=e * ASSUME["inr_kwh"] + gb * ASSUME["inr_gb_month"])


def approved(files, cfg):
    ok = {"dedupe", "archive", "compress"} | ({"near"} if cfg.include_near else set())
    return [f for f in files if f.action in ok]


def summarize(files, meta, cfg):
    total = meta["total"]
    ap = approved(files, cfg)
    saved = sum(f.saved for f in ap)
    removed_from_processing = sum(f.size for f in ap if f.action != "compress")
    before = metrics(total, total, cfg)
    after = metrics(total - saved, total - removed_from_processing, cfg)
    overhead = total / 1e9 * cfg.extrapolate * ASSUME["scan_kwh_gb"]
    return dict(before=before, after=after, saved_bytes=saved, actions=len(ap), scan_overhead_kwh=overhead,
                net_kwh_month=(before["kwh"] - after["kwh"]) - overhead,
                net_co2_month=((before["kwh"] - after["kwh"]) - overhead) * REGIONS[cfg.region])


# ----------------------------------------------------------------------------- reporting
def print_plan(files, summ, cfg):
    print(f"\n{'File':38} {'Size':>9} {'Score':>5} {'Used':>8}  {'Action':9} Why")
    print("-" * 110)
    for f in files:
        flag = " *" if f.protected_recent and f.action == "keep" else ""
        print(f"{f.rel[:38]:38} {fmt_bytes(f.size):>9} {f.score:>5} {ago(f.days):>8}  {f.action + flag:9} {f.reason}")
    b, a = summ["before"], summ["after"]
    print(f"\nBefore -> After ({cfg.region} grid, {cfg.runs} runs/month, estimated)")
    for name, key, fn in (("Storage", "storage", fmt_bytes), ("Time per run", "time_min", fmt_time),
                          ("Energy / month", "kwh", fmt_energy), ("CO2 / month", "co2", fmt_co2),
                          ("Cost / month", "cost", lambda v: f"Rs {v:,.2f}")):
        pct = (1 - a[key] / b[key]) * 100 if b[key] else 0
        print(f"  {name:15} {fn(b[key]):>12} -> {fn(a[key]):>12}   (-{pct:.0f}%)" if pct >= 1 else f"  {name:15} {fn(b[key]):>12} -> {fn(a[key]):>12}   (-{pct:.2f}%)")
    if cfg.extrapolate != 1:
        print(f"  What-if: the energy model above assumes the folder is {cfg.extrapolate:g}x larger than the real {fmt_bytes(summ['before']['storage'])}.")
    elif summ["before"]["storage"] < 1e9:
        print("  Note: this folder is small, so fixed per-run overhead dominates. Use --extrapolate N for a what-if on larger data.")
    print(f"  Net of the scan itself ({fmt_energy(summ['scan_overhead_kwh'])}): saves {fmt_energy(max(summ['net_kwh_month'], 0))} "
          f"and {fmt_co2(max(summ['net_co2_month'], 0))} CO2 in month one.")
    print("  * = relevant and used recently, protected from archiving.")
    if not cfg.include_near and any(f.action == "near" for f in files):
        print("  Near-duplicates are recommendations only. Use --apply-near to include them.")


def write_report(path, root, files, meta, summ, cfg):
    report = dict(tool="GreenData AI", version=VERSION, generated=datetime.now().isoformat(timespec="seconds"),
                  folder=os.path.abspath(root),
                  settings=dict(extrapolate=cfg.extrapolate, region=cfg.region, runs=cfg.runs, threshold=cfg.threshold, weights=list(cfg.weights),
                                keywords=cfg.keywords, assumptions=ASSUME, access_assumed_per_month=cfg.acc),
                  summary=dict(files=len(files), total_bytes=meta["total"], saved_bytes_estimated=summ["saved_bytes"],
                               before=summ["before"], after=summ["after"], net_kwh_month=summ["net_kwh_month"],
                               net_co2_month=summ["net_co2_month"], scan_seconds=meta["scan_seconds"]),
                  files=[dict(n=f.rel, t=f.type, gb=f.size / 1e9, d=round(f.days, 2), acc=cfg.acc, rel=round(f.relevance, 3),
                              dup=f.dup, near=f.near, pii=bool(f.pii), o=f.of, sim=f.sim, score=f.score, action=f.action, rr=f.protected_recent,
                              reason=f.reason, saved_bytes=f.saved) for f in files])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return report


# ----------------------------------------------------------------------------- apply / rollback
def _compress(src, dst):
    if zstd:
        cctx = zstd.ZstdCompressor(level=10)
        with open(src, "rb") as i, open(dst, "wb") as o:
            cctx.copy_stream(i, o)
    else:
        with open(src, "rb") as i, lzma.open(dst, "wb", preset=6) as o:
            shutil.copyfileobj(i, o, 1 << 20)


def _decompress(src, dst):
    if src.endswith(".zst"):
        with open(src, "rb") as i, open(dst, "wb") as o:
            zstd.ZstdDecompressor().copy_stream(i, o)
    else:
        with lzma.open(src, "rb") as i, open(dst, "wb") as o:
            shutil.copyfileobj(i, o, 1 << 20)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _inside(root, rel):
    p = os.path.abspath(os.path.join(root, rel))
    if os.path.commonpath([os.path.abspath(root), p]) != os.path.abspath(root):
        raise ValueError(f"path escapes the folder: {rel}")
    return p


def _audit(root, line):
    os.makedirs(os.path.join(root, STATE_DIR), exist_ok=True)
    with open(os.path.join(root, STATE_DIR, "audit.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")


def apply_plan(root, files, cfg, measure=False):
    run = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(root, STATE_DIR)
    trash, arch, mdir = (os.path.join(base, d, run) if d != "manifests" else os.path.join(base, d) for d in ("trash", "archive", "manifests"))
    for d in (trash, arch, mdir):
        os.makedirs(d, exist_ok=True)
    tracker = None
    if measure and EmissionsTracker:
        try:
            tracker = EmissionsTracker(project_name="greendata-apply", log_level="error", save_to_file=False)
            tracker.start()
        except Exception:
            tracker = None
    t0, c0 = time.time(), time.process_time()
    done, saved, skipped = [], 0, []
    for f in approved(files, cfg):
        src = _inside(root, f.rel)
        try:
            if _sha(src) != f.sha:
                skipped.append((f.rel, "file changed since the scan")); continue
            if f.action in ("dedupe", "near"):
                keep = _inside(root, f.of)
                if not os.path.exists(keep) or (f.action == "dedupe" and _sha(keep) != f.sha):
                    skipped.append((f.rel, "original not found or different")); continue
                dst = os.path.join(trash, f.rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                got = f.size
                rec = dict(action=f.action, path=f.rel, stored=os.path.relpath(dst, root).replace(os.sep, "/"), sha256=f.sha, size=f.size, kept=f.of)
            else:
                dst = (os.path.join(arch, f.rel) if f.action == "archive" else src) + CODEC_EXT
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _compress(src, dst)
                chk = dst + ".verify"
                _decompress(dst, chk)
                ok = _sha(chk) == f.sha
                os.remove(chk)
                if not ok:
                    os.remove(dst); skipped.append((f.rel, "verification failed")); continue
                got = f.size - os.path.getsize(dst)
                if got <= 0:
                    os.remove(dst); skipped.append((f.rel, "no space saved")); continue
                os.remove(src)
                rec = dict(action=f.action, path=f.rel, stored=os.path.relpath(dst, root).replace(os.sep, "/"), sha256=f.sha, size=f.size, stored_size=os.path.getsize(dst))
            saved += got
            rec["saved"] = got
            done.append(rec)
            _audit(root, f"{run} {f.action} {f.rel} saved={got}")
        except (OSError, ValueError) as e:
            skipped.append((f.rel, str(e)))
    cpu, wall = time.process_time() - c0, time.time() - t0
    measured = dict(wall_seconds=wall, cpu_seconds=cpu, cpu_energy_kwh_estimated=cpu * ASSUME["cpu_watts"] / 3.6e6)
    if tracker:
        try:
            tracker.stop()
            measured["codecarbon_energy_kwh"] = tracker.final_emissions_data.energy_consumed
            measured["codecarbon_co2_kg"] = tracker.final_emissions_data.emissions
        except Exception:
            pass
    manifest = dict(run=run, created=datetime.now().isoformat(timespec="seconds"), status="applied", actions=done,
                    saved_bytes=saved, skipped=skipped, measured=measured)
    with open(os.path.join(mdir, run + ".json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def rollback(root, run=None):
    mdir = os.path.join(root, STATE_DIR, "manifests")
    runs = sorted(x[:-5] for x in os.listdir(mdir)) if os.path.isdir(mdir) else []
    cands = []
    for r in runs:
        with open(os.path.join(mdir, r + ".json"), encoding="utf-8") as fh:
            m = json.load(fh)
        if m["status"] == "applied" and (run is None or r == run):
            cands.append(m)
    if not cands:
        return None
    m = cands[-1]
    restored, problems = 0, []
    for rec in reversed(m["actions"]):
        dst, stored = _inside(root, rec["path"]), _inside(root, rec["stored"])
        try:
            if rec["action"] in ("dedupe", "near"):
                if os.path.exists(dst):
                    problems.append((rec["path"], "target exists, left in trash")); continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(stored, dst)
            else:
                if os.path.exists(dst):
                    problems.append((rec["path"], "target exists, kept archive")); continue
                _decompress(stored, dst)
                if _sha(dst) != rec["sha256"]:
                    os.remove(dst); problems.append((rec["path"], "verification failed")); continue
                os.remove(stored)
            restored += 1
            _audit(root, f"{m['run']} rollback {rec['path']}")
        except OSError as e:
            problems.append((rec["path"], str(e)))
    m["status"] = "rolled_back" if not problems else "partially_rolled_back"
    with open(os.path.join(mdir, m["run"] + ".json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2)
    return dict(run=m["run"], restored=restored, problems=problems)


# ----------------------------------------------------------------------------- demo data
def make_demo(root, scale=1):
    os.makedirs(root, exist_ok=True)
    rnd = random.Random(7)
    now = time.time()

    def put(name, data, days):
        p = os.path.join(root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode())
        ts = now - days * 86400
        os.utime(p, (ts, ts))

    rows = int(4000 * scale)
    sales = "order_id,customer,item,qty,price\n" + "".join(f"{i},cust{rnd.randrange(300)},item{rnd.randrange(40)},{rnd.randrange(1, 9)},{rnd.randrange(100, 9000)}\n" for i in range(rows))
    put("sales/sales_2023.csv", sales, 40)
    put("sales/sales_2023_copy.csv", sales, 35)
    put("sales/sales_2023_final.csv", sales.replace("cust1,", "cust7,", 3) + "9999,cust2,item3,1,500\n", 33)
    logs = "".join(f"2025-01-{i % 28 + 1:02d} INFO service=api status=200 path=/v1/items/{rnd.randrange(50)} ms={rnd.randrange(5, 90)}\n" for i in range(int(20000 * scale)))
    put("logs/app_jan.log", logs, 300)
    put("logs/app_feb.log", logs.replace("INFO", "WARN", 40), 270)
    people = "name,email,phone\n" + "".join(f"user{i},user{i}@example.com,9{rnd.randrange(10**8, 10**9 - 1):09d}\n" for i in range(300))
    put("customers/customers.csv", people, 20)
    put("notes/experiment_notes.txt", ("sales forecast model notes. " * 400) + "latest run improved recall.\n", 25)
    put("tmp/tmp_scratch.csv", "a,b,c\n" + "1,2,3\n" * int(30000 * scale), 500)
    blob = os.urandom(int(400_000 * scale))
    put("media/photo_original.bin", blob, 60)
    put("media/photo_backup.bin", blob, 150)
    put("media/model_ckpt_old.bin", os.urandom(int(300_000 * scale)), 400)
    return root


# ----------------------------------------------------------------------------- local web app
import http.server  # noqa: E402
import secrets  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

APPLICABLE = {"dedupe", "archive", "compress", "near"}


class Api:
    """State and actions behind the web UI. One scan = one session, kept in memory."""

    def __init__(self):
        self.sessions, self.lock = {}, threading.Lock()

    @staticmethod
    def _dir(d):
        d = os.path.abspath(os.path.expanduser(str(d or "")))
        if not os.path.isdir(d):
            raise ValueError("That folder does not exist.")
        if os.path.dirname(d) == d:
            raise ValueError("Please choose a folder, not the drive root.")
        return d

    @staticmethod
    def _cfg(b, base=None):
        c = base or Cfg()
        if "keywords" in b:
            c.keywords = [k.strip().lower() for k in str(b["keywords"]).split(",") if k.strip()]
        if b.get("region") in REGIONS:
            c.region = b["region"]
        for key, cast in (("runs", int), ("threshold", int), ("acc", int)):
            if key in b:
                setattr(c, key, cast(b[key]))
        return c

    @staticmethod
    def _rows(files):
        return [dict(n=f.rel, t=f.type, gb=f.size / 1e9, d=round(f.days, 2), acc=2, rel=round(f.relevance, 3), dup=f.dup,
                     near=f.near, pii=bool(f.pii), o=f.of, sim=f.sim, score=f.score, action=f.action, reason=f.reason,
                     saved_bytes=f.saved, rr=f.protected_recent) for f in files]

    def scan(self, b):
        root, cfg = self._dir(b.get("dir")), self._cfg(b)
        files, meta = scan(root, cfg)
        if not files:
            raise ValueError("No files found in that folder.")
        sid = uuid.uuid4().hex
        with self.lock:
            self.sessions[sid] = dict(root=root, cfg=cfg, files=files, meta=meta)
        return dict(session=sid, dir=root, total_bytes=meta["total"], scan_seconds=meta["scan_seconds"], files=self._rows(files))

    def _session(self, b):
        s = self.sessions.get(b.get("session"))
        if not s:
            raise ValueError("Scan session expired. Please scan again.")
        return s

    def replan(self, b):
        s = self._session(b)
        self._cfg(b, s["cfg"])
        rescore(s["files"], s["cfg"], s["meta"]["total"])
        return dict(files=self._rows(s["files"]))

    def apply(self, b):
        s = self._session(b)
        cfg = self._cfg(b, s["cfg"])
        rescore(s["files"], cfg, s["meta"]["total"])
        chosen = set(b.get("approved") or [])
        for f in s["files"]:
            if f.rel not in chosen or f.action not in APPLICABLE:
                f.action, f.saved = "keep", 0
        cfg.include_near = True
        m = apply_plan(s["root"], s["files"], cfg, measure=bool(b.get("measure")))
        rescore(s["files"], cfg, s["meta"]["total"])
        return m

    def rollback(self, b):
        res = rollback(self._dir(b.get("dir")), b.get("run") or None)
        if res is None:
            raise ValueError("Nothing to roll back.")
        return res


def make_server(html_path, host="127.0.0.1", port=8765):
    token, api = secrets.token_urlsafe(24), Api()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            host_ok = self.headers.get("Host", "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost", "[::1]")
            return host_ok and (self.path == "/" or self.headers.get("X-GD-Token") == token)

        def do_GET(self):
            if not self._guard():
                return self._send(403, json.dumps(dict(error="Forbidden")))
            if self.path in ("/", "/index.html"):
                with open(html_path, encoding="utf-8") as fh:
                    return self._send(200, fh.read().replace("__GD_TOKEN__", token), "text/html")
            if self.path == "/api/ping":
                return self._send(200, json.dumps(dict(ok=True, version=VERSION, codec=CODEC_EXT, codecarbon=bool(EmissionsTracker))))
            self._send(404, json.dumps(dict(error="Not found")))

        def do_POST(self):
            if not self._guard():
                return self._send(403, json.dumps(dict(error="Forbidden")))
            route = self.path.rsplit("/", 1)[-1]
            if self.path.startswith("/api/") and route in ("scan", "replan", "apply", "rollback"):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n > 2_000_000:
                        raise ValueError("Request too large.")
                    body = json.loads(self.rfile.read(n) or b"{}")
                    return self._send(200, json.dumps(getattr(api, route)(body)))
                except (ValueError, KeyError, TypeError, OSError) as e:
                    return self._send(400, json.dumps(dict(error=str(e))))
            self._send(404, json.dumps(dict(error="Not found")))

    httpd = http.server.ThreadingHTTPServer((host, port), H)
    return httpd, token


# ----------------------------------------------------------------------------- CLI
def make_cfg(a):
    w = tuple(int(x) for x in a.weights.split(","))
    if len(w) != 4:
        sys.exit("--weights needs 4 numbers: relevance,access,recency,size")
    return Cfg(keywords=[k.strip().lower() for k in a.keywords.split(",") if k.strip()], region=a.region, runs=a.runs,
               threshold=a.threshold, weights=w, acc=a.acc, include_near=getattr(a, "apply_near", False), extrapolate=a.extrapolate)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="greendata", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def opts(p):
        p.add_argument("dir")
        p.add_argument("--keywords", default="", help="comma separated task keywords, e.g. sales,invoice")
        p.add_argument("--region", default="India", choices=sorted(REGIONS))
        p.add_argument("--runs", type=int, default=4, help="processing runs per month")
        p.add_argument("--threshold", type=int, default=35, help="archive files scoring below this")
        p.add_argument("--weights", default="35,25,25,15", help="relevance,access,recency,size")
        p.add_argument("--report", default="greendata_report.json")
        p.add_argument("--acc", type=int, default=2, help="assumed opens per month (file systems do not record this)")
        p.add_argument("--extrapolate", type=float, default=1.0, help="what-if: treat the folder as N times larger in the energy model")

    d = sub.add_parser("demo"); d.add_argument("dir"); d.add_argument("--scale", type=float, default=1)
    s = sub.add_parser("scan"); opts(s)
    p = sub.add_parser("apply"); opts(p)
    p.add_argument("--yes", action="store_true"); p.add_argument("--apply-near", action="store_true")
    p.add_argument("--measure", action="store_true", help="measure energy with CodeCarbon if installed")
    r = sub.add_parser("rollback"); r.add_argument("dir"); r.add_argument("--run")
    w = sub.add_parser("serve", help="open the web app (UI + backend) in your browser")
    w.add_argument("--port", type=int, default=8765); w.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        make_demo(a.dir, a.scale)
        print(f"Demo folder created: {a.dir}\nTry: python greendata.py scan {a.dir} --keywords sales")
        return 0
    if a.cmd == "serve":
        html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "greendata_prototype.html")
        if not os.path.exists(html):
            sys.exit("greendata_prototype.html must be in the same folder as greendata.py")
        httpd, _ = make_server(html, port=a.port)
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        print(f"GreenData AI is running at {url}\nOnly this computer can reach it. Press Ctrl+C to stop.")
        if not a.no_browser:
            import webbrowser
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0
    if a.cmd == "rollback":
        res = rollback(a.dir, a.run)
        if not res:
            print("Nothing to roll back."); return 1
        print(f"Run {res['run']}: restored {res['restored']} items.")
        for rel, why in res["problems"]:
            print(f"  problem: {rel}: {why}")
        return 0 if not res["problems"] else 2
    if not os.path.isdir(a.dir):
        sys.exit(f"Not a folder: {a.dir}")
    cfg = make_cfg(a)
    files, meta = scan(a.dir, cfg)
    if not files:
        print("No files found."); return 1
    summ = summarize(files, meta, cfg)
    print_plan(files, summ, cfg)
    write_report(a.report, a.dir, files, meta, summ, cfg)
    print(f"\nReport written to {a.report} (load it in the prototype with 'Load scan report').")
    if a.cmd == "scan":
        print("Dry run only. Nothing was changed. Use 'apply' to execute (reversible).")
        return 0
    n = len(approved(files, cfg))
    if not n:
        print("Nothing to apply."); return 0
    if not a.yes and input(f"\nApply {n} actions? Files are moved or compressed, never deleted. [y/N] ").lower() != "y":
        print("Cancelled."); return 0
    m = apply_plan(a.dir, files, cfg, measure=a.measure)
    print(f"\nApplied {len(m['actions'])} actions, actually freed {fmt_bytes(m['saved_bytes'])} (run {m['run']}).")
    for rel, why in m["skipped"]:
        print(f"  skipped {rel}: {why}")
    ms = m["measured"]
    print(f"Measured: {ms['wall_seconds']:.2f}s wall, {ms['cpu_seconds']:.2f}s CPU, about {fmt_energy(ms['cpu_energy_kwh_estimated'])} (CPU-time estimate).")
    if "codecarbon_energy_kwh" in ms:
        print(f"CodeCarbon: {fmt_energy(ms['codecarbon_energy_kwh'])}, {fmt_co2(ms['codecarbon_co2_kg'])} CO2")
    print(f"Undo any time with: python greendata.py rollback {a.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
