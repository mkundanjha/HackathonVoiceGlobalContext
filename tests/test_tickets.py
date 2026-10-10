"""Offline checks for ticket details, preservation, and conversation priority."""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import compact
import guide
import renderer


class TicketTests(unittest.TestCase):
    def render(self, ticket, **extra):
        return renderer.render_view('123', 'seller', {'api': {
            'ticketsdetails': {'wip_tickets': ticket, **extra}}}, {}, {'items': 3})

    def ticket(self):
        return {'ticket_id': '10783278', 'ticket_status': 'W',
                'ticket_issue_date': '08-OCT-26', 'ticket_type': 'Service Related',
                'ticket_opening_comments': 'Customer needs help. Please connect with customer on priority.'}

    def test_details_and_deduplication(self):
        ticket = self.ticket()
        body = self.render(ticket, last_7_days={'tickets': [ticket]})
        self.assertEqual(body.count('Open support ticket:'), 1)
        for text in ('10783278', 'status WIP', 'Service Related', '08 Oct 2026', 'on priority'):
            self.assertIn(text, body)

    def test_internal_identifiers_removed(self):
        ticket = self.ticket()
        ticket['ticket_opening_comments'] = (
            'Customer call to employee (Employee - 104740) from Client Mob Number=99*****006, '
            'click_to_call_id=771277492. Please connect with customer on priority.')
        body = self.render(ticket)
        for text in ('104740', '99*****006', '771277492'):
            self.assertNotIn(text, body)
        self.assertIn('on priority', body)

    def test_open_ticket_survives_small_word_budget(self):
        body = compact.compact_body(self.render(self.ticket()), 'seller',
                                    {'max_words': 30, 'source_bullets': 0})
        self.assertIn('Open support ticket: 10783278', body)

    def test_current_closed_record_overrides_historical_wip(self):
        closed = {**self.ticket(), 'ticket_status': 'Resolved'}
        body = self.render(self.ticket(), current={'tickets': [closed]})
        self.assertNotIn('Open support ticket:', body)
        self.assertIn('status Resolved', body)

    def test_unknown_status_is_not_assumed_open(self):
        body = self.render({**self.ticket(), 'ticket_status': 'Z'})
        self.assertNotIn('Open support ticket:', body)

    def test_support_priority_over_callback_and_demo_lead(self):
        body = self.render(self.ticket()).replace(
            '## Pending Actions and Clarifications',
            '## Pending Actions and Clarifications\n- Callback requested by bot, time unconfirmed.')
        body = body.replace('## Leads and Enquiries',
                            '## Leads and Enquiries\n- New available lead: Bags [Demo history: abc]')
        with patch.object(guide, 'emit'):
            result = guide.add_guide('123', 'seller', body, template_only=True)
        section = result.split('## Conversation Guide')[1].split('\n## ')[0]
        self.assertIn('aapke kuch tickets aaye hue hain', section)
        self.assertIn('Priority: address', section)
        self.assertNotIn('nayi lead opportunity', section)
        self.assertNotIn('Talking point 1: Callback', section)

    def test_unresolved_conversation_priority_without_api_ticket(self):
        body = self.render({}).replace('## Support and Service State',
            '## Support and Service State\n- User reported no resolution received [voice 10 Oct 13:55]')
        with patch.object(guide, 'emit'):
            result = guide.add_guide('123', 'seller', body, template_only=True)
        self.assertIn('aapki pending support concern', result)
        self.assertIn('Priority: address', result)
        self.assertNotIn('aapke kuch tickets aaye hue hain', result)

    def test_zero_counts_and_ticket_mentions_do_not_create_ticket_opening(self):
        body = self.render({}).replace('## Support and Service State',
            '## Support and Service State\n- Total ticket count: 0 [API: ticketsdetails]')
        body = body.replace('## Cross-Channel Conversations',
            '## Cross-Channel Conversations\n- User asked which tickets the bot meant.')
        with patch.object(guide, 'emit'):
            result = guide.add_guide('624518', 'seller', body, template_only=True)
        opening = next(line for line in result.splitlines() if line.startswith('- Opening:'))
        self.assertNotIn('ticket', opening.lower())
        self.assertNotIn('Priority: address the unresolved', result)

    def test_no_call_preference_takes_precedence(self):
        body = self.render(self.ticket()) + '\n- User requested no further calls.\n'
        with patch.object(guide, 'emit'):
            result = guide.add_guide('123', 'seller', body, template_only=True)
        self.assertIn('Next action: no outbound calls', result)


if __name__ == '__main__':
    unittest.main()
