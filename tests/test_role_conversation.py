"""User-led role handoffs, isolated persistence and origin resumption."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import role_conversation as routing
import test_context_pipeline as fixtures
from test_context_pipeline import builder, updater, guide


class RoleConversationTests(unittest.TestCase):
    def test_both_origins_switch_return_and_save_only_matching_segments(self):
        for origin in ('buyer', 'seller'):
            other = 'seller' if origin == 'buyer' else 'buyer'
            with self.subTest(origin=origin), \
                    patch.object(routing, 'build_system_prompt', side_effect=lambda g, r, c: f'{r} context'), \
                    patch.object(routing, 'classify_role', side_effect=[origin, other, other, origin]), \
                    patch.object(routing, 'save_conversation') as save, patch.object(routing, 'emit'):
                session = routing.RoleConversation('123', origin, 'chat')
                session.on_bot('Original role question?')
                session.before_user('Original role answer')
                session.on_bot('Unfinished original question?')
                side = session.before_user('Other role need')
                self.assertEqual(session.active, other)
                self.assertIn('Unfinished original question?', side)
                self.assertTrue(side.startswith(other + ' context'))
                session.on_bot('Missing detail?')
                session.before_user('Other role detail')
                session.on_bot('Side need captured. Unfinished original question?')
                resumed = session.before_user('Original question answer')
                self.assertEqual(session.origin, origin)
                self.assertEqual(session.active, origin)
                self.assertTrue(resumed.startswith(origin + ' context'))
                session.flush()
                self.assertEqual([call.args[1] for call in save.call_args_list], [origin, other, origin])
                texts = [[t['text'] for t in call.args[3] if t['speaker'] == 'user']
                         for call in save.call_args_list]
                self.assertEqual(texts, [['Original role answer'], ['Other role need', 'Other role detail'],
                                        ['Original question answer']])

    def test_context_failure_does_not_switch_or_misfile_user_need(self):
        with patch.object(routing, 'build_system_prompt', side_effect=['seller context', RuntimeError('not ready')]), \
                patch.object(routing, 'classify_role', return_value='buyer'), \
                patch.object(routing, 'save_conversation') as save:
            session = routing.RoleConversation('123', 'seller', 'voice')
            with self.assertRaises(RuntimeError):
                session.before_user('I want to purchase valves')
            self.assertEqual(session.active, 'seller')
            self.assertEqual(session.turns, [])
            save.assert_not_called()

    def test_classifier_rejects_invalid_role_and_preserves_unicode_input(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=lambda **kw: response))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"role":"buyer"}'))])
        with patch.object(routing, 'SarvamAI', return_value=client), patch.dict(os.environ, {'SARVAM_API_KEY': 'test'}):
            self.assertEqual(routing.classify_role('seller', 'seller', [], 'मुझे valves खरीदने हैं'), 'buyer')
            response.choices[0].message.content = '{"role":"admin"}'
            with self.assertRaises(ValueError):
                routing.classify_role('seller', 'seller', [], 'test')

    def test_chat_endpoint_uses_active_profile_but_retains_origin(self):
        with patch.dict(os.environ, {'SARVAM_API_KEY': 'test'}):
            import chat_app
        with patch.object(routing, 'build_system_prompt', side_effect=lambda g, r, c: f'{r} context'), \
                patch.object(routing, 'classify_role', side_effect=['buyer', 'seller']), \
                patch.object(routing, 'save_conversation') as save, patch.object(routing, 'emit'), \
                patch.object(chat_app, 'emit'), \
                patch.object(chat_app, 'user_status', return_value={'pending': False}), \
                patch.object(chat_app, '_complete', side_effect=['Seller opening?', 'Buyer requirement noted. Seller question?', 'Seller answer.']):
            started = chat_app.start(chat_app.StartRequest(glid='123', role='seller'))
            sid = started['session_id']
            try:
                reply = chat_app.message(chat_app.MessageRequest(session_id=sid, text='I need valves'))
                self.assertEqual(reply['active_role'], 'buyer')
                self.assertEqual(reply['origin_role'], 'seller')
                self.assertTrue(chat_app.sessions[sid]['messages'][0]['content'].startswith('buyer context'))
                reply = chat_app.message(chat_app.MessageRequest(session_id=sid, text='My seller answer'))
                self.assertEqual(reply['active_role'], 'seller')
                chat_app.end(chat_app.EndRequest(session_id=sid))
                self.assertEqual([call.args[1] for call in save.call_args_list], ['buyer', 'seller'])
            finally:
                chat_app.sessions.pop(sid, None)


class RoleFileTests(unittest.TestCase):
    write = fixtures.PipelineTests.write

    def setUp(self):
        fixtures.PipelineTests.setUp(self)

    def test_side_purchase_creates_buyer_file_without_polluting_seller_file(self):
        extraction = {
            'seller': {'updates': [{'section': 'Support and Service State', 'text': 'App login fails.'}]},
            'buyer': {'updates': [{'section': 'Requirements and Enquiries',
                                   'text': 'Needs 200 valves in Jaipur by 16 Oct 2026.'}]},
        }
        def save(glid, role, channel, turns):
            with patch.object(updater, '_extract', return_value=extraction[role]):
                updater._update(glid, role, channel, turns)
        with patch.object(routing, 'build_system_prompt', side_effect=lambda g, r, c: f'{r} context'), \
                patch.object(routing, 'classify_role', side_effect=['seller', 'buyer', 'seller']), \
                patch.object(routing, 'save_conversation', side_effect=save), patch.object(routing, 'emit'):
            session = routing.RoleConversation('123', 'seller', 'chat')
            session.before_user('App login fails')
            session.on_bot('Since when?')
            session.before_user('Also need 200 valves in Jaipur by 16 October')
            session.on_bot('Requirement noted. Since when does login fail?')
            session.before_user('Login failed since yesterday')
            session.flush()
        buyer = builder.read_view_file(self.contexts / '123/buyer.md')[1]
        seller = builder.read_view_file(self.path)[1]
        self.assertIn('200 valves', buyer)
        self.assertNotIn('App login fails.', buyer)
        self.assertIn('App login fails.', seller)
        self.assertNotIn('200 valves', seller)


if __name__ == '__main__':
    unittest.main()
