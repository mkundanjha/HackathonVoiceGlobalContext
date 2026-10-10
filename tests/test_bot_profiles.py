"""Role-specific openings, guide fallbacks, and contact preferences."""

import copy
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import guide
from config import bot_profile, load_config


class BotProfileTests(unittest.TestCase):
    def render(self, role, body):
        with patch.object(guide, 'emit'):
            return guide.add_guide('123', role, body, template_only=True)

    def test_cold_start_openings_are_role_specific(self):
        buyer = self.render('buyer', '# Buyer Context\n\n## Requirements and Enquiries\n- Unknown.')
        seller = self.render('seller', '# Seller Context\n\n## Leads and Enquiries\n- Unknown.')
        self.assertIn('Aapki buying se judi madad', buyer)
        self.assertNotIn('business follow-ups', buyer)
        self.assertIn('enquiries aur business follow-ups', seller)
        self.assertNotIn('product requirement', seller)

    def test_buyer_opening_uses_known_product_and_preserves_quantity(self):
        body = ('# Buyer Context\n\n## Profile and KYC\n- Customer name: Raghav; City: Indore\n\n'
                '## Requirements and Enquiries\n- Product: Natural LD Granules; Quantity: 800 kg.')
        result = self.render('buyer', body)
        self.assertIn('Namaste Raghav ji', result)
        self.assertIn('Natural LD Granules ki requirement', result)
        self.assertIn('Quantity: 800 kg', result)

    def test_buyer_does_not_get_seller_ticket_pitch(self):
        body = ('# Buyer Context\n\n## Pending Actions and Clarifications\n'
                '- Open support ticket: 12345; status WIP\n'
                '- Buyer reported ticket unresolved [voice 10 Oct 10:00]')
        result = self.render('buyer', body)
        self.assertNotIn('aapke kuch tickets aaye hue hain', result)
        self.assertNotIn('before leads, catalog or executive meetings', result)

    def test_llm_failure_uses_selected_role_opening(self):
        cfg = copy.deepcopy(load_config())
        cfg['context_files']['guide_mode'] = 'llm'
        for role in ('buyer', 'seller'):
            with self.subTest(role=role), patch.object(guide, 'load_config', return_value=cfg), \
                    patch.object(guide, 'emit'), patch.dict('os.environ', {'SARVAM_API_KEY': 'test'}), \
                    patch.object(guide, 'SarvamAI', side_effect=RuntimeError('offline')):
                result = guide.add_guide('123', role, '# Context')
                expected = bot_profile(cfg, role)['opening_new'].format(**cfg['persona'])
                self.assertIn(expected, result)

    def test_no_calls_override_both_profiles(self):
        for role in ('buyer', 'seller'):
            with self.subTest(role=role):
                result = self.render(role, '# Context\n\n## Pending Actions and Clarifications\n'
                                     '- Contact preference: no further calls. [voice 10 Oct 10:00]')
                self.assertIn('Next action: no outbound calls', result)

    def test_invalid_role_does_not_select_seller(self):
        with self.assertRaises(ValueError):
            bot_profile(load_config(), 'other')

    def test_buyer_continuation_does_not_invent_previous_purchase(self):
        body = ('# Buyer Context\n\n## Cross-Channel Conversations\n'
                '- 10 Oct 2026 14:53, voice: User asked about their account support issue.\n\n'
                '## Requirements and Enquiries\n'
                '- User did not confirm any active packaging requirement. [voice 10 Oct 14:53]')
        result = self.render('buyer', body)
        opening = next(line for line in result.splitlines() if line.startswith('- Opening:'))
        self.assertIn('Humari pichli baat ko aage badhane', opening)
        self.assertNotIn('purchase requirement', opening)
        self.assertNotIn('packaging', opening)
        self.assertNotIn('- Talking point', result)

    def test_buyer_pending_concern_precedes_unrelated_historical_product(self):
        body = ('# Buyer Context\n\n## Requirements and Enquiries\n'
                '- Product: Granules; historical quantity 800 kg.\n\n'
                '## Pending Actions and Clarifications\n'
                '- Buyer reported supplier did not deliver metal rods. [voice 10 Oct 14:53]')
        result = self.render('buyer', body)
        opening = next(line for line in result.splitlines() if line.startswith('- Opening:'))
        self.assertNotIn('Granules', opening)
        self.assertIn('- Talking point 1: Buyer reported supplier did not deliver metal rods.', result)
        self.assertIn('historical quantity 800 kg', result)

    def test_buyer_guide_names_and_requirements_in_older_markdown_format(self):
        body = ('# Buyer Context\n\n## Profile and KYC\n- **Name:** Sagnik [API: kycdetails]\n\n'
                '## Requirements and Enquiries\n- New requirement: 10,000 metal rods. [voice 10 Oct 14:53]')
        result = self.render('buyer', body)
        self.assertIn('Namaste Sagnik ji', result)
        self.assertIn('10,000 metal rods ki requirement', result)
        self.assertNotIn('suppliers will contact', result)

    def test_optional_llm_receives_buyer_rules_and_relevant_continuity_header(self):
        cfg = copy.deepcopy(load_config())
        cfg['context_files']['guide_mode'] = 'llm'
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='## Conversation Guide\n- Opening: "Namaste ji, kya abhi baat kar sakte hain?"'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=complete))
        with patch.object(guide, 'load_config', return_value=cfg), patch.object(guide, 'emit'), \
                patch.dict('os.environ', {'SARVAM_API_KEY': 'test'}), \
                patch.object(guide, 'SarvamAI', return_value=client):
            guide.add_guide('123', 'buyer', '# Buyer Context\n\n## Cross-Channel Conversations\n'
                            '- 10 Oct 2026 14:53, voice: Buyer reported a supplier problem.')
        self.assertEqual(len(calls), 1)
        messages = calls[0]['messages']
        self.assertIn(cfg['bot_profiles']['buyer']['instructions'], messages[0]['content'])
        self.assertIn('without assuming a purchase', messages[1]['content'])


if __name__ == '__main__':
    unittest.main()
