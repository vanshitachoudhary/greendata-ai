import hashlib
import json
import os
import tempfile
import unittest

import greendata as g


def tree_hashes(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != g.STATE_DIR]
        for n in fn:
            p = os.path.join(dp, n)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


class GreenDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "demo")
        g.make_demo(self.root, scale=0.5)
        self.cfg = g.Cfg(keywords=["sales"])

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, cfg=None):
        files, meta = g.scan(self.root, cfg or self.cfg)
        return {f.rel: f for f in files}, files, meta

    def test_exact_and_near_duplicates(self):
        by, _, _ = self.scan()
        self.assertTrue(by["sales/sales_2023_copy.csv"].dup)
        self.assertEqual(by["sales/sales_2023_copy.csv"].of, "sales/sales_2023.csv")
        self.assertTrue(by["media/photo_backup.bin"].dup)
        self.assertFalse(by["sales/sales_2023.csv"].dup)
        near = by["sales/sales_2023_final.csv"]
        self.assertTrue(near.near)
        self.assertGreaterEqual(near.sim, 80)

    def test_pii_is_protected(self):
        by, _, _ = self.scan()
        c = by["customers/customers.csv"]
        self.assertIn("email", c.pii)
        self.assertEqual(c.action, "protect")

    def test_relevant_recent_data_is_never_archived(self):
        by, _, _ = self.scan(g.Cfg(keywords=["sales", "forecast"], threshold=95))
        n = by["notes/experiment_notes.txt"]
        self.assertTrue(n.protected_recent)
        self.assertEqual(n.action, "keep")

    def test_old_irrelevant_data_is_archived(self):
        by, _, _ = self.scan()
        self.assertEqual(by["tmp/tmp_scratch.csv"].action, "archive")
        self.assertGreater(by["tmp/tmp_scratch.csv"].saved, 0)

    def test_incompressible_data_is_not_compressed(self):
        by, _, _ = self.scan()
        self.assertEqual(by["media/model_ckpt_old.bin"].action, "keep")

    def test_scan_does_not_change_last_used(self):
        a, _, _ = self.scan()
        b, _, _ = self.scan()
        self.assertAlmostEqual(a["tmp/tmp_scratch.csv"].days, b["tmp/tmp_scratch.csv"].days, delta=1)

    def test_apply_then_rollback_restores_every_byte(self):
        before = tree_hashes(self.root)
        files, meta = g.scan(self.root, self.cfg)
        m = g.apply_plan(self.root, files, self.cfg)
        self.assertGreater(m["saved_bytes"], 0)
        after = tree_hashes(self.root)
        self.assertNotEqual(before, after)
        self.assertIn("customers/customers.csv".replace("/", os.sep), after)  # protected file untouched
        res = g.rollback(self.root)
        self.assertEqual(res["problems"], [])
        self.assertEqual(before, tree_hashes(self.root))
        self.assertIsNone(g.rollback(self.root))  # nothing left to roll back

    def test_apply_skips_files_changed_after_scan(self):
        files, meta = g.scan(self.root, self.cfg)
        with open(os.path.join(self.root, "tmp", "tmp_scratch.csv"), "ab") as fh:
            fh.write(b"changed\n")
        m = g.apply_plan(self.root, files, self.cfg)
        self.assertTrue(any(r == "tmp/tmp_scratch.csv" for r, _ in m["skipped"]))
        self.assertTrue(os.path.exists(os.path.join(self.root, "tmp", "tmp_scratch.csv")))

    def test_report_matches_prototype_format(self):
        files, meta = g.scan(self.root, self.cfg)
        summ = g.summarize(files, meta, self.cfg)
        path = os.path.join(self.tmp.name, "r.json")
        g.write_report(path, self.root, files, meta, summ, self.cfg)
        with open(path) as fh:
            data = json.load(fh)
        keys = {"n", "t", "gb", "d", "acc", "rel", "dup", "near", "pii", "o", "sim"}
        self.assertTrue(all(keys <= set(x) for x in data["files"]))
        self.assertLess(summ["after"]["storage"], summ["before"]["storage"])

    def test_cli_end_to_end(self):
        rep = os.path.join(self.tmp.name, "rep.json")
        self.assertEqual(g.main(["scan", self.root, "--keywords", "sales", "--report", rep]), 0)
        self.assertEqual(g.main(["apply", self.root, "--keywords", "sales", "--report", rep, "--yes"]), 0)
        self.assertEqual(g.main(["rollback", self.root]), 0)


if __name__ == "__main__":
    unittest.main()


class ServerTests(unittest.TestCase):
    def setUp(self):
        import http.client, threading
        self.http = http.client
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "demo")
        g.make_demo(self.root, scale=0.5)
        html = os.path.join(self.tmp.name, "index.html")
        with open(html, "w") as fh:
            fh.write("<html>token=__GD_TOKEN__</html>")
        self.httpd, self.token = g.make_server(html, port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=True, host=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-GD-Token"] = self.token
        if host:
            h["Host"] = host
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_page_gets_token_and_api_needs_it(self):
        st, data = self.call("GET", "/", token=False)
        self.assertEqual(st, 200)
        self.assertIn(self.token, data.decode())
        self.assertEqual(self.call("GET", "/api/ping", token=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/ping")[0], 200)

    def test_rejects_foreign_host_header(self):
        self.assertEqual(self.call("GET", "/", host="evil.example.com")[0], 403)
        self.assertEqual(self.call("POST", "/api/scan", dict(dir=self.root), host="evil.example.com")[0], 403)

    def test_bad_folder_is_a_clean_error(self):
        st, data = self.call("POST", "/api/scan", dict(dir=os.path.join(self.tmp.name, "nope")))
        self.assertEqual(st, 400)
        self.assertIn("does not exist", json.loads(data)["error"])

    def test_scan_replan_apply_rollback_over_http(self):
        before = tree_hashes(self.root)
        st, data = self.call("POST", "/api/scan", dict(dir=self.root, keywords="sales"))
        self.assertEqual(st, 200)
        scan = json.loads(data)
        rows = {r["n"]: r for r in scan["files"]}
        self.assertEqual(rows["customers/customers.csv"]["action"], "protect")
        self.assertEqual(rows["sales/sales_2023_copy.csv"]["action"], "dedupe")
        # a stricter threshold archives more files
        _, d2 = self.call("POST", "/api/replan", dict(session=scan["session"], threshold=60))
        rows2 = {r["n"]: r for r in json.loads(d2)["files"]}
        self.assertGreaterEqual(sum(r["action"] == "archive" for r in rows2.values()), sum(r["action"] == "archive" for r in rows.values()))
        # apply only the duplicate the user approved; a PII file listed by mistake is ignored
        st, d3 = self.call("POST", "/api/apply", dict(session=scan["session"], threshold=35,
                           approved=["sales/sales_2023_copy.csv", "customers/customers.csv"]))
        self.assertEqual(st, 200)
        res = json.loads(d3)
        self.assertEqual([a["path"] for a in res["actions"]], ["sales/sales_2023_copy.csv"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "customers", "customers.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "sales", "sales_2023_copy.csv")))
        st, d4 = self.call("POST", "/api/rollback", dict(dir=self.root))
        self.assertEqual(st, 200)
        self.assertEqual(before, tree_hashes(self.root))
