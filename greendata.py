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
import contextlib
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
import threading
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
# Context rules. A bare 10 or 12 digit number is only treated as personal data when the text around it says so.
PHONE_FORMATTED = re.compile(r"(?<!\d)(?:\+91[\s-]?[6-9]\d{9}|(?:91|0)[\s-][6-9]\d{9}|[6-9]\d{4}[\s-]\d{5})(?!\d)")
PHONE_WORDS = re.compile(r"\b(?:phone|mobile|mob|cell|contact|tel|telephone|whatsapp|call|ph)\b", re.I)
AADHAAR_WORDS = re.compile(r"(?:aadhaar|aadhar|uidai|\buid\b|\bid\b|identity|\bkyc\b|आधार)", re.I)
NOT_PERSONAL = {"order", "orders", "invoice", "inv", "tracking", "awb", "ref", "reference", "txn", "transaction", "sku",
                "serial", "ticket", "bill", "po", "utr", "rrn", "batch", "lot", "account", "acct", "card", "isbn"}
PAN_HOLDER = set("ABCFGHLJPT")  # 4th letter of a real PAN is the holder type
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


def _after_id_word(text, pos):
    """True when one of the two words just before pos says the number is an order, invoice, tracking or similar ID."""
    words = re.findall(r"[A-Za-z]+", text[max(0, pos - 40):pos])[-2:]
    return any(w.lower() in NOT_PERSONAL for w in words)


def _phone(text):
    """Indian mobile numbers. Formatted numbers always count. A bare 10 digit number counts only with phone context:
    a phone word just before it, a phone column in the header line, or a list of at least 5 different such numbers.
    A number right after words like order, invoice or tracking never counts (those are IDs, not people)."""
    if any(not _after_id_word(text, m.start()) for m in PHONE_FORMATTED.finditer(text)):
        return True
    header = PHONE_WORDS.search(text[:text.find("\n") if "\n" in text[:2000] else 200]) is not None
    bare = set()
    for m in PII_RE["phone"].finditer(text):
        if _after_id_word(text, m.start()):
            continue
        if header or PHONE_WORDS.search(text[max(0, m.start() - 30):m.start()]):
            return True
        bare.add(m.group())
    return len(bare) >= 5


def _aadhaar(text):
    """12 digit Aadhaar with a valid Verhoeff check digit. Written as 4-4-4 it counts. Written as 12 plain digits it needs an Aadhaar word nearby."""
    for m in PII_RE["aadhaar"].finditer(text):
        raw = m.group()
        if _after_id_word(text, m.start()) or not verhoeff_ok(re.sub(r"\s", "", raw)):
            continue
        if re.search(r"\s", raw) or AADHAAR_WORDS.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def has_pii(text):
    found = []
    if PII_RE["email"].search(text):
        found.append("email")
    if _phone(text):
        found.append("phone")
    if _aadhaar(text):
        found.append("aadhaar")
    if any(m.group()[3] in PAN_HOLDER for m in PII_RE["PAN"].finditer(text)):
        found.append("PAN")
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


_MASK = (1 << 64) - 1
_RNG = random.Random(42)
_PERMS = [(_RNG.getrandbits(64) | 1, _RNG.getrandbits(64)) for _ in range(64)]
SIG_K = 1000  # sketch size: only the 1000 smallest shingle hashes feed the signature (bottom-k sampling, same for every file)


