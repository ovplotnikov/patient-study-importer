"""OCR evidence must survive extraction without silently hiding image content."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from docx import Document
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from patient_importer.extraction import extract
from patient_importer.ocr import Line, Page, same_reading


def reading(text, engine='paddle-eslav', size=(160, 60)):
    return Page(text, (Line(text, (8, 5, 151, 45), .92),), engine, size)


class OcrEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='psi-synthetic-ocr-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = Image.new('RGB', (160, 60), 'white')

    def test_small_image_in_mixed_pdf_preserves_source_page_and_conflict(self):
        scan = self.root/'scan.pdf'
        self.image.save(scan, 'PDF')
        writer = PdfWriter(clone_from=PdfReader(scan))
        page = writer.pages[0]
        resources = page['/Resources']
        resources[NameObject('/Font')] = DictionaryObject({
            NameObject('/F1'): DictionaryObject({
                NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'),
                NameObject('/BaseFont'): NameObject('/Helvetica'),
            })
        })
        stream = DecodedStreamObject()
        stream.set_data(page.get_contents().get_data() +
                        b'\nBT /F1 10 Tf 3 12 Td (Synthetic native text on mixed page) Tj ET')
        page.replace_contents(stream)
        path = self.root/'mixed.pdf'
        writer.write(path)
        first = reading('Креатинин 71,5 мкмоль/л')
        other = reading('Креатинин 715 ммоль/л', 'tesseract-fast')
        with patch('patient_importer.extraction.recognize_image',
                   return_value=Page(first.text, first.lines, first.engine, first.image_size, (other,))) as ocr:
            data = extract(path)
        self.assertEqual(ocr.call_args.args[0].size, self.image.size)
        self.assertIn('Synthetic native text', data['native_pages'][0])
        self.assertEqual(data['pages'][1], first.text)
        self.assertEqual(data['ocr_pages'], [2])
        self.assertEqual(data['page_numbers'], [1, 1])
        self.assertEqual(data['ocr_evidence'][0]['bbox'], first.lines[0].bbox)
        self.assertEqual(data['ocr_evidence'][0]['source_kind'], 'embedded')
        self.assertEqual(data['ocr_evidence'][0]['page'], 1)
        self.assertEqual(data['ocr_evidence'][0]['page_index'], 2)
        self.assertEqual(data['ocr_alternatives'][0]['text'], other.text)
        self.assertIn('OCR_DISAGREEMENT', {issue['code'] for issue in data['extraction_issues']})

    def test_docx_small_picture_and_unsupported_media_are_both_accounted_for(self):
        picture = io.BytesIO()
        self.image.save(picture, 'PNG')
        picture.seek(0)
        document = Document()
        document.add_paragraph('Вымышленный текст документа')
        document.add_picture(picture)
        document.sections[0].header.paragraphs[0].text = 'Вымышленный заголовок'
        path = self.root/'word.docx'
        document.save(path)
        with ZipFile(path, 'a') as archive:
            archive.writestr('word/media/unsupported.emf', b'synthetic unsupported media')
        with patch('patient_importer.extraction.recognize_image',
                   return_value=reading('Глюкоза <0,50 ммоль/л')) as ocr:
            data = extract(path)
        self.assertEqual(ocr.call_args.args[0].size, self.image.size)
        self.assertEqual(data['pages'][1], 'Глюкоза <0,50 ммоль/л')
        self.assertEqual(data['ocr_pages'], [2])
        codes = {item['code'] for item in data['extraction_issues']}
        self.assertIn('EMBEDDED_IMAGE_UNSUPPORTED', codes)
        self.assertIn('DOCX_EXTRA_TEXT', codes)

    def test_pdf_vector_and_blank_pages_are_accounted_for(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=180, height=72)
        stream = DecodedStreamObject()
        stream.set_data(b'15 12 80 15 re f')
        page.replace_contents(stream)
        writer.add_blank_page(width=180, height=72)
        path = self.root/'vector-and-blank.pdf'
        writer.write(path)
        with patch('patient_importer.extraction.recognize_image', return_value=reading('Белок ≤0,07 г/л')) as ocr:
            data = extract(path)
        ocr.assert_called_once()
        self.assertEqual(data['pages'], ['Белок ≤0,07 г/л', ''])
        self.assertEqual(data['ocr_pages'], [1])
        self.assertTrue(any(item['code'] == 'PDF_PAGE_EMPTY' and item['page'] == 2
                            for item in data['extraction_issues']))

    def test_tiff_frame_failure_keeps_other_frames_and_marks_review(self):
        path = self.root/'frames.tiff'
        self.image.save(path, save_all=True, append_images=[self.image])
        with patch('patient_importer.extraction.recognize_image',
                   side_effect=[reading('Белок ≤0,07 г/л'), RuntimeError('unavailable')]):
            data = extract(path)
        self.assertEqual(data['pages'], ['Белок ≤0,07 г/л', ''])
        self.assertEqual(data['page_numbers'], [1, 2])
        self.assertTrue(any(item['code'] == 'OCR_FAILED' and item['page'] == 2
                            for item in data['extraction_issues']))

    def test_readings_do_not_collapse_digits_units_or_comparison_signs(self):
        reference = reading('Глюкоза ≤0,50 ммоль/л')
        for other in ('Глюкоза <0,50 ммоль/л', 'Глюкоза ≤050 ммоль/л',
                      'Глюкоза ≤0,50 мкмоль/л', 'Глюкоза ≤0,50 ммоль/π'):
            self.assertFalse(same_reading(reference, reading(other)))
        self.assertFalse(same_reading(reading('71,5 mg/L'), reading('71,5 Mg/L')))
        self.assertFalse(same_reading(reading('3,2 mM'), reading('3,2 mm')))
        self.assertTrue(same_reading(reading('−12,6 г/л'), reading(' -12,6  г/л')))


if __name__ == '__main__':
    unittest.main()
