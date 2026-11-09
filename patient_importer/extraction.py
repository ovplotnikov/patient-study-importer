"""Offline document readers and bundled OCR."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path
from threading import Lock
from xml.etree import ElementTree
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

from docx import Document
from PIL import Image, ImageOps, ImageSequence
from pypdf import PdfReader

from .ocr import Line, Page, open_rapid_engine, rapid_page, same_reading, tesseract_page
from .pdf_render import PDFIUM_LOCK

SUPPORTED = {'.pdf', '.docx', '.txt', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}
RASTER_SUFFIXES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.gif', '.webp', '.jp2'}
PDF_RENDER_SCALE = 300 / 72
MAX_RENDER_PIXELS = 18_000_000
MAX_PARALLEL_OCR_PIXELS = 18_000_000
PDF_OCR_CACHE_MAX_ENTRIES = 16
PDF_OCR_CACHE_MAX_BYTES = 8 * 1024 * 1024

# RapidOCR reuses a mutable engine. Import and manual reprocessing can each
# call the extractor, so keep one active OCR page per process.
_OCR_PAGE_LOCK = Lock()


def resource_path(name):
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return root / 'vendor' / name


def supported(path):
    if path.suffix.lower() in SUPPORTED:
        return True
    with path.open('rb') as handle:
        return handle.read(5) == b'%PDF-'


def native_mac_ocr(path):
    executable = resource_path('mac-helper')
    if not executable.exists():
        raise RuntimeError('В комплекте программы отсутствует модуль OCR')
    process = subprocess.run([str(executable), str(path)], capture_output=True, text=True, timeout=180)
    if process.returncode:
        raise RuntimeError('Не удалось распознать документ')
    return json.loads(process.stdout)


def text_only_page(text, engine, image_size=None):
    return Page(text, tuple(Line(line, None, None) for line in text.splitlines()), engine, image_size)


@lru_cache(maxsize=1)
def rapid_engine(model_dir):
    return open_rapid_engine(model_dir)


def recognize_image(image):
    with _OCR_PAGE_LOCK:
        return _recognize_image(image)


def _recognize_image(image):
    image = ImageOps.exif_transpose(image).convert('RGB')
    model_dir = resource_path('rapidocr')
    executable = resource_path('tesseract/tesseract.exe' if os.name == 'nt' else 'tesseract/tesseract')
    paddle = None
    tesseract = None
    unavailable = []
    bundled_windows = os.name == 'nt' and getattr(sys, 'frozen', False)
    use_paddle = model_dir.is_dir() or bundled_windows
    has_tesseract = executable.exists()
    tesseract_args = executable, executable.parent / 'tessdata', 'tesseract-fast'
    with ExitStack() as stack:
        secondary = None
        if use_paddle and has_tesseract and image.width * image.height <= MAX_PARALLEL_OCR_PIXELS:
            # Tesseract only reads its own copy. Keep the worker scoped to the
            # page and limit the extra RGB copy to 54 MB.
            try:
                worker = stack.enter_context(ThreadPoolExecutor(max_workers=1))
                secondary = worker.submit(tesseract_page, image.copy(), *tesseract_args)
            except (MemoryError, RuntimeError):
                # If a worker cannot start, run the same two reads in order.
                pass
        if use_paddle:
            try:
                paddle = rapid_page(image, rapid_engine(model_dir), improve_geometry=True)
            except Exception:
                unavailable.append('PaddleOCR')
        if has_tesseract:
            try:
                tesseract = (secondary.result() if secondary is not None
                             else tesseract_page(image, *tesseract_args))
            except RuntimeError:
                unavailable.append('Tesseract')
        elif bundled_windows:
            unavailable.append('Tesseract')
    if paddle and paddle.text.strip():
        alternatives = (tesseract,) if tesseract and not same_reading(paddle, tesseract) else ()
        return replace(paddle, alternatives=alternatives, unavailable=tuple(unavailable),
                       verification_engine=tesseract.engine if tesseract is not None else None)
    if tesseract is not None:
        alternatives = (paddle,) if paddle and not same_reading(tesseract, paddle) else ()
        return replace(tesseract, alternatives=alternatives, unavailable=tuple(unavailable))
    if paddle is not None:
        return replace(paddle, unavailable=tuple(unavailable))
    if sys.platform == 'darwin' and not executable.exists():
        with tempfile.TemporaryDirectory(prefix='patient-ocr-') as directory:
            source = Path(directory) / 'page.png'
            image.save(source)
            return text_only_page(native_mac_ocr(source)[0], 'apple-vision', image.size)
    raise RuntimeError('Не удалось распознать страницу скана')

def recognize_embedded(data):
    with Image.open(io.BytesIO(data)) as image:
        return recognize_image(image)


def _cacheable_ocr_size(page):
    """Estimate retained OCR bytes for a complete, nonempty two-engine read."""
    if not isinstance(page, Page) or page.verification_engine is None:
        return 0
    readings = (page, *page.alternatives)
    if any(reading.unavailable or not reading.text.strip() for reading in readings):
        return 0
    return sum(512 + sys.getsizeof(reading.text) +
               sum(512 + sys.getsizeof(line.text) for line in reading.lines)
               for reading in readings)


def issue(result, code, message, page=None):
    item = {'code': code, 'message': message, 'severity': 'review'}
    if page is not None:
        item['page'] = page
    if item not in result['extraction_issues']:
        result['extraction_issues'].append(item)


def new_result(format_name, pages=()):
    return {'pages': list(pages), 'page_numbers': list(range(1, len(pages)+1)),
            'native_pages': list(pages), 'ocr_pages': [], 'format': format_name,
            'ocr_evidence': [], 'ocr_alternatives': [], 'extraction_issues': []}


def add_recognition(result, recognized, page, *, replace=None, source='page', image_index=None, evidence_only=False):
    # Text-only adapters can still supply review evidence without fake confidence.
    if isinstance(recognized, str):
        recognized = text_only_page(recognized, 'text-only-ocr')
    if evidence_only:
        index = page
    elif replace is None:
        result['pages'].append(recognized.text)
        result['page_numbers'].append(page)
        index = len(result['pages'])
    else:
        result['pages'][replace] = recognized.text
        index = replace + 1
    if not evidence_only:
        result['ocr_pages'].append(index)
    for line_number, line in enumerate(recognized.lines, 1):
        result['ocr_evidence'].append({
            **asdict(line), 'page': page, 'page_index': index, 'line': line_number,
            'engine': recognized.engine, 'image_size': recognized.image_size,
            'verification_engine': recognized.verification_engine,
            'source_kind': source, 'source_index': image_index,
        })
    issue(result, 'OCR_USED', 'Сверьте распознанные со скана данные с оригиналом', page)
    for alternate in recognized.alternatives:
        result['ocr_alternatives'].append({
            'page': page, 'page_index': index, 'source_kind': source,
            'source_index': image_index, 'engine': alternate.engine, 'text': alternate.text,
            'image_size': alternate.image_size, 'lines': [asdict(line) for line in alternate.lines],
        })
    if recognized.alternatives:
        issue(result, 'OCR_DISAGREEMENT', 'Два распознавания дали разные результаты. Сверьте значения и единицы с оригиналом', page)
    if recognized.unavailable:
        issue(result, 'OCR_ENGINE_UNAVAILABLE', 'Не удалось выполнить оба прохода OCR', page)
    if any(line.confidence is not None and line.confidence < .55 for line in recognized.lines):
        issue(result, 'OCR_LOW_CONFIDENCE', 'На скане есть неуверенно распознанные строки', page)
    if not recognized.text.strip():
        issue(result, 'OCR_EMPTY', 'В изображении не найден читаемый текст', page)


def pdf_pages(path):
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('PDF защищён паролем')
    pages = []
    failed = set()
    for i, page in enumerate(reader.pages, 1):
        try:
            pages.append(page.extract_text(extraction_mode='layout'))
        except Exception:
            pages.append('')
            failed.add(i)
    result = new_result('PDF', pages)
    for page in sorted(failed):
        issue(result, 'PDF_TEXT_UNREADABLE', 'Не удалось прочитать текстовый слой PDF', page)
    targets = set()
    embedded_keys = {}
    for i, page in enumerate(reader.pages, 1):
        try:
            embedded_keys[i] = list(page.images.keys())
        except Exception:
            issue(result, 'EMBEDDED_IMAGE_UNREADABLE', 'Не удалось прочитать список изображений PDF', i)
            embedded_keys[i] = []
            targets.add(i)
        if i in failed or embedded_keys[i] and len(pages[i-1].strip()) < 20:
            targets.add(i)
        elif len(pages[i-1].strip()) < 20:
            # Vector outlines and some damaged text layers have no image object.
            try:
                has_content = page.get_contents() is not None
            except Exception:
                has_content = True
            if has_content:
                targets.add(i)
            else:
                issue(result, 'PDF_PAGE_EMPTY', 'Страница PDF не содержит читаемого текста', i)
    if targets:
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(path)
        try:
            for i in sorted(targets):
                try:
                    with PDFIUM_LOCK:
                        pdf_page = pdf[i-1]
                        try:
                            width, height = pdf_page.get_size()
                            scale = min(PDF_RENDER_SCALE, (MAX_RENDER_PIXELS / (width * height)) ** .5)
                            bitmap = pdf_page.render(scale=scale)
                            try:
                                image = bitmap.to_pil().copy()
                            finally:
                                bitmap.close()
                        finally:
                            pdf_page.close()
                    add_recognition(result, recognize_image(image), i, replace=i-1)
                except Exception:
                    issue(result, 'OCR_FAILED', 'Не удалось распознать страницу PDF', i)
        finally:
            with PDFIUM_LOCK:
                pdf.close()
    # Cache exact image bytes for this PDF read only. Page-specific decisions and
    # evidence below are still evaluated separately for every occurrence.
    embedded_ocr = {}
    cache_bytes = 0
    for i, page in enumerate(reader.pages, 1):
        if i in targets:
            continue
        seen = set()
        for j, key in enumerate(embedded_keys[i], 1):
            try:
                data = page.images[key].data
                if data in seen:
                    continue
                seen.add(data)
                recognized = embedded_ocr.get(data)
                if recognized is None:
                    recognized = recognize_embedded(data)
                    if len(embedded_ocr) < PDF_OCR_CACHE_MAX_ENTRIES:
                        try:
                            size = _cacheable_ocr_size(recognized)
                            total = size + sys.getsizeof(data)
                            if size and cache_bytes + total <= PDF_OCR_CACHE_MAX_BYTES:
                                embedded_ocr[data] = recognized
                                cache_bytes += total
                        except MemoryError:
                            # A cache allocation must not discard a successful read.
                            pass
            except Exception:
                issue(result, 'EMBEDDED_IMAGE_UNREADABLE', 'Не удалось прочитать вложенное изображение PDF', i)
                continue
            text = recognized if isinstance(recognized, str) else recognized.text
            # Exclude recognizable signature stamps from field parsing, with a visible review reason.
            if re.search(r'сертификат|документ.{0,25}подписан|электронн.{0,15}подпис', text, re.I | re.S) and not re.search(
                    r'креатинин|паренхим|гемоглобин|лейкоцит|эритроцит|почка|почки', text, re.I):
                issue(result, 'SIGNATURE_IMAGE', 'На странице есть изображение электронной подписи. Проверьте, что результаты находятся в основной части бланка', i)
                continue
            normalized = ' '.join(text.split())
            matches_native = bool(normalized and normalized in ' '.join(pages[i-1].split()))
            add_recognition(result, recognized, i, source='embedded', image_index=j,
                            evidence_only=matches_native)
    return result


def docx_pages(path):
    document = Document(path)
    blocks = []
    for item in document.iter_inner_content():
        if hasattr(item, 'rows'):
            blocks.extend('  '.join(cell.text for cell in row.cells) for row in item.rows)
        else:
            blocks.append(item.text)
    result = new_result('DOCX', ['\n'.join(blocks)])
    if document.element.xpath('.//w:txbxContent | .//w:object | .//w:altChunk | .//w:sdt | .//w:tbl//w:tbl'):
        issue(result, 'DOCX_COMPLEX_CONTENT', 'В документе Word есть текстовые блоки или объекты. Проверьте их в оригинале')
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml', name):
                continue
            try:
                tree = ElementTree.fromstring(archive.read(name))
                word_ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
                has_text = any((part.text or '').strip() for part in tree.iter(f'{{{word_ns}}}t'))
            except (ElementTree.ParseError, OSError, zipfile.BadZipFile):
                has_text = True
            if has_text:
                issue(result, 'DOCX_EXTRA_TEXT', 'Проверьте текст в колонтитулах и сносках Word')
        for j, name in enumerate((n for n in archive.namelist() if n.startswith('word/media/') and not n.endswith('/')), 1):
            if Path(name).suffix.lower() not in RASTER_SUFFIXES:
                issue(result, 'EMBEDDED_IMAGE_UNSUPPORTED', 'Формат вложенного изображения Word не поддерживается')
                continue
            try:
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    for frame in ImageSequence.Iterator(image):
                        add_recognition(result, recognize_image(frame), 1,
                                        source='embedded', image_index=j)
            except Exception:
                issue(result, 'EMBEDDED_IMAGE_UNREADABLE', 'Не удалось прочитать вложенное изображение Word')
    return result


def extract(path):
    path = Path(path)
    with path.open('rb') as handle:
        is_pdf = handle.read(5) == b'%PDF-'
    if is_pdf:
        return pdf_pages(path)
    if path.suffix.lower() == '.docx':
        return docx_pages(path)
    if path.suffix.lower() == '.txt':
        raw = path.read_bytes()
        # Without a byte order mark almost any even-length bytes decode as UTF-16.
        encodings = ['utf-16'] if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else ['utf-8-sig', 'cp1251']
        for encoding in encodings:
            try:
                return new_result('TXT', [raw.decode(encoding)])
            except UnicodeError:
                continue
        raise ValueError('Не удалось прочитать кодировку TXT')
    result = new_result('Изображение')
    with Image.open(path) as image:
        for i, frame in enumerate(ImageSequence.Iterator(image), 1):
            try:
                add_recognition(result, recognize_image(frame), i, source='image')
            except RuntimeError:
                add_recognition(result, text_only_page('', 'ocr-failed'), i, source='image')
                issue(result, 'OCR_FAILED', 'Не удалось распознать изображение', i)
    return result
