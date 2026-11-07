"""Manual contribution must never attach clinical content or send a message."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit, parse_qs, unquote

from patient_importer import support


class SupportTests(unittest.TestCase):
    def test_mail_draft_contains_only_static_text(self):
        link = support.support_mailto('templates@example.org')
        parts = urlsplit(link)
        self.assertEqual(parts.scheme, 'mailto')
        self.assertEqual(parts.path, 'templates@example.org')
        self.assertEqual(parts.fragment, '')
        fields = parse_qs(parts.query)
        self.assertEqual(set(fields), {'subject', 'body'})
        self.assertIn('вымышленными', fields['body'][0])
        self.assertNotIn('attachment', link.lower())
        self.assertEqual(list(inspect.signature(support.support_mailto).parameters), ['address'])

    def test_address_cannot_inject_headers_or_attachments(self):
        for address in ['', 'a@example.org?attach=C:/test.pdf', 'a@example.org\r\nBcc:b@example.org',
                        'a@example.org#CANARY', 'a@example.org/anything', None]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                support.support_mailto(address)
        parts = urlsplit(support.support_mailto('test#draft@example.org'))
        self.assertEqual(parts.fragment, '')
        self.assertEqual(unquote(parts.path), 'test#draft@example.org')

    def test_support_hint_uses_only_serious_fixed_codes(self):
        self.assertTrue(support.has_serious_issue(['unknown_type']))
        self.assertTrue(support.has_serious_issue(['unmapped_columns']))
        self.assertFalse(support.has_serious_issue(['OCR_USED', 'missing_date', 'ocr_value']))
        self.assertFalse(support.has_serious_issue(['PRIVATE-CANARY', {'code': 'unknown_type'}]))
        self.assertIn('не обезличивает', support.SUPPORT_CAUTION)

    def test_startup_consent_waits_for_native_dialog_and_update_offer(self):
        from patient_importer.gui import Application
        app = SimpleNamespace(closing=False, working=False, updating=False, update_prompt_active=True,
                              diagnostics=Mock(), root=Mock(), offer_diagnostics=Mock(), show_diagnostics=Mock())
        app.diagnostics.should_prompt.return_value = True
        app.diagnostics.mark_prompted.return_value = True
        app.root.tk.call.return_value = ''
        Application.offer_diagnostics(app)
        app.diagnostics.mark_prompted.assert_not_called()
        app.update_prompt_active = False
        app.root.tk.call.return_value = '.__native_dialog_without_python_widget'
        Application.offer_diagnostics(app)
        app.diagnostics.mark_prompted.assert_not_called()
        app.root.tk.call.return_value = ''
        Application.offer_diagnostics(app)
        app.diagnostics.mark_prompted.assert_called_once()
        app.show_diagnostics.assert_called_once_with(first_run=True)


if __name__ == '__main__':
    unittest.main()
