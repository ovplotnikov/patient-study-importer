"""Compare local OCR engines on generated Russian forms; no document inputs."""
import argparse
from collections import defaultdict
from pathlib import Path
import json
import re
import statistics
import sys
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patient_importer.analysis import analyze
from patient_importer.ocr import open_rapid_engine, rapid_page, tesseract_page


def normalize(text):
    return ' '.join(text.casefold().split())


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j-1] + (left != right)))
        previous = current
    return previous[-1]


def synthetic_form(font, layout, holdout, *, validation=False):
    image = Image.new('RGB', (1450, 920), '#ffffff')
    draw = ImageDraw.Draw(image)
    title = ['Анализ крови биохимический', 'Общий анализ крови', 'Общий анализ мочи'][layout]
    person = 'ТЕСТОВА ПРОБА МАКЕТОВНА' if holdout else 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ'
    born = '27.08.1986' if holdout else '01.01.2000'
    date = '14.09.2024' if holdout else '02.03.2025'
    values = [
        [('Креатинин', '−12,6' if holdout else '71,5', 'мкмоль/л', '40–90'),
         ('Глюкоза', '<0,48' if holdout else '<0,50', 'ммоль/л', '3,6–5,8'),
         ('Альбумин', '38,2' if holdout else '42,7', 'г/л', '35–50')],
        [('Гемоглобин', '≥126' if holdout else '143', 'г/л', '120–160'),
         ('Лейкоциты', '6,42' if holdout else '5,91', '10^9/л', '4–10'),
         ('Тромбоциты', '270' if holdout else '241', '10^9/л', '150–400')],
        [('Белок', '≤0,07' if holdout else '<0,09', 'г/л', '0–0,10'),
         ('Эритроциты', '3' if holdout else '0', 'в п/зр', '0–2'),
         ('Плотность', '1.021' if holdout else '1,016', 'г/мл', '1,005–1,030')],
    ][layout]
    if validation:
        person, born, date = 'ТЕСТОВАЯ ПРОБА ВЫМЫШЛЕННАЯ', '19.11.1973', '27.01.2026'
        values = [(label, value, unit, ref) for (label, _, unit, ref), value in zip(
            values, [('54.7', '≤0,61', '36,8'), ('≥112', '4,65', '188'), ('<0,06', '4', '1,019')][layout]
        )]
    # Deliberately vary title position, size, grid and column order.
    font_main = ImageFont.truetype(font, 34 if layout != 1 else 31)
    font_title = ImageFont.truetype(font, 44)
    x = [68, 90, 52][layout]
    intro = ['СИНТЕТИЧЕСКИЙ БЛАНК', title, 'Пациент: '+person,
             'Дата рождения: '+born, 'Дата выполнения исследования: '+date]
    for j, line in enumerate(intro):
        draw.text((x, 42 + j * 67), line, font=font_title if j == 1 else font_main, fill='black')
    columns = [x, x+455, x+725, x+1010]
    order = [0, 2, 1, 3] if layout == 2 else [0, 1, 2, 3]
    if validation:
        order = [1, 0, 2, 3]
    header = ('Показатель', 'Результат', 'Единицы', 'Норма')
    for i, row in enumerate([header]+values):
        y = 417+i*83
        if layout == 1:
            draw.line((x-12, y-8, 1370, y-8), fill='#ababab', width=2)
        for column, index in zip(columns, order):
            if draw.textbbox((column, y), row[index], font=font_main)[2] > image.width:
                raise ValueError(f'Synthetic field exceeds image width: {row[index]}')
            draw.text((column, y), row[index], font=font_main, fill='black')
    expected = '\n'.join(intro+[' '.join(row[i] for i in order) for row in [header]+values])
    fields = [title, person, born, date]
    rows = [[row[i] for i in order[:3]] for row in values]
    return image, expected, fields, rows, values


