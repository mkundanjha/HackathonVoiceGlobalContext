"""Offline regression checks: no API calls, servers, or real context writes."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import compact
import context as prompt_context
import context_updater as updater
import guide
import renderer
import summarizer
import view_builder as builder
from config import load_config


def skeleton(role="seller", **sections):
    return "# Context\n\n" + "\n\n".join(
        f"## {s}\n" + "\n".join(sections.get(s, ["- Unknown."])) for s in renderer.SECTIONS[role])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.contexts = self.root / "contexts"
        self.samples = self.root / "samples"
        self.cfg = copy.deepcopy(load_config())
        for module in (builder, updater, summarizer):
            for name, value in (("ROOT", self.root), ("CONTEXTS_DIR", self.contexts),
                                ("SAMPLES_DIR", self.samples), ("DATASET_DIR", self.root / "datasets")):
                if hasattr(module, name):
                    p = patch.object(module, name, value)
                    p.start()
                    self.addCleanup(p.stop)
            for name, value in (("emit", lambda *a, **k: None), ("load_config", lambda: self.cfg)):
                p = patch.object(module, name, value)
                p.start()
                self.addCleanup(p.stop)
        for name, value in (("emit", lambda *a, **k: None), ("load_config", lambda: self.cfg)):
            p = patch.object(guide, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.path = self.contexts / "123" / "seller.md"

    def write(self, body, **meta):
        builder.write_view_file(self.path, {"context_version": 1, **meta}, body)

    def test_compaction_keeps_old_unresolved_actions_and_complete_facts(self):
        action = "- Callback requested for 12 Oct 2026 at 15:00. [from chat 01 Sep 10:00]"
        fact = "- Needs 800 kg LD granules, delivery by 15 Oct 2026. [chat 10 Oct 10:00]"
        body = skeleton(**{"Pending Actions and Clarifications": [action],
                           "Products and Categories": [fact] + [f"- Product {i} with extra details." for i in range(30)]})
        result = compact.compact_body(body, "seller", {"max_words": 90, "source_bullets": 2})
        self.assertIn(action, result)
        self.assertIn(fact, result)
        self.assertLess(len(result.split()), len(body.split()))
        self.assertEqual(renderer.SECTIONS["seller"], list(renderer._sections_of(result)))

    def test_correction_and_resolution_do_not_delete_unmentioned_fact(self):
        old = "- Needs 500 kg. [chat 09 Oct 10:00]"
        untouched = "- Delivery city is Indore. [chat 09 Oct 10:00]"
        pending = "- Send catalog tomorrow. [from chat 09 Oct 10:00]"
        self.write(skeleton(**{"Products and Categories": [old, untouched],
                               "Pending Actions and Clarifications": [pending]}))
        extraction = {"updates": [{"section": "Products and Categories", "text": "Needs 800 kg.",
                                   "replaces": [old, "- invented deletion"]}], "resolved": [pending]}
        with patch.object(updater, "_extract", return_value=extraction):
            updater._update("123", "seller", "chat", [])
        meta, body = builder.read_view_file(self.path)
        self.assertNotIn(old, body)
        self.assertNotIn(pending, body)
        self.assertIn(untouched, body)
        self.assertIn("Needs 800 kg.", body)
        self.assertIn(old, meta["suppressed_bullets"])
        self.assertEqual(builder.read_view_file(self.path.parent / "history/seller.v1.md")[0]["context_version"], 1)

    def test_no_call_request_removes_resolved_callback_and_blocks_new_one(self):
        pending = "- Call on 11 Oct. [from voice 09 Oct 10:00]"
        self.write(skeleton(**{"Pending Actions and Clarifications": [pending]}))
        with patch.object(updater, "_extract", return_value={
                "resolved": [pending], "pending": ["Call tomorrow"], "contact_preference": "no_calls"}):
            updater._update("123", "seller", "voice", [])
        _, body = builder.read_view_file(self.path)
        self.assertIn("no outbound calls", body)
        self.assertNotIn(pending, body)
        self.assertNotIn("Call tomorrow", body)

    def test_rescheduled_meeting_slot_reaches_next_voice_prompt(self):
        old = '- Meeting requested for 12 Oct 2026 at 14:00 IST; executive confirmation pending. [from chat 10 Oct 10:00]'
        new = 'Meeting requested for 13 Oct 2026 at 15:00 IST; executive confirmation pending.'
        self.write(skeleton(**{'Pending Actions and Clarifications': [old]}))
        with patch.object(updater, '_extract', return_value={
                'resolved': [old], 'pending': [new], 'contact_preference': 'unchanged'}):
            updater._update('123', 'seller', 'chat', [])
        _, body = builder.read_view_file(self.path)
        self.assertNotIn(old, body)
        self.assertIn(new, body)
        with patch('demo_history.user_status', return_value={'pending': False}), \
                patch.object(prompt_context, 'load_view', return_value=body), \
                patch.object(prompt_context, 'load_memory', return_value=[]), \
                patch.object(prompt_context, 'wait_for_update'), \
                patch.object(prompt_context, 'emit'):
            prompt = prompt_context.build_system_prompt('123', 'seller', 'voice')
        self.assertIn(new, prompt)
        self.assertNotIn(old, prompt)

    def test_forced_refresh_preserves_learned_facts_and_suppresses_old_source(self):
        old = "- Needs 500 kg. [API: BLdetails]"
        learned = "- Needs 800 kg. [chat 10 Oct 10:00]"
        self.write(skeleton(**{"Products and Categories": [learned]}),
                   conversation_updates=1, suppressed_bullets=[old])
        state = {"context_api": {"coverage": "partial"}}
        with patch.object(builder, "collect_sources", return_value=({"api": {"x": "data"}}, state)), \
                patch.object(builder, "render_view", return_value=skeleton(**{"Products and Categories": [old]})), \
                patch.object(summarizer, "summarize_in_background"):
            builder.get_view("123", "seller", force=True)
        meta, body = builder.read_view_file(self.path)
        self.assertIn(learned, body)
        self.assertNotIn(old, body)
        self.assertEqual(meta["conversation_updates"], 1)

    def test_updated_context_is_refreshed_by_source_age(self):
        self.write(skeleton(), generated_at="2000-01-01T00:00:00+00:00", conversation_updates=3)
        with patch.object(builder, "generate_view", return_value=self.path) as refresh:
            builder.get_view("123", "seller")
        refresh.assert_called_once_with("123", "seller")

    def test_unavailable_refresh_keeps_existing_context(self):
        body = skeleton(**{"Products and Categories": ["- Learned fact. [chat 10 Oct 10:00]"]})
        self.write(body)
        with patch.object(builder, "collect_sources", return_value=({}, {})):
            self.assertEqual(builder.generate_view("123", "seller"), self.path)
        self.assertEqual(builder.read_view_file(self.path)[1], body)

    def test_cold_start_without_sources_does_not_create_file(self):
        with patch.object(builder, "collect_sources", return_value=({}, {})):
            self.assertIsNone(builder.get_view("900000000001", "buyer"))
        self.assertFalse((self.contexts / "900000000001/buyer.md").exists())

    def test_chat_followup_is_in_next_voice_prompt(self):
        followup = "- Callback requested for 12 Oct 2026. [from chat 10 Oct 10:00]"
        summary = "- 10 Oct 2026 10:00, chat: Seller requested a callback on 12 Oct."
        body = guide.add_guide("123", "seller", skeleton(**{
            "Cross-Channel Conversations": [summary], "Pending Actions and Clarifications": [followup]}))
        with patch.object(prompt_context, "wait_for_update"), patch.object(prompt_context, "load_view", return_value=body), \
                patch.object(prompt_context, "load_memory", return_value=[]), patch.object(prompt_context, "emit") as event:
            prompt = prompt_context.build_system_prompt("123", "seller", "voice")
        self.assertIn(summary[2:], prompt)
        self.assertIn(followup[2:], prompt)
        self.assertEqual(event.call_args.kwargs["prompt_chars"], len(prompt))

    def test_changed_record_cannot_use_old_summary(self):
        cache_path = self.contexts / "123/summaries.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(json.dumps({"enquiry:1": {"hash": summarizer._hash("old text"),
                                                       "summary": "Old quantity"}}))
        with patch.object(summarizer, "free_text_records", return_value={"enquiry:1": "new text"}):
            self.assertEqual(summarizer.load_summaries("123", {}, "seller"), {})

    def test_old_background_pass_cannot_overwrite_new_refresh(self):
        self.write(skeleton(), source_revision=2)
        before = self.path.read_text()
        with patch.object(summarizer, "free_text_records", return_value={"enquiry:1": "record text"}), \
                patch.object(summarizer, "_llm_summaries", return_value={"enquiry:1": "summary"}):
            summarizer._summarize("123", "seller", {}, revision=1)
        self.assertEqual(self.path.read_text(), before)

    def test_older_slow_refresh_cannot_overwrite_newer_sources(self):
        self.write(skeleton(), source_revision=200)
        before = self.path.read_text()
        with patch.object(builder.time, "time_ns", return_value=100), \
                patch.object(builder, "collect_sources", return_value=({"api": {"x": "data"}}, {"context_api": {"coverage": "partial"}})), \
                patch.object(summarizer, "summarize_in_background") as background:
            self.assertEqual(builder.generate_view("123", "seller"), self.path)
        self.assertEqual(self.path.read_text(), before)
        background.assert_not_called()

    def test_template_guide_never_calls_llm_and_uses_name(self):
        body = skeleton(**{"Business Profile and KYC": ["- Customer name: Raghav; City: Indore [API: kycdetails]"]})
        with patch.object(guide, "SarvamAI", side_effect=AssertionError("Unexpected LLM call")):
            result = guide.add_guide("123", "seller", body)
        self.assertIn("Namaste Raghav ji", result)

    def test_dataset_index_invalidates_when_file_changes(self):
        path = self.root / "data.csv"
        path.write_text("glid,value\n123,old\n")
        stat = path.stat()
        self.assertEqual(builder._dataset_index(path, "glid", stat.st_mtime_ns, stat.st_size)["123"][0]["value"], "old")
        path.write_text("glid,value\n123,new value\n")
        stat = path.stat()
        self.assertEqual(builder._dataset_index(path, "glid", stat.st_mtime_ns, stat.st_size)["123"][0]["value"], "new value")

    def test_ticket_resolution_reconciles_incomplete_extraction_and_refresh(self):
        ticket = '- Open support ticket: 10783278; status WIP [API: ticketsdetails; ticket 10783278]'
        complaint = '- User reported no resolution received for the ticket [voice 10 Oct 13:54]'
        followup = '- Executive to follow up on ticket 10783278 [from voice 10 Oct 14:32]'
        self.write(skeleton(**{'Support and Service State': [complaint, ticket],
                               'Pending Actions and Clarifications': [followup]}))
        with patch.object(updater, '_extract', return_value={'updates': [{
                'section': 'Support and Service State',
                'text': 'User confirmed ticket 10783278 issue is resolved.'}]}):
            updater._update('123', 'seller', 'voice', [])
        meta, body = builder.read_view_file(self.path)
        self.assertNotIn(ticket, body)
        self.assertNotIn(complaint, body)
        self.assertNotIn(followup, body)
        self.assertNotIn(followup, meta['learned_state']['Pending Actions and Clarifications'])
        self.assertNotIn('aapke kuch tickets aaye hue hain', body)
        with patch.object(builder, 'collect_sources', return_value=({'api': {'ticketsdetails': {
                'wip_tickets': {'ticket_id': '10783278', 'ticket_status': 'W',
                                'ticket_opening_comments': 'Older WIP snapshot'}}}},
                {'context_api': {'coverage': 'partial'}})), \
                patch.object(summarizer, 'summarize_in_background'):
            builder.generate_view('123', 'seller')
        _, refreshed = builder.read_view_file(self.path)
        self.assertIn('User confirmed ticket 10783278 issue is resolved.', refreshed)
        self.assertNotIn('Open support ticket:', refreshed)
        self.assertNotIn('aapke kuch tickets aaye hue hain', refreshed)

    def test_cached_buyer_guide_migrates_once_without_fetching_sources(self):
        path = self.contexts / '123/buyer.md'
        old = '# Buyer Context\n\n## Conversation Guide\n- Opening: business updates\n\n## Requirements and Enquiries\n- Product: Granules; quantity 800 kg.'
        builder.write_view_file(path, {'glid': '123', 'role': 'buyer', 'context_version': 1,
                                      'compact_version': compact.COMPACT_VERSION}, old)
        meta, body = builder.read_view_file(path)
        meta.pop('guide_version', None)  # Simulate a pre-profile cached file.
        path.write_text('---\n' + '\n'.join(f'{k}: {json.dumps(v)}' for k, v in meta.items()) + '\n---\n\n' + body)
        with patch.object(builder, 'collect_sources', side_effect=AssertionError('No API fetch expected')):
            updated = builder.get_view('123', 'buyer')
            self.assertIn('Granules ki requirement', updated)
            self.assertNotIn('business updates', updated)
            self.assertEqual(builder.get_view('123', 'buyer'), updated)
        self.assertEqual(builder.read_view_file(path)[0]['context_version'], 2)
        self.assertTrue((path.parent / 'history/buyer.v1.md').exists())

    def test_seller_api_excludes_personal_buyer_activity_and_identifiers(self):
        body = renderer.render_view("123", "seller", {"api": {
            "kycdetails": {"customer_name": "Raghav", "address": "private", "gst_new": "identifier"},
            "activitydetails": {"buyer_activity": [{"product_name": "Personal shopping"}], "enq_last_30_days": 0}}}, {}, {"items": 3})
        self.assertIn("Customer name: Raghav", body)
        self.assertIn("Enq last 30 days: 0", body)
        self.assertNotIn("Personal shopping", body)
        self.assertNotIn("private", body)
        self.assertNotIn("identifier", body)


if __name__ == "__main__":
    unittest.main()
