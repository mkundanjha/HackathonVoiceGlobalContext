"""History API simulation checks with isolated files and no upstream requests."""

import json
from unittest.mock import patch
import unittest

import test_context_pipeline as fixtures
from test_context_pipeline import skeleton, builder, updater, summarizer, guide, prompt_context
import demo_history as demo


class DemoHistoryTests(unittest.TestCase):
    write = fixtures.PipelineTests.write

    def setUp(self):
        fixtures.PipelineTests.setUp(self)
        p = patch.object(demo, 'DB_PATH', self.root / 'memory/demo_history.sqlite3')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(demo, 'emit')
        self.events = p.start()
        self.addCleanup(p.stop)
        self.payload = {'event_id': 'test-enquiry', 'glid': '123', 'role': 'seller',
                        'activity_type': 'enquiry.created', 'product': 'Courier bags',
                        'quantity': '500', 'unit': 'pieces', 'city': 'Kolkata'}

    def send(self, **changes):
        payload = {**self.payload, **changes}
        demo.submit(payload)
        self.assertTrue(demo.process_one())
        return demo.get(payload['event_id'])

    def test_enquiry_updates_real_file_history_and_opening(self):
        original = skeleton(**{'Pending Actions and Clarifications': ['- Send catalog. [from chat 10 Oct 10:00]']})
        self.write(original, glid='123', role='seller', generated_at='2026-10-10T10:00:00+05:30')
        with patch.object(guide, 'SarvamAI', side_effect=AssertionError('No LLM required')), \
                patch.object(builder, 'collect_sources', side_effect=AssertionError('No upstream fetch')):
            row = self.send()
        meta, body = builder.read_view_file(self.path)
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['version'], 2)
        self.assertIn('500 pieces', body)
        self.assertIn('Courier bags', body.split('## Business Profile')[0])
        self.assertIn('Send catalog.', body)
        self.assertEqual(meta['generated_at'], '2026-10-10T10:00:00+05:30')
        self.assertEqual(builder.read_view_file(self.path.parent / 'history/seller.v1.md')[1], original)
        self.assertGreaterEqual(row['save_ms'], row['processing_ms'])
        self.assertFalse(demo.user_status('123', 'seller')['pending'])

    def test_lead_is_available_opportunity_not_purchase(self):
        row = self.send(activity_type='lead.available')
        _, body = builder.read_view_file(self.path)
        self.assertEqual(row['status'], 'done')
        self.assertIn('New available lead: Courier bags', body)
        self.assertNotIn('Bought', body)

    def test_buyer_requirement_routes_to_buyer_section(self):
        self.send(role='buyer')
        _, body = builder.read_view_file(self.contexts / '123/buyer.md')
        self.assertIn('Requirement submitted: Courier bags', body.split('## Requirements and Enquiries')[1].split('## Searches')[0])

    def test_ticket_open_close_refresh_and_reopen_are_specific_to_id(self):
        self.send(event_id='ticket-a-open', activity_type='ticket.opened',
                  ticket_id='111', issue='App login fails')
        self.send(event_id='ticket-b-open', activity_type='ticket.opened',
                  ticket_id='222', issue='Catalog update pending')
        _, body = builder.read_view_file(self.path)
        self.assertIn('Open support ticket: 111', body)
        self.assertIn('aapke kuch tickets aaye hue hain', body)
        row = self.send(event_id='ticket-a-close', activity_type='ticket.closed', ticket_id='111')
        _, body = builder.read_view_file(self.path)
        self.assertEqual(row['status'], 'done')
        self.assertNotIn('Open support ticket: 111', body)
        self.assertIn('Open support ticket: 222', body)
        self.assertIn('Support ticket: 111; status Closed', body)
        # Even expired ticket events override a stale API WIP snapshot after refresh.
        with demo.connect() as db:
            db.execute('UPDATE activity SET saved_at=saved_at-? WHERE event_id LIKE ?', (40*86400, 'ticket-%'))
        with patch.object(builder, 'collect_sources', return_value=({'api': {
                'ticketsdetails': {'wip_tickets': {'ticket_id': '111', 'ticket_status': 'W'}}}},
                {'context_api': {'coverage': 'partial'}})), \
                patch.object(summarizer, 'summarize_in_background'):
            builder.generate_view('123', 'seller')
        _, body = builder.read_view_file(self.path)
        self.assertNotIn('Open support ticket: 111', body)
        self.assertIn('Open support ticket: 222', body)
        self.send(event_id='ticket-b-close', activity_type='ticket.closed', ticket_id='222')
        _, body = builder.read_view_file(self.path)
        self.assertNotIn('aapke kuch tickets aaye hue hain', body)
        self.send(event_id='ticket-a-reopen', activity_type='ticket.opened',
                  ticket_id='111', issue='Login fails again')
        _, body = builder.read_view_file(self.path)
        self.assertIn('Open support ticket: 111', body)
        self.assertIn('Login fails again', body)
        self.assertNotIn('Support ticket: 111; status Closed', body)

    def test_ticket_api_accepts_only_required_fields_and_rejects_invalid_input(self):
        import os
        from fastapi import HTTPException
        from pydantic import ValidationError
        with patch.dict(os.environ, {'SARVAM_API_KEY': 'offline-test-key'}):
            import console
        payload = {'event_id': 'ticket-open', 'glid': '123', 'role': 'seller',
                   'activity_type': 'ticket.opened', 'ticket_id': '111', 'issue': 'Login fails'}
        row = console.demo_history_save(console.DemoHistoryRequest(**payload))
        self.assertNotIn('product', json.loads(row['payload']))
        self.assertTrue(demo.process_one())
        close = {**payload, 'event_id': 'ticket-close', 'activity_type': 'ticket.closed'}
        close.pop('issue')
        console.demo_history_save(console.DemoHistoryRequest(**close))
        self.assertTrue(demo.process_one())
        for changes in ({'ticket_id': None}, {'issue': ' '}, {'issue': '[fake]'}, {'role': 'buyer'}):
            with self.assertRaises(HTTPException) as error:
                console.demo_history_save(console.DemoHistoryRequest(**{**payload, **changes}))
            self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(ValidationError):
            console.DemoHistoryRequest(**{**payload, 'ticket_id': 'abc'})

    def test_duplicate_retries_do_not_create_extra_versions(self):
        row = self.send()
        retry = demo.submit(self.payload)
        self.assertEqual(retry['version'], row['version'])
        self.assertFalse(demo.process_one())
        with self.assertRaises(ValueError):
            demo.submit({**self.payload, 'quantity': '800'})

    def test_restart_recovery_does_not_reapply_published_event(self):
        row = self.send()
        before = self.path.read_text()
        with demo.connect() as db:
            db.execute("UPDATE activity SET status='queued' WHERE event_id=?", (row['event_id'],))
        self.assertTrue(demo.process_one())
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(demo.get(row['event_id'])['version'], row['version'])

    def test_failure_is_visible_and_retry_recovers(self):
        demo.submit(self.payload)
        with patch.object(builder, 'write_view_file', side_effect=OSError('disk unavailable')):
            demo.process_one()
        self.assertEqual(demo.get('test-enquiry')['status'], 'failed')
        self.assertEqual(demo.user_status('123', 'seller')['metrics']['count'], 0)
        demo.retry('test-enquiry')
        self.assertTrue(demo.process_one())
        self.assertEqual(demo.get('test-enquiry')['status'], 'done')

    def test_source_refresh_keeps_demo_overlay(self):
        self.send()
        with patch.object(builder, 'collect_sources', return_value=({'api': {'kycdetails': {'customer_name': 'Raghav'}}}, {'context_api': {'coverage': 'partial'}})), \
                patch.object(summarizer, 'summarize_in_background'):
            builder.generate_view('123', 'seller')
        _, body = builder.read_view_file(self.path)
        self.assertIn('New enquiry: Courier bags', body)
        self.assertIn('Namaste Raghav ji', body)

    def test_conversation_update_keeps_demo_overlay(self):
        self.send()
        with patch.object(updater, '_extract', return_value={'summary': 'Seller confirmed interest.'}):
            updater._update('123', 'seller', 'chat', [])
        _, body = builder.read_view_file(self.path)
        self.assertIn('New enquiry: Courier bags', body)
        self.assertIn('Seller confirmed interest.', body)

    def test_pending_activity_prevents_new_prompt(self):
        demo.submit(self.payload)
        with self.assertRaisesRegex(RuntimeError, 'updating context'):
            prompt_context.build_system_prompt('123', 'seller', 'voice')

    def test_latest_three_activities_survive_compaction(self):
        for i in range(5):
            self.send(event_id=f'event-{i}', product=f'Product {i}')
        _, body = builder.read_view_file(self.path)
        self.assertEqual(guide.strip_guide(body).count('[Demo history:'), 3)
        self.assertIn('New enquiry: Product 4', body)
        self.assertNotIn('New enquiry: Product 0', body)

    def test_api_validation_status_and_retry_contract(self):
        import os
        from fastapi import HTTPException
        from pydantic import ValidationError
        with patch.dict(os.environ, {'SARVAM_API_KEY': 'offline-test-key'}):
            import console
        request = console.DemoHistoryRequest(**{**self.payload, 'quantity': 1234567})
        record = console.demo_history_save(request)
        self.assertEqual(json.loads(record['payload'])['quantity'], '1234567')
        self.assertEqual(console.demo_history_event('test-enquiry')['event_id'], 'test-enquiry')
        self.assertTrue(console.demo_history_status('123', 'seller')['pending'])
        self.assertEqual(console.demo_history_save(request)['event_id'], 'test-enquiry')
        with self.assertRaises(ValidationError):
            console.DemoHistoryRequest(**{**self.payload, 'quantity': 0})
        for changes, status in (({'product': '   '}, 400), ({'product': '[Injected]'}, 400),
                                ({'role': 'buyer', 'activity_type': 'lead.available'}, 400),
                                ({'product': 'Changed'}, 409)):
            with self.assertRaises(HTTPException) as error:
                console.demo_history_save(console.DemoHistoryRequest(**{**self.payload, **changes}))
            self.assertEqual(error.exception.status_code, status)
        with self.assertRaises(HTTPException) as error:
            console.demo_history_event('missing')
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as error:
            console.voice_start(console.UserRequest(glid='123', role='seller'))
        self.assertEqual(error.exception.status_code, 409)

    def test_available_lead_requires_only_product_and_city(self):
        import os
        from fastapi import HTTPException
        with patch.dict(os.environ, {'SARVAM_API_KEY': 'offline-test-key'}):
            import console
        payload = {k: v for k, v in self.payload.items() if k not in ('quantity', 'unit')}
        payload['activity_type'] = 'lead.available'
        record = console.demo_history_save(console.DemoHistoryRequest(**payload))
        self.assertNotIn('quantity', json.loads(record['payload']))
        demo.process_one()
        body = builder.read_view_file(self.path)[1]
        self.assertIn('New available lead: Courier bags; Kolkata', body)
        self.assertNotIn('None', body)
        with self.assertRaises(HTTPException) as error:
            console.demo_history_save(console.DemoHistoryRequest(**{**payload, 'activity_type': 'enquiry.created'}))
        self.assertEqual(error.exception.status_code, 400)

    def test_background_summary_preserves_demo_activity(self):
        self.send()
        meta, body = builder.read_view_file(self.path)
        meta['source_revision'] = 42
        builder.write_view_file(self.path, meta, body)
        with patch.object(summarizer, 'free_text_records', return_value={'enquiry:old': 'record text'}), \
                patch.object(summarizer, '_llm_summaries', return_value={'enquiry:old': 'summary'}):
            summarizer._summarize('123', 'seller', {}, revision=42)
        self.assertIn('New enquiry: Courier bags', builder.read_view_file(self.path)[1])

    def test_unrelated_user_keeps_existing_guide_after_demo_database_exists(self):
        self.send()
        body = guide.add_guide('456', 'seller', skeleton())
        path = self.contexts / '456/seller.md'
        builder.write_view_file(path, {'glid': '456', 'role': 'seller', 'context_version': 1}, body)
        self.assertEqual(builder.read_view_file(path)[1], body)

    def test_worker_recovers_unfinished_event_after_restart(self):
        import threading
        demo.submit(self.payload)
        with demo.connect() as db:
            db.execute("UPDATE activity SET status='processing' WHERE event_id='test-enquiry'")
        completed = threading.Event()
        self.events.side_effect = lambda step, *args, **kwargs: completed.set() if step == 'demo_file' else None
        try:
            demo.start_worker()
            self.assertTrue(completed.wait(3), 'Worker did not finish recovered event')
            self.assertEqual(demo.get('test-enquiry')['status'], 'done')
        finally:
            demo.stop_worker()


if __name__ == '__main__':
    unittest.main()
