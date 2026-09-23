import unittest
from datetime import timedelta
from nginxlog import parse_line, summarize

L1 = '203.0.113.9 - alice [10/Oct/2026:13:55:36 +0700] "GET /api/v1/items?id=3 HTTP/1.1" 200 512 "https://ex.com/" "Mozilla/5.0 (X11)"'
L2 = '198.51.100.2 - - [10/Oct/2026:13:56:01 +0000] "POST /login HTTP/2.0" 302 - "-" "curl/8.0 \\"quoted\\""'
L3 = '198.51.100.2 - - [10/Oct/2026:13:57:00 +0000] "-" 400 0 "-" "-"'

class T(unittest.TestCase):
    def test_full(self):
        r = parse_line(L1)
        self.assertEqual((r["ip"], r["user"], r["method"], r["path"], r["protocol"]),
                         ("203.0.113.9", "alice", "GET", "/api/v1/items?id=3", "HTTP/1.1"))
        self.assertEqual((r["status"], r["bytes"], r["referer"]), (200, 512, "https://ex.com/"))
        self.assertEqual(r["time"].utcoffset(), timedelta(hours=7))
        self.assertEqual(r["time"].minute, 55)
    def test_dashes_and_escapes(self):
        r = parse_line(L2)
        self.assertIsNone(r["user"]); self.assertIsNone(r["referer"])
        self.assertEqual(r["bytes"], 0)
        self.assertEqual(r["user_agent"], 'curl/8.0 "quoted"')
    def test_bad_request_string(self):
        r = parse_line(L3)
        self.assertIsNone(r["method"]); self.assertEqual(r["status"], 400)
    def test_garbage(self):
        self.assertIsNone(parse_line("not a log line"))
    def test_summary(self):
        s = summarize([L1, L2, L3, "junk", L1])
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_status"], {200: 2, 302: 1, 400: 1})
        self.assertEqual(s["top_paths"][0], ("/api/v1/items?id=3", 2))
