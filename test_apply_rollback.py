"""Apply and rollback must be exact. These tests try to break them."""
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import greendata as g

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == "nt"
WORDS = "alpha beta gamma delta sales invoice forecast model report data user order".split()


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def text(rnd, n):
    return " ".join(rnd.choice(WORDS) + str(rnd.randrange(50)) for _ in range(n)) + "\n"


def snap(root):
    """Content, modification time and permissions of every file outside .greendata."""
    out = {}
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if x != g.STATE_DIR]
        for f in fs:
            p = os.path.join(d, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (hashlib.sha256(fh.read()).hexdigest(), st.st_mtime_ns, st.st_mode & 0o777)
    return out


def same(test, before, after):
    test.assertEqual(sorted(before), sorted(after), "set of files differs")
    for k, (h, mt, mode) in before.items():
        test.assertEqual(after[k][0], h, f"content differs: {k}")
        test.assertLess(abs(after[k][1] - mt), 2_000_000_000, f"mtime differs: {k}")
        if not WINDOWS:
            test.assertEqual(after[k][2], mode, f"permissions differ: {k}")


def build(root, seed):
    rnd = random.Random(seed)
    names = ["a b", "ünï", "x-y", "data", "dir.with.dots", "日本語"]
    contents = [text(rnd, rnd.randrange(80, 3000)) for _ in range(8)]
    now = time.time()
    for i in range(rnd.randrange(25, 50)):
        sub = os.path.join("", *[rnd.choice(names) for _ in range(rnd.randrange(0, 3))])
        name = rnd.choice(["copy of x", "report", "x (1)", "file", "old", "naïve"]) + str(i) + "." + rnd.choice(["txt", "csv", "log", "bin", "dat"])
        p = os.path.join(root, sub, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        r = rnd.random()
        if r < .35:
            data = rnd.choice(contents).encode()
        elif r < .5:
            data = rnd.choice(contents).replace("alpha1", "zzz", 1).encode()
        elif r < .6:
            data = os.urandom(rnd.randrange(1, 40000))
        elif r < .65:
            data = b""
        else:
            data = text(rnd, rnd.randrange(10, 2000)).encode()
        with open(p, "wb") as fh:
            fh.write(data)
        ts = now - rnd.randrange(1, 700) * 86400
        os.utime(p, (ts, ts))
        if not WINDOWS and rnd.random() < .12:
            os.chmod(p, rnd.choice([0o444, 0o755]))


class ApplyRollbackTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def scan(self, **kw):
        cfg = g.Cfg(keywords=["sales"], threshold=kw.pop("threshold", 60), **kw)
        files, meta = g.scan(self.root, cfg)
        cfg.include_near = True
        return files, cfg

    def test_random_folders_come_back_exactly(self):
        """Content, modification time and permissions, after apply and rollback, on 12 random folders."""
        total = 0
        for seed in range(12):
            shutil.rmtree(self.root, ignore_errors=True)
            os.makedirs(self.root)
            build(self.root, seed)
            before = snap(self.root)
            files, cfg = self.scan()
            m = g.apply_plan(self.root, files, cfg)
            total += len(m["actions"])
            r = g.rollback(self.root, m["run"]) if m["actions"] else dict(problems=[])
            self.assertEqual(r["problems"], [], f"seed {seed}")
            same(self, before, snap(self.root))
        self.assertGreater(total, 30)  # the folders really did contain things to change

    def test_rollback_restores_modification_time(self):
        build(self.root, 3)
        old = {k: v[1] for k, v in snap(self.root).items()}
        files, cfg = self.scan(threshold=100)  # archive nearly everything
        m = g.apply_plan(self.root, files, cfg)
        self.assertTrue(any(a["action"] == "archive" for a in m["actions"]))
        g.rollback(self.root)
        for k, v in snap(self.root).items():
            self.assertLess(abs(v[1] - old[k]), 2_000_000_000, k)

    def test_never_overwrites_a_file_the_user_already_has(self):
        big = os.path.join(self.root, "big.txt")
        with open(big, "w") as fh:
            fh.write(text(random.Random(1), 60000))
        os.utime(big, (time.time() - 400 * 86400,) * 2)
        mine = b"MY OWN FILE, NOT FROM GREENDATA"
        with open(big + g.CODEC_EXT, "wb") as fh:
            fh.write(mine)
        with open(os.path.join(self.root, "small.txt"), "w") as fh:
            fh.write("x y z\n")
        cfg = g.Cfg(keywords=[], threshold=0, acc=1)
        files, _ = g.scan(self.root, cfg)
        self.assertEqual({f.rel: f.action for f in files}["big.txt"], "compress")
        m = g.apply_plan(self.root, files, cfg)
        self.assertEqual(read(big + g.CODEC_EXT), mine)
        self.assertIn("already exists", " ".join(why for _, why in m["skipped"]))
        g.rollback(self.root)
        self.assertEqual(read(big + g.CODEC_EXT), mine)
        self.assertTrue(os.path.exists(big))

    def test_hard_crash_in_the_middle_can_still_be_rolled_back(self):
        build(self.root, 7)
        before = snap(self.root)
        script = (
            "import sys, os; sys.path.insert(0, %r)\n"
            "import greendata as g\n"
            "cfg = g.Cfg(keywords=['sales'], threshold=60)\n"
            "files, _ = g.scan(%r, cfg); cfg.include_near = True\n"
            "n = [0]; real = g._audit\n"
            "def die(root, line):\n"
            "    real(root, line); n[0] += 1\n"
            "    if n[0] == 4: os._exit(9)\n"          # no cleanup at all, like a killed server
            "g._audit = die\n"
            "g.apply_plan(%r, files, cfg)\n" % (HERE, self.root, self.root))
        p = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 9, p.stderr.decode()[-300:])
        self.assertNotEqual(snap(self.root), before)  # it really stopped half way
        r = g.rollback(self.root)
        self.assertIsNotNone(r, "nothing to roll back after a crash")
        self.assertEqual(r["problems"], [])
        same(self, before, snap(self.root))

    def test_interrupt_exception_in_the_middle_can_be_rolled_back(self):
        build(self.root, 8)
        before = snap(self.root)
        files, cfg = self.scan()
        real, calls = shutil.move, [0]

        def boom(*a, **k):
            calls[0] += 1
            if calls[0] == 3:
                raise KeyboardInterrupt()
            return real(*a, **k)

        g.shutil.move = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                g.apply_plan(self.root, files, cfg)
        finally:
            g.shutil.move = real
        self.assertEqual(g.rollback(self.root)["problems"], [])
        same(self, before, snap(self.root))

    def test_two_applies_in_the_same_second_keep_separate_records(self):
        build(self.root, 11)
        before = snap(self.root)
        files, cfg = self.scan()
        m1 = g.apply_plan(self.root, files, cfg)
        m2 = g.apply_plan(self.root, files, cfg)  # same scan again, straight away
        self.assertNotEqual(m1["run"], m2["run"])
        self.assertEqual(m2["actions"], [])
        self.assertFalse([w for _, w in m2["skipped"] if "Errno" in w or "No such file" in w], "raw OS errors shown to the user")
        g.rollback(self.root)
        g.rollback(self.root)
        same(self, before, snap(self.root))

    def test_parallel_applies_do_not_corrupt_each_other(self):
        build(self.root, 13)
        before = snap(self.root)
        files, cfg = self.scan()
        out = []
        ts = [threading.Thread(target=lambda: out.append(g.apply_plan(self.root, files, cfg))) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len({m["run"] for m in out}), 4)
        self.assertEqual(sum(1 for m in out if m["actions"]), 1)  # only one run did the work
        while g.rollback(self.root):
            pass
        same(self, before, snap(self.root))

    def test_edited_file_is_left_alone(self):
        build(self.root, 5)
        files, cfg = self.scan()
        victim = next(f for f in files if f.action in ("dedupe", "archive", "near"))
        with open(victim.abs, "ab") as fh:
            fh.write(b"EDITED AFTER SCAN")
        after_edit = read(victim.abs)
        m = g.apply_plan(self.root, files, cfg)
        self.assertEqual(read(victim.abs), after_edit)
        self.assertIn(victim.rel, [r for r, _ in m["skipped"]])

    def test_blocked_rollback_can_be_retried(self):
        build(self.root, 3)
        before = snap(self.root)
        files, cfg = self.scan()
        m = g.apply_plan(self.root, files, cfg)
        moved = [a for a in m["actions"] if a["action"] in ("dedupe", "near")]
        self.assertTrue(moved)
        blocker = os.path.join(self.root, moved[0]["path"])
        os.makedirs(os.path.dirname(blocker), exist_ok=True)
        with open(blocker, "w") as fh:
            fh.write("someone made a new file here")
        r = g.rollback(self.root)
        self.assertEqual([p for p, _ in r["problems"]], [moved[0]["path"]])
        self.assertGreater(r["restored"], 0)
        os.remove(blocker)
        r2 = g.rollback(self.root)  # retry
        self.assertIsNotNone(r2)
        self.assertEqual((r2["problems"], r2["restored"]), ([], 1))
        same(self, before, snap(self.root))

    def test_result_numbers_match_the_disk(self):
        def disk():
            return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(self.root) for f in fs
                       if f != "audit.log" and not d.endswith("manifests"))

        for seed in (2, 4):
            shutil.rmtree(self.root, ignore_errors=True)
            os.makedirs(self.root)
            build(self.root, seed)
            files, cfg = self.scan()
            t0 = disk()
            m = g.apply_plan(self.root, files, cfg)
            t1 = disk()
            self.assertEqual(m["freed_now_bytes"], t0 - t1, "freed_now_bytes must equal the real drop in disk use")
            self.assertEqual(m["freed_now_bytes"] + m["in_trash_bytes"], m["saved_bytes"])
            self.assertEqual(m["saved_bytes"], sum(a["saved"] for a in m["actions"]))
            self.assertGreater(m["in_trash_bytes"], 0)

    def test_state_folder_is_tidy_after_a_full_rollback(self):
        build(self.root, 4)
        files, cfg = self.scan()
        g.apply_plan(self.root, files, cfg)
        g.rollback(self.root)
        left = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.root, g.STATE_DIR, "trash")) for f in fs]
        left += [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.root, g.STATE_DIR, "archive")) for f in fs]
        self.assertEqual(left, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, g.STATE_DIR, "verify.tmp")))

    def test_rollback_with_nothing_applied(self):
        build(self.root, 1)
        self.assertIsNone(g.rollback(self.root))


if __name__ == "__main__":
    unittest.main()