def _signature(shingles):
    """MinHash signature. Bottom-k sampling keeps it fast on big files; candidates are still confirmed with the exact Jaccard similarity."""
    sample = sorted(shingles)[:SIG_K] if len(shingles) > SIG_K else shingles
    return [min((a * h + b) & _MASK for h in sample) for a, b in _PERMS]


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
    p0 = time.process_time()
    for f in files:
        try:
            analyze(f, cfg)
            restore_times(f)
        except OSError as e:
            print(f"  skipped {f.rel}: {e}", file=sys.stderr)
    p1 = time.process_time()
    files = [f for f in files if f.sha]
    find_duplicates(files, cfg)
    p2 = time.process_time()
    total = sum(f.size for f in files)
    rescore(files, cfg, total)
    p3 = time.process_time()
    # CPU seconds spent in each phase (used by the dashboard and by benchmark.py)
    phases = dict(read_hash=p1 - p0, duplicates=p2 - p1, score_decide=p3 - p2)
    meta = dict(scan_seconds=time.time() - t0, scan_cpu_seconds=time.process_time() - c0, total=total, phases=phases)
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
    t = pg * ASSUME["min_per_gb"] + ASSUME["fixed_min_run"]
    return dict(storage=storage, time_min=t, kwh=e, cpu_h=t * runs / 60,  # modelled: one busy core during each run
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
                          ("CPU time / month", "cpu_h", lambda v: fmt_time(v * 60)), ("Energy / month", "kwh", fmt_energy), ("CO2 / month", "co2", fmt_co2),
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
        if not zstd:
            raise RuntimeError("this file was compressed with zstd, run: pip install zstandard")
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


_ROOT_LOCKS, _ROOT_LOCKS_GUARD = {}, threading.Lock()
RESTORABLE_STATES = ("applied", "partially_rolled_back")


@contextlib.contextmanager
def _root_lock(root):
    """One apply or rollback at a time per folder, so two clicks (or two visitors) cannot corrupt each other."""
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.setdefault(os.path.abspath(root), threading.Lock())
    with lock:
        yield


def _new_run(base):
    """Run ids sort by time. The random suffix keeps two runs in the same second apart."""
    while True:
        run = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
        if not os.path.exists(os.path.join(base, "manifests", run + ".json")) and not os.path.exists(os.path.join(base, "trash", run)):
            return run


def _rel(root, p):
    return os.path.relpath(p, root).replace(os.sep, "/")


def _restore_meta(dst, rec):
    """Put back the permissions and the times the file had before it was changed."""
    try:
        if "mtime_ns" in rec:
            os.utime(dst, ns=(rec.get("atime_ns", rec["mtime_ns"]), rec["mtime_ns"]))
        if "mode" in rec:
            os.chmod(dst, rec["mode"])
    except OSError:
        pass


def apply_plan(root, files, cfg, measure=False):
    with _root_lock(root):
        return _apply_locked(root, files, cfg, measure)


def _apply_locked(root, files, cfg, measure):
    base = os.path.join(root, STATE_DIR)
    os.makedirs(base, exist_ok=True)
    run = _new_run(base)
    trash, arch, mdir = os.path.join(base, "trash", run), os.path.join(base, "archive", run), os.path.join(base, "manifests")
    for d in (trash, arch, mdir):
        os.makedirs(d, exist_ok=True)
    jpath, tmpchk = os.path.join(mdir, run + ".journal"), os.path.join(base, "verify.tmp")
    tracker = None
    if measure and EmissionsTracker:
        try:
            tracker = EmissionsTracker(project_name="greendata-apply", log_level="error", save_to_file=False)
            tracker.start()
        except Exception:
            tracker = None
    t0, c0 = time.time(), time.process_time()
    done, saved, skipped = [], 0, []
    # The journal is written BEFORE each change and flushed. If the program is killed half way, rollback can still find every file.
    with open(jpath, "a", encoding="utf-8") as jr:
        def jw(kind, **kw):
            jr.write(json.dumps(dict(k=kind, **kw)) + "\n")
            jr.flush()

        for f in approved(files, cfg):
            created, src = [], None
            try:
                src = _inside(root, f.rel)
                if not os.path.isfile(src):
                    skipped.append((f.rel, "file is no longer at this location (already applied, moved or deleted)")); continue
                st = os.stat(src)  # before reading it, so the access time we record is the real one
                if _sha(src) != f.sha:
                    skipped.append((f.rel, "file changed since the scan")); continue
                meta = dict(mode=st.st_mode & 0o7777, mtime_ns=st.st_mtime_ns, atime_ns=st.st_atime_ns)
                if f.action in ("dedupe", "near"):
                    keep = _inside(root, f.of)
                    if not os.path.exists(keep) or (f.action == "dedupe" and _sha(keep) != f.sha):
                        skipped.append((f.rel, "original not found or different")); continue
                    dst = os.path.join(trash, f.rel)
                    rec = dict(action=f.action, path=f.rel, stored=_rel(root, dst), sha256=f.sha, size=f.size, kept=f.of, **meta)
                    jw("begin", **rec)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    created.append(dst)
                    shutil.move(src, dst)
                    got = f.size
                else:
                    dst = (os.path.join(arch, f.rel) if f.action == "archive" else src) + CODEC_EXT
                    if os.path.lexists(dst):
                        skipped.append((f.rel, f"a file named {os.path.basename(dst)} already exists here, left untouched")); continue
                    rec = dict(action=f.action, path=f.rel, stored=_rel(root, dst), sha256=f.sha, size=f.size, **meta)
                    jw("begin", **rec)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    created += [dst, tmpchk]
                    _compress(src, dst)
                    _decompress(dst, tmpchk)
                    ok = _sha(tmpchk) == f.sha
                    os.remove(tmpchk)
                    if not ok:
                        os.remove(dst); jw("undo", path=f.rel); skipped.append((f.rel, "verification failed")); continue
                    rec["stored_size"] = os.path.getsize(dst)
                    got = f.size - rec["stored_size"]
                    if got <= 0:
                        os.remove(dst); jw("undo", path=f.rel); skipped.append((f.rel, "no space saved")); continue
                    os.remove(src)
                saved += got
                rec["saved"] = got
                done.append(rec)
                jw("done", path=f.rel, saved=got)
                _audit(root, f"{run} {f.action} {f.rel} saved={got}")
            except (OSError, ValueError, RuntimeError, lzma.LZMAError) as e:
                if src and os.path.exists(src):  # the original is still in place, so remove anything half written
                    for p in created:
                        with contextlib.suppress(OSError):
                            if os.path.isfile(p):
                                os.remove(p)
                jw("undo", path=f.rel)
                skipped.append((f.rel, str(e)))
    with contextlib.suppress(OSError):
        os.remove(tmpchk)
    cpu, wall = time.process_time() - c0, time.time() - t0
    measured = dict(wall_seconds=wall, cpu_seconds=cpu, cpu_energy_kwh_estimated=cpu * ASSUME["cpu_watts"] / 3.6e6)
    if tracker:
        try:
            tracker.stop()
            measured["codecarbon_energy_kwh"] = tracker.final_emissions_data.energy_consumed
            measured["codecarbon_co2_kg"] = tracker.final_emissions_data.emissions
        except Exception:
            pass
    # saved_bytes = how much leaves the working folder. Only compression really frees disk now.
    # Exact and near copies are moved into .greendata/trash and stay on disk until that folder is deleted.
    freed_now = sum(r["saved"] for r in done if r["action"] in ("archive", "compress"))
    manifest = dict(run=run, created=datetime.now().isoformat(timespec="seconds"), status="applied", actions=done,
                    saved_bytes=saved, freed_now_bytes=freed_now, in_trash_bytes=saved - freed_now, skipped=skipped, measured=measured)
    _write_manifest(root, manifest)
    with contextlib.suppress(OSError):
        os.remove(jpath)
    return manifest


def _write_manifest(root, m):
    path = os.path.join(root, STATE_DIR, "manifests", m["run"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2)
    os.replace(tmp, path)


def _load_runs(root):
    """All runs that can still be rolled back, oldest first. A run that was cut short (a journal without a manifest) is rebuilt from its journal."""
    mdir = os.path.join(root, STATE_DIR, "manifests")
    if not os.path.isdir(mdir):
        return []
    names = os.listdir(mdir)
    runs = []
    for n in sorted(names):
        if n.endswith(".json"):
            with open(os.path.join(mdir, n), encoding="utf-8") as fh:
                runs.append(json.load(fh))
        elif n.endswith(".journal") and n[:-8] + ".json" not in names:
            begins, finished, undone = {}, {}, set()
            with open(os.path.join(mdir, n), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue  # a half written last line
                    kind = d.pop("k", "")
                    if kind == "begin":
                        begins[d["path"]] = d
                    elif kind == "done":
                        finished[d["path"]] = d.get("saved", 0)
                    elif kind == "undo":
                        undone.add(d["path"])
            acts = []
            for p, rec in begins.items():
                interrupted = p not in finished and p not in undone and os.path.exists(os.path.join(root, rec["stored"]))
                if p in finished or interrupted:
                    rec["saved"] = finished.get(p, 0)
                    acts.append(rec)
            if acts:
                runs.append(dict(run=n[:-8], created="", status="applied", actions=acts, saved_bytes=sum(a["saved"] for a in acts),
                                 skipped=[], recovered=True, measured={}))
    return [m for m in runs if m.get("status") in RESTORABLE_STATES]


def _restore_one(root, rec):
    """Bring one file back. It looks at what is really on disk, so it is safe to run again after an interruption. Returns None or a problem text."""
    dst, stored = _inside(root, rec["path"]), _inside(root, rec["stored"])
    has_dst, has_stored = os.path.lexists(dst), os.path.exists(stored)
    if rec["action"] in ("dedupe", "near"):
        if has_stored and has_dst:
            return "target exists, left in trash"
        if has_stored:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(stored, dst)
        elif not has_dst:
            return "the stored copy is missing"
        if _sha(dst) != rec["sha256"]:
            return "restored file does not match the recorded hash"
    else:
        if has_stored and has_dst:
            if _sha(dst) != rec["sha256"]:
                return "target exists, kept archive"
            os.remove(stored)  # the original is intact (an interrupted apply), drop our extra copy
        elif has_stored:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".gd-restoring"
            try:
                _decompress(stored, tmp)
                if _sha(tmp) != rec["sha256"]:
                    return "verification failed"
                os.replace(tmp, dst)
            finally:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
            os.remove(stored)
        elif not has_dst:
            return "the archived copy is missing"
    _restore_meta(dst, rec)
    return None


def _prune_empty(path, stop):
    """Remove empty folders left behind in .greendata after a rollback."""
    for d, _, _ in sorted(os.walk(path), key=lambda x: -len(x[0])):
        with contextlib.suppress(OSError):
            os.rmdir(d)
    with contextlib.suppress(OSError):
        os.rmdir(stop)


def rollback(root, run=None):
    with _root_lock(root):
        cands = [m for m in _load_runs(root) if run is None or m["run"] == run]
        if not cands:
            return None
        m = cands[-1]
        restored, problems = 0, []
        for rec in reversed(m["actions"]):
            if rec.get("restored"):
                continue
            try:
                why = _restore_one(root, rec)
            except (OSError, ValueError, RuntimeError, lzma.LZMAError) as e:
                why = str(e)
            if why:
                problems.append((rec["path"], why))
                continue
            rec["restored"] = True
            restored += 1
            _audit(root, f"{m['run']} rollback {rec['path']}")
        m["status"] = "rolled_back" if not problems else "partially_rolled_back"
        _write_manifest(root, m)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(root, STATE_DIR, "manifests", m["run"] + ".journal"))
        if not problems:
            for sub in ("trash", "archive"):
                p = os.path.join(root, STATE_DIR, sub, m["run"])
                if os.path.isdir(p):
                    _prune_empty(p, p)
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


def cpu_snapshot(sample=0.2):
    """Measured CPU usage of this machine right now (psutil if installed, else the 1-minute load average)."""
    cores = os.cpu_count() or 1
    pct, source = None, "unavailable"
    try:
        import psutil
        pct, source = psutil.cpu_percent(interval=sample), "psutil"
    except Exception:
        try:
            pct, source = min(100.0, os.getloadavg()[0] / cores * 100), "load average"
        except (AttributeError, OSError):  # Windows without psutil
            pass
    return dict(cores=cores, system_pct=pct, source=source)


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
        if "weights" in b:
            w = tuple(max(0, int(x)) for x in b["weights"])
            if len(w) != 4 or sum(w) <= 0:
                raise ValueError("weights needs 4 numbers: relevance, access, recency, size.")
            c.weights = w
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
        return dict(session=sid, dir=root, total_bytes=meta["total"], scan_seconds=meta["scan_seconds"], files=self._rows(files),
                    cpu=self._cpu_info(meta))

    @staticmethod
    def _cpu_info(meta):
        """Measured CPU use of the scan: CPU time / elapsed time, per phase, plus the machine's current load."""
        wall, cpu = meta["scan_seconds"], meta["scan_cpu_seconds"]
        snap = cpu_snapshot(0.0)
        return dict(wall_seconds=wall, cpu_seconds=cpu, utilisation_pct=100 * cpu / wall if wall else 0.0,
                    phases=meta.get("phases", {}), **snap)

    def cpu(self, b):
        return cpu_snapshot()

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

        def _drain(self):
            """Read and drop the request body. Without this, Windows resets the connection when we answer first."""
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 2_000_000)
                if n > 0:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass

        def do_POST(self):
            if not self._guard():
                self._drain()
                return self._send(403, json.dumps(dict(error="Forbidden")))
            route = self.path.rsplit("/", 1)[-1]
            if self.path.startswith("/api/") and route in ("scan", "replan", "apply", "rollback", "cpu"):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n > 2_000_000:
                        raise ValueError("Request too large.")
                    body = json.loads(self.rfile.read(n) or b"{}")
                    return self._send(200, json.dumps(getattr(api, route)(body)))
                except (ValueError, KeyError, TypeError, OSError) as e:
                    return self._send(400, json.dumps(dict(error=str(e))))
            self._drain()
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
    print(f"\nApplied {len(m['actions'])} actions (run {m['run']}). Freed on disk now: {fmt_bytes(m['freed_now_bytes'])}.")
    if m["in_trash_bytes"]:
        print(f"Copies moved to {STATE_DIR}/trash: {fmt_bytes(m['in_trash_bytes'])}. They stay on disk until you delete that folder (after that, rollback cannot bring them back).")
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