def variants(image):
    small = image.resize((image.width//2, image.height//2), Image.Resampling.LANCZOS)
    yield 'clean', image
    yield 'blur', small.filter(ImageFilter.GaussianBlur(.9))
    yield 'skew', image.rotate(3.5, resample=Image.Resampling.BICUBIC, fillcolor='white', expand=True)
    yield 'rotation', image.transpose(Image.Transpose.ROTATE_90)
    yield 'contrast', ImageOps.colorize(ImageOps.grayscale(small), '#555555', '#bfbfbf')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tesseract', type=Path, required=True)
    parser.add_argument('--fast', type=Path, required=True)
    parser.add_argument('--best', type=Path, required=True)
    parser.add_argument('--onnx', type=Path)
    parser.add_argument('--cyrillic', action='store_true')
    parser.add_argument('--font', required=True)
    parser.add_argument('--holdout-font')
    parser.add_argument('--validation-font', help='Compare the fixed selected engine and baseline on fresh layouts only')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.cyrillic and not args.onnx:
        parser.error('--cyrillic requires --onnx')
    if args.validation_font and not args.cyrillic:
        parser.error('--validation-font requires --onnx and --cyrillic')
    engines = {name: lambda image, name=name, data=data: tesseract_page(
        image, args.tesseract, data, name
    ) for name, data in [('tesseract-fast', args.fast), ('tesseract-best', args.best)]}
    engines['tesseract-fast-psm6'] = lambda image: tesseract_page(
        image, args.tesseract, args.fast, 'tesseract-fast-psm6', psm=6
    )
    if args.onnx:
        paddle = open_rapid_engine(args.onnx, language='eslav')
        engines['paddle-eslav'] = lambda image: rapid_page(image, paddle, engine_name='paddle-eslav')
        engines['paddle-eslav-geometry'] = lambda image: rapid_page(image, paddle, improve_geometry=True, engine_name='paddle-eslav')
        if args.cyrillic:
            cyrillic = open_rapid_engine(args.onnx, language='cyrillic')
            engines['paddle-cyrillic-geometry'] = lambda image: rapid_page(
                image, cyrillic, improve_geometry=True, engine_name='paddle-cyrillic'
            )
    if args.validation_font:
        engines = {name: engines[name] for name in ('tesseract-fast', 'paddle-cyrillic-geometry')}
        splits = [('validation', args.validation_font)]
    else:
        splits = [('development', args.font), ('holdout', args.holdout_font or args.font)]
    records = []
    for split, font in splits:
        for layout in range(3):
            image, expected, fields, rows, values = synthetic_form(
                font, layout, split != 'development', validation=split=='validation'
            )
            print(split, layout, flush=True)
            expected = normalize(expected)
            for defect, variant in variants(image):
                for name, recognize in engines.items():
                    start = time.monotonic()
                    page = recognize(variant)
                    seconds = time.monotonic()-start
                    text = normalize(page.text)
                    lines = {normalize(line) for line in page.text.splitlines()}
                    field_ok = sum(bool(re.search(r'(?<!\w)' + re.escape(normalize(field)) + r'(?!\w)', text))
                                   for field in fields)
                    row_ok = sum(any(line.startswith(normalize(' '.join(row))+' ')
                                     or line == normalize(' '.join(row))
                                     for line in lines) for row in rows)
                    data = {'pages': [page.text], 'page_numbers': [1], 'ocr_pages': [1],
                            'native_pages': [], 'format': 'Изображение'}
                    observations = analyze(data, 'synthetic.png')['observations']
                    whitespace = lambda value: ' '.join(str(value).split())
                    parsed_rows = {
                        tuple(whitespace(item.get(key, '')) for key in ('label', 'result', 'unit', 'reference'))
                        for item in observations
                    }
                    parsed_exact = sum(tuple(whitespace(value) for value in row) in parsed_rows for row in values)
                    parsed_triplets = {parsed[:3] for parsed in parsed_rows}
                    parsed_triplet = sum(tuple(whitespace(value) for value in row[:3]) in parsed_triplets
                                         for row in values)
                    records.append(dict(engine=name, split=split,
                                        layout=layout, defect=defect, fields=field_ok, field_count=len(fields),
                                        rows=row_ok, row_count=len(rows), exact_page=int(field_ok==len(fields) and row_ok==len(rows)),
                                        errors=edit_distance(expected, text), characters=len(expected),
                                        parsed_exact=parsed_exact, parsed_triplet=parsed_triplet,
                                        seconds=round(seconds, 4)))
    groups = defaultdict(list)
    for record in records:
        groups[record['engine'], record['split']].append(record)
    for key, group in groups.items():
        total = lambda field: sum(r[field] for r in group)
        print(*key, f'fields={total("fields")}/{total("field_count")}',
              f'rows={total("rows")}/{total("row_count")}', f'pages={total("exact_page")}/{len(group)}',
              f'parsed_triplet={total("parsed_triplet")}/{total("row_count")}',
              f'parsed_full={total("parsed_exact")}/{total("row_count")}',
              f'CER={total("errors")/total("characters"):.1%}',
              f'median={statistics.median(r["seconds"] for r in group):.3f}s')
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
