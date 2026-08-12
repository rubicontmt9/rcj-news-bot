import json
import tempfile
import unittest
from pathlib import Path

from rcj_news import state as state_module
from rcj_news.state import State


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "seen.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_starts_empty(self):
        st = State.load(self.path)
        self.assertFalse(st.existed)
        self.assertTrue(st.is_new_source("a"))
        self.assertEqual(st.seen_ids("a"), set())

    def test_roundtrip(self):
        st = State.load(self.path)
        st.mark_seen("a", ["x", "y"])
        st.record_result("a", error=None, used_url="https://example.com/feed/")
        st.save()

        reloaded = State.load(self.path)
        self.assertTrue(reloaded.existed)
        self.assertFalse(reloaded.is_new_source("a"))
        self.assertEqual(reloaded.seen_ids("a"), {"x", "y"})
        self.assertEqual(reloaded.source("a")["used_url"], "https://example.com/feed/")

    def test_mark_seen_is_idempotent(self):
        st = State.load(self.path)
        st.mark_seen("a", ["x"])
        st.mark_seen("a", ["x", "y"])
        self.assertEqual(st.source("a")["seen"], ["x", "y"])

    def test_seen_list_is_capped_keeping_newest(self):
        st = State.load(self.path)
        total = state_module.MAX_SEEN_PER_SOURCE + 50
        st.mark_seen("a", [f"id{i}" for i in range(total)])
        seen = st.source("a")["seen"]
        self.assertEqual(len(seen), state_module.MAX_SEEN_PER_SOURCE)
        self.assertEqual(seen[-1], f"id{total - 1}")

    def test_known_links_dedupe_and_cap(self):
        st = State.load(self.path)
        st.set_known_links("w", ["b", "a", "b"])
        self.assertEqual(st.source("w")["known_links"], ["b", "a"])

    def test_error_then_success_clears_error(self):
        st = State.load(self.path)
        st.record_result("a", error="boom", used_url=None)
        self.assertEqual(st.source("a")["last_error"], "boom")
        st.record_result("a", error=None, used_url=None)
        self.assertNotIn("last_error", st.source("a"))
        self.assertIn("last_ok", st.source("a"))

    def test_conditional_headers_and_hash(self):
        st = State.load(self.path)
        st.record_fetch("w", etag='W/"1"', last_modified="Mon, 10 Aug 2026 00:00:00 GMT", content_hash="h")
        self.assertEqual(st.conditional_headers("w"), ('W/"1"', "Mon, 10 Aug 2026 00:00:00 GMT"))
        self.assertEqual(st.content_hash("w"), "h")

    def test_prune_removes_unknown_sources(self):
        st = State.load(self.path)
        st.mark_seen("keep", ["1"])
        st.mark_seen("drop", ["1"])
        st.prune({"keep"})
        self.assertIn("keep", st.data["sources"])
        self.assertNotIn("drop", st.data["sources"])

    def test_corrupt_file_is_recovered(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")
        st = State.load(self.path)
        self.assertFalse(st.existed)
        self.assertEqual(st.data["sources"], {})

    def test_saved_json_is_readable_utf8(self):
        st = State.load(self.path)
        st.mark_seen("千葉", ["x"])
        st.save()
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("千葉", raw)
        json.loads(raw)


if __name__ == "__main__":
    unittest.main()
