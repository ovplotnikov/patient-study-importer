"""Keep OCR evidence intact when engines overlap and a PDF reuses an image."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageOps
from pypdf import PdfWriter

from patient_importer import analysis, extraction
from patient_importer.ocr import Line, Page
from patient_importer.smoke import make_mixed_pdf


FORM = ('Анализ крови биохимический\nПациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\n'
        'Дата рождения: 01.01.2000\nДата выполнения исследования: 02.03.2025\n'
        'Креатинин ≤71,5 мкмоль/л')


def reading(text, engine='paddle-cyrillic', size=(7, 17)):
    return Page(text, tuple(Line(line, (1, index, 6, index + 1), .92)
                            for index, line in enumerate(text.splitlines())), engine, size)


class OverlappingOcrTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='psi-ocr-synthetic-')))
        (root / 'rapidocr').mkdir()
        (root / 'tesseract').mkdir()
        suffix = 'tesseract.exe' if extraction.os.name == 'nt' else 'tesseract'
        (root / 'tesseract' / suffix).touch()
        self.enterContext(patch.object(extraction, 'resource_path', side_effect=lambda name: root / name))
        self.enterContext(patch.object(extraction, 'rapid_engine', return_value=object()))
        self.primary = reading(FORM)
        self.secondary = reading(FORM.replace('≤71,5 мкмоль/л', '<71,5 ммоль/л'), 'tesseract-fast')
        self.image = Image.new('RGB', (17, 7), (200, 100, 50))
        self.image.putpixel((0, 0), (1, 2, 3))
        self.image.getexif()[274] = 6
        self.path = root / 'synthetic.png'
        self.image.save(self.path, 'PNG', exif=self.image.getexif())

    def test_overlap_preserves_pixels_and_complete_extraction_and_quality(self):
        with patch.object(extraction, 'rapid_page', return_value=self.primary) as primary, \
             patch.object(extraction, 'tesseract_page', return_value=self.secondary) as secondary, \
             patch.object(extraction, 'ThreadPoolExecutor', side_effect=RuntimeError('thread unavailable')):
            baseline = extraction.extract(self.path)
        primary.assert_called_once()
        secondary.assert_called_once()

        tess_started = Event()
        rapid_started = Event()
        frames = {}

        def paddle(frame, engine, *, improve_geometry):
            frames['primary'] = frame
            self.assertTrue(improve_geometry)
            self.assertTrue(tess_started.wait(5), 'Tesseract did not start during RapidOCR')
            rapid_started.set()
            return self.primary

        def tesseract(frame, *args):
            frames['secondary'] = frame
            tess_started.set()
            self.assertTrue(rapid_started.wait(5), 'RapidOCR did not start during Tesseract')
            return self.secondary

        with patch.object(extraction, 'rapid_page', side_effect=paddle), \
             patch.object(extraction, 'tesseract_page', side_effect=tesseract):
            actual = extraction.extract(self.path)
        expected_frame = ImageOps.exif_transpose(self.image).convert('RGB')
        self.assertIsNot(frames['primary'], frames['secondary'])
        for frame in frames.values():
            self.assertEqual((frame.mode, frame.size, frame.tobytes()),
                             (expected_frame.mode, expected_frame.size, expected_frame.tobytes()))
        self.assertEqual(actual, baseline)
        parsed = analysis.analyze(actual, self.path.name)
        self.assertEqual(parsed, analysis.analyze(baseline, self.path.name))
        self.assertFalse(parsed['quality']['confirmed'])
        self.assertIn('OCR_DISAGREEMENT', {item['code'] for item in actual['extraction_issues']})
        self.assertEqual(actual['ocr_evidence'][0]['verification_engine'], 'tesseract-fast')

    def test_engine_failures_preserve_fallback_and_unavailable_flags(self):
        cases = (
            (ValueError('primary failed'), self.secondary, replace(self.secondary, unavailable=('PaddleOCR',))),
            (self.primary, RuntimeError('secondary failed'), replace(self.primary, unavailable=('Tesseract',))),
            (Page('', (), self.primary.engine, self.primary.image_size), self.secondary,
             replace(self.secondary, alternatives=(Page('', (), self.primary.engine, self.primary.image_size),))),
        )
        for primary, secondary, expected in cases:
            with self.subTest(primary=type(primary).__name__, secondary=type(secondary).__name__), \
                 patch.object(extraction, 'rapid_page', side_effect=[primary]), \
                 patch.object(extraction, 'tesseract_page', side_effect=[secondary]):
                self.assertEqual(extraction.recognize_image(self.image), expected)

    def test_over_budget_page_still_runs_both_engines_in_order(self):
        order = []

        def paddle(frame, *args, **kwargs):
            order.append(('primary', frame.size))
            return self.primary

        def tesseract(frame, *args):
            order.append(('secondary', frame.size))
            return self.secondary

        with patch.object(extraction, 'MAX_PARALLEL_OCR_PIXELS', 100), \
             patch.object(extraction, 'ThreadPoolExecutor') as executor, \
             patch.object(extraction, 'rapid_page', side_effect=paddle), \
             patch.object(extraction, 'tesseract_page', side_effect=tesseract):
            actual = extraction.recognize_image(self.image)
        executor.assert_not_called()
        self.assertEqual(order, [('primary', (7, 17)), ('secondary', (7, 17))])
        self.assertEqual(actual, replace(self.primary, alternatives=(self.secondary,),
                                         verification_engine='tesseract-fast'))

    def test_page_slot_is_held_until_secondary_finishes(self):
        first_tess_started = Event()
        second_caller_started = Event()
        second_tess_started = Event()
        release_first = Event()
        first = Image.new('RGB', (17, 7), 'red')
        second = Image.new('RGB', (17, 7), 'blue')

        def tesseract(frame, *args):
            if frame.getpixel((0, 0)) == (255, 0, 0):
                first_tess_started.set()
                self.assertTrue(release_first.wait(5))
            else:
                second_tess_started.set()
            return self.secondary

        def next_page():
            second_caller_started.set()
            return extraction.recognize_image(second)

        with patch.object(extraction, 'rapid_page', return_value=self.primary), \
             patch.object(extraction, 'tesseract_page', side_effect=tesseract), \
             ThreadPoolExecutor(max_workers=2) as callers:
            initial = callers.submit(extraction.recognize_image, first)
            try:
                self.assertTrue(first_tess_started.wait(5))
                following = callers.submit(next_page)
                self.assertTrue(second_caller_started.wait(5))
                self.assertFalse(second_tess_started.wait(.1))
            finally:
                release_first.set()
            self.assertEqual(initial.result(5), following.result(5))
            self.assertTrue(second_tess_started.is_set())

    def test_interrupted_primary_waits_for_worker_and_releases_page_slot(self):
        tess_started = Event()
        primary_failed = Event()
        release_worker = Event()
        tess_finished = Event()

        def paddle(*args, **kwargs):
            self.assertTrue(tess_started.wait(5))
            primary_failed.set()
            raise KeyboardInterrupt()

        def tesseract(*args):
            tess_started.set()
            self.assertTrue(release_worker.wait(5))
            tess_finished.set()
            return self.secondary

        with patch.object(extraction, 'rapid_page', side_effect=paddle), \
             patch.object(extraction, 'tesseract_page', side_effect=tesseract), \
             ThreadPoolExecutor(max_workers=1) as caller:
            run = caller.submit(extraction.recognize_image, self.image)
            try:
                self.assertTrue(primary_failed.wait(5))
                with self.assertRaises(TimeoutError):
                    run.result(.1)
            finally:
                release_worker.set()
            with self.assertRaises(KeyboardInterrupt):
                run.result(5)
        self.assertTrue(tess_finished.is_set())
        with patch.object(extraction, 'rapid_page', return_value=self.primary), \
             patch.object(extraction, 'tesseract_page', return_value=self.secondary):
            self.assertEqual(extraction.recognize_image(self.image).verification_engine, 'tesseract-fast')


class PdfOcrCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='psi-pdf-synthetic-')))
        self.complete = replace(reading(FORM), verification_engine='tesseract-fast',
                                alternatives=(reading(FORM.replace('≤', '<'), 'tesseract-fast'),))
        self.mixed = []
        for color in ('white', 'gray'):
            scan = self.root / f'{color}-scan.pdf'
            mixed = self.root / f'{color}-mixed.pdf'
            Image.new('RGB', (17, 7), color).save(scan, 'PDF')
            make_mixed_pdf(scan, mixed)
            self.mixed.append(mixed)

    def repeated_pdf(self, sources=(0, 0, 0)):
        path = self.root / 'repeated.pdf'
        writer = PdfWriter()
        for source in sources:
            writer.append(self.mixed[source], pages=[0])
        writer.write(path)
        writer.close()
        return path

    def test_cache_keeps_each_page_evidence_and_reprocess_reads_again(self):
        path = self.repeated_pdf()
        with patch.object(extraction, 'recognize_embedded', return_value=self.complete) as recognize, \
             patch.object(extraction, 'PDF_OCR_CACHE_MAX_ENTRIES', 0):
            baseline = extraction.extract(path)
        self.assertEqual(recognize.call_count, 3)

        revised = replace(self.complete, text=FORM.replace('71,5', '72,5'))
        with patch.object(extraction, 'recognize_embedded', side_effect=[self.complete, revised]) as recognize:
            cached = extraction.extract(path)
            self.assertEqual(recognize.call_count, 1)
            reprocessed = extraction.extract(path)
            self.assertEqual(recognize.call_count, 2)
        self.assertEqual(cached, baseline)
        self.assertEqual(cached['page_numbers'], [1, 2, 3, 1, 2, 3])
        self.assertEqual(cached['ocr_pages'], [4, 5, 6])
        self.assertEqual([item['page'] for item in cached['ocr_alternatives']], [1, 2, 3])
        self.assertEqual(reprocessed['pages'][3:], [revised.text] * 3)

    def test_cache_retries_failed_incomplete_and_empty_reads(self):
        path = self.repeated_pdf()
        failure_cases = (
            RuntimeError('temporary failure'),
            replace(self.complete, unavailable=('Tesseract',)),
            replace(self.complete, verification_engine=None),
            replace(self.complete, text='', lines=()),
            replace(self.complete, alternatives=(reading('', 'tesseract-fast'),)),
        )
        for failure in failure_cases:
            with self.subTest(failure=failure), \
                 patch.object(extraction, 'recognize_embedded', side_effect=[failure, self.complete]) as recognize:
                data = extraction.extract(path)
                self.assertEqual(recognize.call_count, 2)
                self.assertEqual(data['pages'][-2:], [self.complete.text] * 2)

    def test_cache_respects_entry_and_byte_budgets(self):
        path = self.repeated_pdf((0, 1, 0, 1))
        cases = (('PDF_OCR_CACHE_MAX_ENTRIES', 1, 3), ('PDF_OCR_CACHE_MAX_BYTES', 1, 4))
        for limit, value, expected_calls in cases:
            with self.subTest(limit=limit), \
                 patch.object(extraction, limit, value), \
                 patch.object(extraction, 'recognize_embedded', return_value=self.complete) as recognize:
                data = extraction.extract(path)
                self.assertEqual(recognize.call_count, expected_calls)
                self.assertEqual(data['pages'][4:], [self.complete.text] * 4)


if __name__ == '__main__':
    unittest.main()
