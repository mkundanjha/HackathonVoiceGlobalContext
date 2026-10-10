"""Offline mode must never call MERP or read unrelated local CSV records."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import context_api as api
from demo_export import demo_redact
import view_builder as builder
import renderer
from config import load_config
from datetime import datetime, timezone


class DemoModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.snapshot = {"glid": "123", "fetched_at": "2026-10-01T00:00:00Z", "origin": "api",
                         "data": {"kycdetails": {"city": "Indore"}, "ticketsdetails": []},
                         "datasets": {"buyer": {"history.csv": [{"product": "Bags"}]}, "seller": {}}}
        (self.directory / "123.json").write_text(json.dumps(self.snapshot))
        p = patch.dict(os.environ, {"GC_MODE": "demo", "GC_DEMO_DIR": str(self.directory)})
        p.start()
        self.addCleanup(p.stop)

    def test_demo_needs_no_credentials_or_http_or_fixture_writes(self):
        with patch.object(api, "_config", side_effect=AssertionError("credentials accessed")), \
                patch.object(api.httpx, "Client", side_effect=AssertionError("network accessed")), \
                patch.object(api, "save_response", side_effect=AssertionError("fixture mutated")):
            data, timings = api.fetch_context("123")
        self.assertEqual(data, self.snapshot["data"])
        self.assertEqual(timings["source_mode"], "demo")
        self.assertEqual(timings["snapshot_fetched_at"], self.snapshot["fetched_at"])

    def test_missing_glid_never_falls_back_to_live(self):
        with patch.object(api.httpx, "Client", side_effect=AssertionError("network accessed")):
            with self.assertRaises(api.ContextAPIError):
                api.fetch_context("456")

    def test_glid_cannot_escape_snapshot_directory(self):
        for glid in ("../123", "123/456", "", "abc"):
            with self.subTest(glid=glid), self.assertRaises(api.ContextAPIError):
                api.fetch_context(glid)

    def test_invalid_snapshot_is_reported(self):
        (self.directory / "123.json").write_text('{"glid":"456","data":{}}')
        with self.assertRaises(api.ContextAPIError):
            api.fetch_context("123")

    def test_segments_are_filtered(self):
        data, _ = api.fetch_context("123", ["kycdetails"])
        self.assertEqual(list(data), ["kycdetails"])

    def test_demo_dataset_history_is_role_specific(self):
        with patch.object(builder, "_dataset_index", side_effect=AssertionError("CSV accessed")):
            self.assertEqual(builder.collect_dataset_rows("123", "buyer"), self.snapshot["datasets"]["buyer"])
            self.assertEqual(builder.collect_dataset_rows("123", "seller"), {})
            self.assertEqual(builder.collect_dataset_rows("456", "buyer"), {})

    def test_live_mode_still_uses_original_api_calls(self):
        with patch.dict(os.environ, {"GC_MODE": "live"}), \
                patch.object(api, "_config", return_value={"timeout": 1}), \
                patch.object(api.httpx, "Client"), \
                patch.object(api, "generate_mapping_id", return_value="uid") as mapping, \
                patch.object(api, "get_context", return_value={"city": "Delhi"}) as context, \
                patch.object(api, "save_response"):
            data, _ = api.fetch_context("123")
        self.assertEqual(data, {"city": "Delhi"})
        self.assertEqual(mapping.call_args.args[:2], ("123", api.ALL_SEGMENTS))
        self.assertEqual(context.call_args.args[0], "uid")

    def test_invalid_mode_does_not_contact_merp(self):
        with patch.dict(os.environ, {"GC_MODE": "typo"}), self.assertRaises(api.ContextAPIError):
            api.fetch_context("123")

    def test_export_redaction_keeps_context_and_removes_contacts(self):
        result = demo_redact({"company_name": "Example Bags", "city": "Indore",
                              "email": "test@example.com", "mobile": 9876543210,
                              "gst_no": "22AAAAA0000A1Z5", "gst_verified_flag": 1,
                              "recording_url": "https://example.com/private",
                              "description": "Call 9876543210 or test@example.com",
                              "address": "Street 1", "ak": "credential"})
        text = json.dumps(result)
        for secret in ("9876543210", "test@example.com", "22AAAAA0000A1Z5", "https://", "credential", "Street 1"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["company_name"], "Example Bags")
        self.assertEqual(result["gst_verified_flag"], 1)

    def test_available_ids_exclude_manifest(self):
        (self.directory / "manifest.json").write_text("{}")
        self.assertEqual(api.available_demo_glids(), ["123"])

    def test_snapshot_date_is_not_replaced_by_current_build_date(self):
        with patch.object(builder, "emit"):
            sources, state = builder.collect_sources("123", "buyer")
        self.assertEqual(state["context_api"]["fetched_at"], self.snapshot["fetched_at"])
        self.assertIn("demo_snapshot", sources)
        body = renderer.render_view("123", "seller", sources, {}, load_config()["context_files"])
        self.assertIn("Offline demo: saved API capture from 2026-10-01", body)

    def test_switching_modes_refreshes_a_fresh_cached_context(self):
        contexts = self.directory / "contexts"
        path = contexts / "123" / "buyer.md"
        path.parent.mkdir(parents=True)
        meta = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "source_state": {"context_api": {"source_mode": "live"}}}
        path.write_text("---\n" + json.dumps(meta) + "\n---\n\n# Context")
        with patch.object(builder, "CONTEXTS_DIR", contexts), \
                patch.object(builder, "SAMPLES_DIR", self.directory / "samples"), \
                patch.object(builder, "emit"), \
                patch.object(builder, "generate_view", return_value=path) as generate, \
                patch.object(builder, "_ensure_guide", return_value="refreshed"):
            self.assertEqual(builder.get_view("123", "buyer"), "refreshed")
        generate.assert_called_once_with("123", "buyer")


if __name__ == "__main__":
    unittest.main()
