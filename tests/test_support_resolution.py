"""Ticket resolution must survive stale API refreshes and guide generation."""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import compact
import guide
from support_state import reconcile_support

OPEN = '- Open support ticket: 10783278; status WIP [API: ticketsdetails; ticket 10783278]'
OLD = '- User reported no resolution received for the ticket [voice 10 Oct 13:54]'
DONE = '- User confirmed ticket 10783278 issue is resolved. [voice 10 Oct 14:34]'
FOLLOWUP = '- Executive to follow up on ticket 10783278 [from voice 10 Oct 14:32]'


def body(*support, pending=()):
    return ('# Seller Context\n\n## Support and Service State\n' + '\n'.join(support)
            + '\n\n## Pending Actions and Clarifications\n' + '\n'.join(pending))


class ResolutionTests(unittest.TestCase):
    def test_resolution_removes_stale_status_and_followup(self):
        result = reconcile_support(body(OLD, DONE, OPEN, pending=[FOLLOWUP]))
        self.assertIn(DONE, result)
        for old in (OLD, OPEN, FOLLOWUP):
            self.assertNotIn(old, result)
        with patch.object(guide, 'emit'):
            guided = guide.add_guide('123', 'seller', result, template_only=True)
        self.assertNotIn('aapke kuch tickets aaye hue hain', guided)
        self.assertNotIn('Priority: address the unresolved support', guided)

    def test_changed_api_wording_does_not_revive_ticket(self):
        refreshed = body(OPEN.replace('status WIP', 'status WIP; updated note'))
        result = compact.merge_learned(refreshed, {'Support and Service State': [OLD, DONE]}, [])
        self.assertIn(DONE, result)
        self.assertNotIn('Open support ticket:', result)
        self.assertNotIn(OLD, result)

    def test_another_open_ticket_is_preserved(self):
        second = OPEN.replace('10783278', '99999')
        result = reconcile_support(body(OPEN, second, DONE, OLD))
        self.assertIn(second, result)
        self.assertIn(OLD, result)  # Generic complaint is ambiguous with multiple tickets.
        self.assertNotIn(OPEN, result)

    def test_close_request_or_negated_resolution_does_not_settle_ticket(self):
        for text in ('User requested ticket 10783278 be closed.',
                     'User confirmed ticket 10783278 is not resolved.',
                     'User confirmed ticket 10783278 issue is not fixed.',
                     'User reported ticket 10783278 remains unresolved.'):
            with self.subTest(text=text):
                result = reconcile_support(body(OPEN, f'- {text} [voice 10 Oct 14:34]'))
                self.assertIn(OPEN, result)

    def test_explicit_reopening_restores_priority(self):
        reopened = '- User reported ticket 10783278 is unresolved again. [voice 10 Oct 14:40]'
        result = reconcile_support(body(DONE, reopened, OPEN))
        self.assertIn(OPEN, result)
        with patch.object(guide, 'emit'):
            guided = guide.add_guide('123', 'seller', result, template_only=True)
        self.assertIn('aapke kuch tickets aaye hue hain', guided)

    def test_unconfirmed_or_unspecific_resolution_does_not_guess_ticket(self):
        for text in ('User confirmed their issue is resolved.',
                     'Ticket 10783278 is resolved. [API: ticketsdetails]'):
            result = reconcile_support(body(OPEN, f'- {text} [voice 10 Oct 14:34]'))
            self.assertIn(OPEN, result)


if __name__ == '__main__':
    unittest.main()
