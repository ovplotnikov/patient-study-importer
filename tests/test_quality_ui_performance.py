"""Review-window responsiveness and preview reuse with synthetic originals."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from time import monotonic, sleep
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from PIL import Image, TiffImagePlugin

from patient_importer import quality_ui, ui
from patient_importer.storage import ReviewCommittedError


class QualityUIPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f'Tk display unavailable: {exc}') from exc
        cls.root.withdraw()
        ui.setup(cls.root)
        cls.addClassCleanup(cls.root.destroy)

    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.original = Path(folder) / 'synthetic.txt'
        self.original.write_text('Учебный оригинал', encoding='utf-8')
        self.analysis = {'pages': ['Учебный оригинал'], 'observations': [], 'sections': []}
        self.record = {'id': 'D-demo', 'filename': self.original.name, 'issues': [],
                       'revision': 'demo-revision', 'date': '2025-03-02', 'type': 'УЗИ почек',
                       'observations': [{'label': 'Учебный показатель', 'result': '5', 'page': 1}],
                       'analysis': self.analysis}
        self.other = dict(self.record, id='D-other')
        self.workspace = Mock()
        self.workspace.document.return_value = {'original': str(self.original),
                                                'analysis': '{"pages":["Учебный оригинал"]}'}
        self.workspace.quality_review_list.return_value = [self.record, self.other]
        self.workspace.document_list.return_value = [self.record, self.other]
        self.actions = ui.WorkspaceActions(self.root)
        self.icons = ui.Icons(self.root)
        self.on_change = Mock()
        self.started = Event()
        self.released = Event()
        self.main_thread = get_ident()
        self.errors = []
        self.enterContext(patch.object(self.root, 'report_callback_exception', self.report_error))
        self.dialog = self.enterContext(patch.object(quality_ui.messagebox, 'showerror'))
        self.warning = self.enterContext(patch.object(quality_ui.messagebox, 'showwarning'))
        self.addCleanup(self.finish_widgets)

    def report_error(self, _kind, error, _traceback):
        self.errors.append(error)

    def finish_widgets(self):
        self.released.set()
        self.pump_until(lambda: not self.actions.active)
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def pump_until(self, condition):
        deadline = monotonic() + 5
        while not condition():
            self.root.update()
            self.assertFalse(self.errors, self.errors)
            self.assertLess(monotonic(), deadline, 'Tk operation timed out')
            sleep(.005)
        self.root.update()
        self.assertFalse(self.errors, self.errors)

    def block(self, _document_id, *_args, **_kwargs):
        self.worker_thread = get_ident()
        self.started.set()
        if not self.released.wait(3):
            raise TimeoutError('Test did not release worker')

    def test_quality_keeps_tk_responsive_and_preserves_stale_revision_error(self):
        error = ValueError('Данные изменились. Откройте документ для сверки снова.')

        def save(*args, **kwargs):
            self.block(*args, **kwargs)
            raise error

        self.workspace.confirm_quality.side_effect = save
        review = quality_ui.QualityReviewWindow(self.root, self.workspace, self.on_change,
                                                self.icons, actions=self.actions)
        review.note.insert('1.0', 'Учебная сверка')
        review.verified.set(True)
        review.enable_confirm()
        review.confirm_button.invoke()
        self.assertTrue(self.started.wait(1))
        self.assertNotEqual(self.worker_thread, self.main_thread)
        self.assertTrue(self.actions.active)
        self.assertEqual(str(review.confirm_button['state']), 'disabled')
        painted = []
        self.root.after(0, lambda: painted.append(get_ident()))
        self.pump_until(lambda: painted)
        self.assertEqual(painted, [self.main_thread])
        review.confirm()
        review.close()
        review.list.selection_set('D-other')
        review.load()
        self.assertTrue(review.window.winfo_exists())
        self.assertEqual(review.selected, 'D-demo')
        self.assertEqual(review.list.selection(), ('D-demo',))
        self.workspace.confirm_quality.assert_called_once()
        self.assertEqual(self.workspace.confirm_quality.call_args.kwargs['expected_revision'], 'demo-revision')
        self.assertIsNot(self.workspace.confirm_quality.call_args.kwargs['observations'], review.observations)
        self.released.set()
        self.pump_until(lambda: not self.actions.active)
        self.assertEqual(review.validation.get(), str(error))
        self.assertEqual(review.note.get('1.0', 'end-1c'), 'Учебная сверка')
        self.assertEqual(str(review.confirm_button['state']), 'normal')
        self.on_change.assert_not_called()
        self.dialog.assert_not_called()

    def test_duplicate_cannot_be_closed_or_recorded_twice_in_flight(self):
        self.workspace.mark_duplicate.side_effect = self.block
        done = []
        parent = tk.Toplevel(self.root)
        review = quality_ui.DuplicateWindow(parent, self.workspace, self.record, 'D-other',
                                             self.icons, lambda: done.append(get_ident()),
                                             actions=self.actions)
        review.confirm_button.invoke()
        self.assertTrue(self.started.wait(1))
        review.confirm()
        review.close()
        self.assertTrue(review.window.winfo_exists())
        self.assertEqual(str(review.cancel_button['state']), 'disabled')
        self.workspace.mark_duplicate.assert_called_once_with(
            'D-demo', 'D-other', 'Оригиналы сверены: повтор того же исследования',
            expected_revision='demo-revision')
        self.released.set()
        self.pump_until(lambda: not self.actions.active)
        self.assertFalse(review.window.winfo_exists())
        self.assertEqual(done, [self.main_thread])
        self.assertNotEqual(self.worker_thread, self.main_thread)

    def test_history_cancel_before_apply_and_keep_candidate_after_failed_save(self):
        candidate = {'document_id': 'D-demo', 'revision': 'demo-revision',
                     'previous_analysis': self.analysis, 'proposed_analysis': self.analysis,
                     'analysis': self.analysis}
        self.workspace.prepare_reprocess.return_value = candidate
        error = RuntimeError('Сбой сохранения')

        def save(*args, **kwargs):
            self.block(*args, **kwargs)
            raise error

        self.workspace.apply_reprocess.side_effect = save
        history = quality_ui.HistoryWindow(self.root, self.workspace, self.on_change,
                                           self.icons, actions=self.actions)
        history.reprocess()
        history.reprocess()
        history.close()
        self.assertTrue(history.window.winfo_exists())
        self.pump_until(lambda: history.candidate is not None)
        self.workspace.prepare_reprocess.assert_called_once_with('D-demo')
        history.cancel_button.invoke()
        self.assertIsNone(history.candidate)
        self.workspace.apply_reprocess.assert_not_called()
        history.reprocess()
        self.pump_until(lambda: history.candidate is not None)
        history.apply_button.invoke()
        self.assertTrue(self.started.wait(1))
        history.apply()
        history.close_candidate()
        self.assertTrue(history.candidate_window.winfo_exists())
        self.assertNotEqual(self.worker_thread, self.main_thread)
        self.workspace.apply_reprocess.assert_called_once_with('D-demo', candidate)
        self.released.set()
        self.pump_until(lambda: not self.actions.active)
        self.assertIs(history.candidate, candidate)
        self.assertEqual(str(history.apply_button['state']), 'normal')
        self.assertEqual(str(history.cancel_button['state']), 'normal')
        self.dialog.assert_called_once_with('Не удалось применить', str(error),
                                            parent=history.candidate_window)

    def test_quality_retains_edits_if_workbook_is_locked_before_commit(self):
        error = RuntimeError('Закройте Excel и повторите действие.')
        self.workspace.confirm_quality.side_effect = error
        review = quality_ui.QualityReviewWindow(self.root, self.workspace, self.on_change,
                                                self.icons, actions=self.actions)
        review.note.insert('1.0', 'Исправление учебных данных')
        review.verified.set(True)
        review.confirm()
        self.pump_until(lambda: not self.actions.active)
        self.assertEqual(review.selected, 'D-demo')
        self.assertEqual(review.note.get('1.0', 'end-1c'), 'Исправление учебных данных')
        self.assertEqual(str(review.confirm_button['state']), 'normal')
        self.on_change.assert_not_called()
        self.dialog.assert_called_once_with('Не удалось сохранить проверку', str(error), parent=review.window)

    def test_duplicate_after_commit_closes_stale_modal_and_refreshes(self):
        error = ReviewCommittedError('Решение сохранено в базе. Обновите таблицу.')
        self.workspace.mark_duplicate.side_effect = error
        parent = tk.Toplevel(self.root)
        review = quality_ui.DuplicateWindow(parent, self.workspace, self.record, 'D-other',
                                             self.icons, self.on_change, actions=self.actions)
        review.confirm_button.invoke()
        self.pump_until(lambda: not self.actions.active)
        self.assertFalse(review.window.winfo_exists())
        self.on_change.assert_called_once_with()
        self.warning.assert_called_once_with('Решение сохранено', str(error), parent=parent)
        self.dialog.assert_not_called()

    def test_history_after_commit_discards_stale_candidate_and_refreshes(self):
        candidate = {'document_id': 'D-demo', 'revision': 'demo-revision',
                     'previous_analysis': self.analysis, 'proposed_analysis': self.analysis,
                     'analysis': self.analysis}
        error = ReviewCommittedError('Решение сохранено в базе. Обновите таблицу.')
        self.workspace.apply_reprocess.side_effect = error
        history = quality_ui.HistoryWindow(self.root, self.workspace, self.on_change,
                                           self.icons, actions=self.actions)
        history.show_candidate(candidate)
        history.apply_button.invoke()
        self.pump_until(lambda: not self.actions.active)
        self.assertIsNone(history.candidate)
        self.assertFalse(history.candidate_window.winfo_exists())
        self.assertEqual(self.workspace.document_list.call_count, 2)
        self.on_change.assert_called_once_with()
        self.warning.assert_called_once_with('Результат сохранён', str(error), parent=history.window)
        self.dialog.assert_not_called()

    def test_preview_decodes_only_selected_tiff_frames_and_keeps_two_pages(self):
        path = self.original.with_suffix('.tiff')
        frames = [Image.new('RGB', (100, 120), (i * 60, 0, 0)) for i in range(3)]
        frames[0].save(path, save_all=True, append_images=frames[1:])
        preview = quality_ui.SourcePreview(self.root, self.icons)
        document = {'original': str(path)}
        evidence = [{'source_kind': 'image', 'page': 1, 'line': line, 'page_index': 1,
                     'image_size': [100, 120], 'bbox': [4, line * 10, 70, line * 10 + 8]}
                    for line in (1, 2)]
        analysis = dict(self.analysis, ocr_evidence=evidence)
        decoded = []
        load_frame = TiffImagePlugin.TiffImageFile.load

        def spy(image):
            if image.tile:
                decoded.append(image.tell())
            return load_frame(image)

        with patch.object(TiffImagePlugin.TiffImageFile, 'load', spy):
            preview.load(document, analysis)
            self.assertEqual(decoded, [])
            self.root.update()
            self.assertEqual(decoded, [0])
            first_image = preview.canvas.find_all()[0]
            for line in (1, 2):
                preview.locate({'page': 1, 'page_index': 1, 'line': line})
                self.assertEqual(len(preview.canvas.find_withtag('source-highlight')), 1)
                self.assertEqual(preview.canvas.find_all()[0], first_image)
            self.assertEqual(decoded, [0])
            preview.turn(1)
            preview.turn(-1)
            self.assertEqual(decoded, [0, 1])
            preview.turn(2)
            self.assertEqual(decoded, [0, 1, 2])
            self.assertEqual(len(preview.photos), 2)
            self.assertNotIn(1, {page for page, _width in preview.photos})
            preview.load({'original': str(self.original)}, self.analysis)
            self.assertFalse(preview.has_image())
            self.assertEqual(preview.photos, {})
            self.assertEqual(preview.text.get('1.0', 'end-1c'), 'Учебный оригинал')

    def test_pdf_page_cache_reuses_bitmap_and_scales_highlight_after_zoom(self):
        from pypdf import PdfWriter

        path = self.original.with_suffix('.pdf')
        writer = PdfWriter()
        for _ in range(2):
            writer.add_blank_page(width=300, height=420)
        writer.write(path)
        preview = quality_ui.SourcePreview(self.root, self.icons)
        evidence = [{'source_kind': 'page', 'page': 1, 'line': 1, 'page_index': 1,
                     'image_size': [300, 420], 'bbox': [40, 90, 160, 110]}]
        observation = {'page': 1, 'page_index': 1, 'line': 1}

        with patch.object(preview, 'render_photo', wraps=preview.render_photo) as render_photo:
            preview.load({'original': str(path)}, dict(self.analysis, ocr_evidence=evidence))
            self.root.update()
            self.assertEqual(preview.count(), 2)
            preview.locate(observation)
            highlight = preview.canvas.find_withtag('source-highlight')[0]
            before = preview.canvas.coords(highlight)
            self.assertEqual(render_photo.call_count, 1)
            preview.turn(1)
            preview.locate(observation)
            self.assertEqual(render_photo.call_count, 2)
            preview.resize(.25)
            highlight = preview.canvas.find_withtag('source-highlight')[0]
            after = preview.canvas.coords(highlight)
            self.assertEqual(render_photo.call_count, 3)
            self.assertGreater(after[2] - after[0], before[2] - before[0])
            self.assertLessEqual(sum(photo.width() * photo.height()
                                     for photo in preview.photos.values()), preview.CACHE_PIXELS)
