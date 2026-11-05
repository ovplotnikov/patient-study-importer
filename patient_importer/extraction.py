"""Offline document readers and bundled OCR."""
from pathlib import Path
from functools import lru_cache
import csv
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import zipfile

from docx import Document
from pypdf import PdfReader

SUPPORTED = {'.pdf', '.docx', '.txt', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}


def resource_path(name):
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return root / 'vendor' / name


def supported(path):
    if path.suffix.lower() in SUPPORTED:
        return True
    with path.open('rb') as handle:
        return handle.read(5) == b'%PDF-'


def recognize_images(images):
    executable = resource_path('tesseract/tesseract.exe' if os.name == 'nt' else 'tesseract/tesseract')
    if not executable.exists():
        raise RuntimeError('В комплекте программы отсутствует модуль распознавания сканов')
    output = []
    for image in images:
        with tempfile.TemporaryDirectory(prefix='patient-ocr-') as directory:
            source = Path(directory)/'page.png'
            image.save(source)
            command = [str(executable), str(source), 'stdout', '--tessdata-dir', str(executable.parent/'tessdata'), '-l', 'rus+eng', '--psm', '3', '-c', 'tessedit_create_tsv=1']
            process = subprocess.run(command, capture_output=True, timeout=180, creationflags=0x08000000 if os.name=='nt' else 0)
            if process.returncode:
                raise RuntimeError('Не удалось распознать страницу скана')
            # Rebuild columns from coordinates, retaining evidence for table parsing.
            words = []
            for row in csv.DictReader(io.StringIO(process.stdout.decode('utf-8','replace')),delimiter='\t'):
                if row.get('level')!='5' or not row.get('text','').strip():
                    continue
                words.append(row)
            lines=[]
            for word in sorted(words,key=lambda w:int(w['top'])+int(w['height'])/2):
                center=int(word['top'])+int(word['height'])/2
                nearest=min(lines,key=lambda line:abs(line['center']-center)) if lines else None
                if nearest and abs(nearest['center']-center)<=max(3,int(word['height'])*.45):nearest['words'].append(word)
                else:lines.append({'center':center,'words':[word]})
            rendered = []
            scale=max(2,statistics.median(int(w['width'])/max(1,len(w['text'])) for w in words)) if words else 8
            for line in lines:
                words=line['words']
                text = '';previous_right=None
                for word in sorted(words,key=lambda w:int(w['left'])):
                    left=int(word['left'])
                    gap=left if previous_right is None else left-previous_right
                    spaces=max(0,round(gap/scale)) if previous_right is None else (1 if gap<scale*1.5 else max(2,round(gap/scale)))
                    text+=' '*spaces+word['text']
                    previous_right=left+int(word['width'])
                rendered.append(text)
            output.append('\n'.join(rendered))
    return output


def native_mac_ocr(path):
    executable=resource_path('mac-helper')
    if not executable.exists():
        raise RuntimeError('В комплекте программы отсутствует модуль OCR')
    process=subprocess.run([str(executable),str(path)],capture_output=True,text=True,timeout=180)
    if process.returncode:
        raise RuntimeError('Не удалось распознать документ')
    return json.loads(process.stdout)


@lru_cache(maxsize=128)
def recognize_embedded(data):
    from PIL import Image
    image=Image.open(io.BytesIO(data)).convert('RGB')
    if sys.platform=='darwin' and not resource_path('tesseract/tesseract').exists():
        with tempfile.TemporaryDirectory(prefix='patient-insert-') as directory:
            source=Path(directory)/'image.png';image.save(source)
            return native_mac_ocr(source)[0]
    return recognize_images([image])[0]


def extract(path):
    with path.open('rb') as handle:
        is_pdf=handle.read(5)==b'%PDF-'
    if is_pdf:
        reader=PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF защищён паролем')
        pages=[page.extract_text(extraction_mode='layout') for page in reader.pages]
        native_pages=list(pages)
        # A genuinely empty PDF page must not turn the whole document into OCR.
        targets=[i for i,page in enumerate(reader.pages) if len(pages[i].strip())<20 and len(page.images)>0]
        if targets:
            if sys.platform=='darwin' and not resource_path('tesseract/tesseract').exists():
                recognized=native_mac_ocr(path)
                for i in targets:
                    pages[i]=recognized[i]
            else:
                import pypdfium2 as pdfium
                pdf=pdfium.PdfDocument(path)
                try:
                    images=[pdf[i].render(scale=2.5).to_pil() for i in targets]
                    for i,text in zip(targets,recognize_images(images)):
                        pages[i]=text
                finally:
                    pdf.close()
        ocr_pages={i+1 for i in targets}
        page_numbers=list(range(1,len(pages)+1))
        for i,page in enumerate(reader.pages):
            if i in targets:continue
            seen=set()
            for embedded in page.images:
                image=embedded.image
                if image.width<400 or image.height<150:continue
                if embedded.data in seen:continue
                seen.add(embedded.data)
                text=recognize_embedded(embedded.data).strip()
                # Signature stamps carry certificate metadata, not study results.
                if re.search(r'сертификат|документ.{0,25}подписан|электронн.{0,15}подпис',text,re.I|re.S) and not re.search(r'креатинин|паренхим|гемоглобин|лейкоцит|эритроцит|почка|почки',text,re.I):continue
                normalized=lambda value:' '.join(value.lower().split())
                if text and normalized(text) not in normalized(pages[i]):
                    pages.append(text);page_numbers.append(i+1)
                    ocr_pages.add(len(pages))
        return {'pages':pages,'page_numbers':page_numbers,'native_pages':native_pages,'ocr_pages':sorted(ocr_pages),'format':'PDF'}
    if path.suffix.lower()=='.docx':
        document=Document(path)
        blocks=[]
        for item in document.iter_inner_content():
            if hasattr(item,'rows'):
                blocks.extend('  '.join(cell.text for cell in row.cells) for row in item.rows)
            else:
                blocks.append(item.text)
        pages=['\n'.join(blocks)]
        ocr_pages=[]
        # Embedded scans are part of the document, even if the DOCX also has text.
        with zipfile.ZipFile(path) as archive:
            media=[name for name in archive.namelist() if name.startswith('word/media/') and Path(name).suffix.lower() in {'.png','.jpg','.jpeg','.tif','.tiff','.bmp'}]
            for name in media:
                from PIL import Image
                image=Image.open(io.BytesIO(archive.read(name)))
                if image.width<400 or image.height<200:
                    continue
                with tempfile.TemporaryDirectory(prefix='patient-docx-') as directory:
                    source=Path(directory)/'scan.png';image.save(source)
                    text=recognize_embedded(source.read_bytes())
                pages.append(text);ocr_pages.append(len(pages))
        return {'pages':pages,'ocr_pages':ocr_pages,'format':'DOCX'}
    if path.suffix.lower()=='.txt':
        raw=path.read_bytes()
        for encoding in ['utf-8-sig','utf-16','cp1251']:
            try:
                text=raw.decode(encoding)
                return {'pages':[text],'ocr_pages':[],'format':'TXT'}
            except UnicodeError:
                continue
        raise ValueError('Не удалось прочитать кодировку TXT')
    if sys.platform=='darwin' and not resource_path('tesseract/tesseract').exists():
        pages=native_mac_ocr(path)
    else:
        from PIL import Image, ImageSequence, ImageOps
        with Image.open(path) as image:
            pages=recognize_images([ImageOps.exif_transpose(frame.copy()).convert('RGB') for frame in ImageSequence.Iterator(image)])
    return {'pages':pages,'ocr_pages':list(range(1,len(pages)+1)),'format':'Изображение'}
