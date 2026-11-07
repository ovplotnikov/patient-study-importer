"""Local OCR adapters. Recognition output is evidence, never verified data."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import csv
import io
import os
import statistics
import subprocess
import tempfile


@dataclass(frozen=True)
class Line:
    text: str
    bbox: tuple[int, int, int, int] | None
    confidence: float | None


@dataclass(frozen=True)
class Page:
    text: str
    lines: tuple[Line, ...]
    engine: str
    image_size: tuple[int, int] | None
    alternatives: tuple[Page, ...] = ()
    unavailable: tuple[str, ...] = ()
    verification_engine: str | None = None


def assemble_page(words, engine, image_size, slope=0.):
    """Keep geometric row gaps for the current table parser."""
    if not words:
        return Page('', (), engine, image_size)
    scale = max(2, statistics.median(
        (bbox[2] - bbox[0]) / max(1, len(text)) for text, bbox, _ in words
    ))
    rows = []
    def row_center(word):
        left, top, right, bottom = word[1]
        return (top + bottom - slope * (left + right)) / 2

    for word in sorted(words, key=row_center):
        _, (_, top, _, bottom), _ = word
        center = row_center(word)
        nearest = min(rows, key=lambda row: abs(row[0] - center)) if rows else None
        if nearest is not None and abs(nearest[0] - center) <= max(3, (bottom - top) * .45):
            nearest[1].append(word)
        else:
            rows.append((center, [word]))
    lines = []
    for _, row in rows:
        parts = []
        previous_right = None
        for text, (left, _, right, _), _ in sorted(row, key=lambda w: w[1][0]):
            if previous_right is None:
                spaces = max(0, round(left / scale))
            else:
                gap = left - previous_right
                spaces = 1 if gap < scale * 1.5 else max(2, round(gap / scale))
            parts.append(' ' * spaces + text)
            previous_right = right
        boxes = [bbox for _, bbox, _ in row]
        # Preserve the weakest measured token, rather than hiding it in a mean.
        scores = [score for _, _, score in row if score is not None]
        bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
        lines.append(Line(''.join(parts), bbox, min(scores) if scores else None))
    return Page('\n'.join(line.text for line in lines), tuple(lines), engine, image_size)


def tesseract_page(image, executable, tessdata, engine, psm=3):
    with tempfile.TemporaryDirectory(prefix='patient-ocr-') as directory:
        source = Path(directory) / 'page.png'
        image.save(source)
        command = [str(executable), str(source), 'stdout', '--tessdata-dir',
                   str(tessdata), '-l', 'rus+eng', '--psm', str(psm),
                   '-c', 'tessedit_create_tsv=1']
        try:
            result = subprocess.run(command, capture_output=True, timeout=180,
                                    creationflags=0x08000000 if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('Не удалось запустить распознавание страницы скана') from exc
        if result.returncode:
            raise RuntimeError('Не удалось распознать страницу скана')
    words = []
    for row in csv.DictReader(io.StringIO(result.stdout.decode('utf-8', 'replace')), delimiter='\t'):
        if row.get('level') != '5' or not row.get('text', '').strip():
            continue
        left, top, width, height = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
        conf = float(row['conf'])
        words.append((row['text'], (left, top, left + width, top + height),
                      min(1., conf / 100) if conf >= 0 else None))
    return assemble_page(words, engine, image.size)


RAPID_MODELS = {
    'Det': 'ch_PP-OCRv5_det_mobile.onnx',
    'Cls': 'ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx',
}


def open_rapid_engine(model_dir, *, language='cyrillic'):
    """Load only explicitly provisioned models. There is no download route here."""
    from rapidocr import RapidOCR, EngineType, LangRec, ModelType, OCRVersion

    if language not in {'eslav', 'cyrillic'}:
        raise ValueError('Unsupported OCR language model')
    params = {
        'Global.max_side_len': 2000, 'Global.log_level': 'error',
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'Rec.lang_type': LangRec.ESLAV if language == 'eslav' else LangRec.CYRILLIC, 'Rec.model_type': ModelType.MOBILE,
    }
    models = {**RAPID_MODELS, 'Rec': f'{language}_PP-OCRv5_rec_mobile.onnx'}
    for stage, name in models.items():
        path = model_dir / name
        if not path.is_file():
            raise FileNotFoundError(path)
        params.update({f'{stage}.engine_type': EngineType.ONNXRUNTIME,
                       f'{stage}.ocr_version': OCRVersion.PPOCRV5,
                       f'{stage}.model_type': ModelType.MOBILE,
                       f'{stage}.model_path': str(path)})
    return RapidOCR(params=params)


def rapid_page(image, engine, *, improve_geometry=False, engine_name='paddle-cyrillic'):
    import numpy as np

    def recognize(frame):
        result = engine(np.asarray(frame.convert('RGB'))[:, :, ::-1].copy())
        words, slopes = [], []
        horizontal, vertical = 0, 0
        for points, text, score in zip(
                result.boxes if result.boxes is not None else (),
                result.txts or (), result.scores or ()):
            x, y = points[:, 0], points[:, 1]
            left, top, right, bottom = int(x.min()), int(y.min()), int(x.max()), int(y.max())
            words.append((text, (left, top, right, bottom), float(score)))
            width, height = right - left, bottom - top
            if len(text) >= 3 and width > 2 * height:
                horizontal += len(text)
                dx = float(points[1][0] - points[0][0])
                if dx > 1:
                    slope = float(points[1][1] - points[0][1]) / dx
                    if abs(slope) <= .15:
                        slopes.append(slope)
            if len(text) >= 3 and height > 2 * width:
                vertical += len(text)
        slope = statistics.median(slopes) if improve_geometry and slopes else 0.
        return assemble_page(words, engine_name, frame.size, slope), vertical > horizontal

    page, vertical = recognize(image)
    if not improve_geometry or not vertical:
        return page
    # OCR each plausible page orientation and keep a single reading.
    # Coordinates are mapped back to the supplied image for source preview.
    from PIL import Image
    alternatives = []
    width, height = image.size
    for turn in (Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_270):
        candidate, _ = recognize(image.transpose(turn))
        lines = []
        for line in candidate.lines:
            left, top, right, bottom = line.bbox
            if turn == Image.Transpose.ROTATE_90:
                bbox = (width-bottom, left, width-top, right)
            else:
                bbox = (top, height-right, bottom, height-left)
            lines.append(replace(line, bbox=bbox))
        alternatives.append(replace(candidate, lines=tuple(lines), image_size=image.size))

    def orientation_score(candidate):
        return sum(len(line.text.strip()) * (line.confidence or 0) for line in candidate.lines)

    return max(alternatives, key=orientation_score) if any(p.text for p in alternatives) else page

def same_reading(primary, other):
    def normalized_lines(page):
        # Spacing and equivalent minus glyphs do not change a reading.
        return [' '.join(line.text.replace('−', '-').split())
                for line in page.lines if line.text.strip()]

    return normalized_lines(primary) == normalized_lines(other)
