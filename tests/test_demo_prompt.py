"""Check shared demo prompt assembly without APIs or context writes."""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import context
from config import load_config


class DemoPromptTests(unittest.TestCase):
    def build(self, role, channel, view, memory=None):
        with patch('demo_history.user_status', return_value={'pending': False}), \
                patch.object(context, 'wait_for_update'), \
                patch.object(context, 'load_view', return_value=view), \
                patch.object(context, 'load_memory', return_value=memory or []), \
                patch.object(context, 'emit'):
            return context.build_system_prompt('123', role, channel)

    def test_roles_and_channels_resolve_placeholders_and_preserve_data(self):
        cfg = load_config()
        view = '# Context\n- Product code: {name}; requested 800 kg; city Indore.'
        for role in ('seller', 'buyer'):
            for channel in ('voice', 'chat'):
                with self.subTest(role=role, channel=channel):
                    prompt = self.build(role, channel, view)
                    self.assertIn(view, prompt)  # Braces inside data must stay literal.
                    self.assertIn(cfg['call_purpose'][role], prompt)
                    self.assertIn(cfg['prompts'][f'{channel}_style'], prompt)
                    self.assertIn(f'helping a {role} on {channel}', prompt)
                    self.assertIn(cfg['bot_profiles'][role]['title'], prompt)
                    self.assertIn(cfg['bot_profiles'][role]['instructions'], prompt)
                    other = 'seller' if role == 'buyer' else 'buyer'
                    self.assertNotIn(cfg['bot_profiles'][other]['title'], prompt)
                    self.assertNotIn(cfg['bot_profiles'][other]['instructions'], prompt)
                    instructions = prompt.split('Context for this ')[0]
                    self.assertIsNone(context.PLACEHOLDER.search(instructions))

    def test_whatsapp_requirement_survives_into_voice_with_recent_correction(self):
        view = '# Buyer Context\n- WhatsApp: needs 500 kg granules in Indore.'
        memory = [{'channel': 'chat', 'ended_at': 1791600000,
                   'turns': [{'speaker': 'user', 'text': 'Actually I need 800 kg.'}]}]
        prompt = self.build('buyer', 'voice', view, memory)
        self.assertIn(view, prompt)
        self.assertIn('Actually I need 800 kg.', prompt)
        self.assertIn('do not claim direct access', prompt)

    def test_cold_start_includes_role_goal_and_retains_available_memory(self):
        memory = [{'channel': 'voice', 'ended_at': 1791600000,
                   'turns': [{'speaker': 'user', 'text': 'Call me on 12 October.'}]}]
        for role in ('seller', 'buyer'):
            with self.subTest(role=role):
                prompt = self.build(role, 'chat', None, memory)
                self.assertIn(load_config()['prompts']['cold_start'], prompt)
                self.assertIn(load_config()['call_purpose'][role], prompt)
                self.assertIn('Call me on 12 October.', prompt)

    def test_seller_ticket_and_contact_restriction_reach_prompt(self):
        view = '# Seller Context\n## Support and Service State\n- Open support ticket: 10783278; status WIP'
        prompt = self.build('seller', 'voice', view)
        self.assertIn('Open support ticket: 10783278', prompt)
        self.assertIn('Actual unresolved ticket details are present', prompt)
        self.assertIn('Respect no-call requests and agreed callback timing', prompt)

    def test_zero_tickets_override_old_bot_claims_on_both_channels(self):
        view = ('# Seller Context\n## Support and Service State\n'
                '- Last 7 days: Total ticket count: 0 [API: ticketsdetails]\n'
                '- Last 8 to 90 days: Total ticket count: 0 [API: ticketsdetails]\n'
                '## Cross-Channel Conversations\n- User asked about open tickets.')
        memory = [{'channel': 'voice', 'ended_at': 1791600000, 'turns': [
            {'speaker': 'bot', 'text': 'Aapke kuch tickets aaye hue hain.'}]}]
        for channel in ('voice', 'chat'):
            prompt = self.build('seller', channel, view, memory)
            self.assertIn('No actual unresolved ticket details are present', prompt)
            self.assertNotIn('Actual unresolved ticket details are present', prompt)

    def test_resolved_ticket_does_not_enable_ticket_opening(self):
        view = ('# Seller Context\n## Support and Service State\n'
                '- Open support ticket: 10783278; status WIP [API: ticketsdetails]\n'
                '- User confirmed ticket 10783278 issue is resolved [voice 10 Oct 14:00]')
        self.assertIn('No actual unresolved ticket details are present',
                      self.build('seller', 'voice', view))


if __name__ == '__main__':
    unittest.main()
