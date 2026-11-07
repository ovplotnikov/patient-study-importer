"""Create version-matched, offline visual help from fresh synthetic UI captures."""
from pathlib import Path
import base64
from html import escape
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patient_importer import __version__
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle

root=Path(__file__).resolve().parents[1]
screens=Path(sys.argv[1]);out=Path(sys.argv[2]) if len(sys.argv)>2 else root/'build'/'guide'
out.mkdir(parents=True,exist_ok=True)
steps=[
 ('01-ready.png','Добавьте исследования','Нажмите «Добавить файлы» или перетащите их в окно. Можно добавить папку целиком.','Нажмите «Обработать». Исходники остаются на месте; программа работает с копиями.'),
 ('02-result.png','Посмотрите результат','У каждого файла есть свой результат: добавлен, повтор, нужно уточнение или ошибка.','Повторы не создают новые исследования. Новые данные добавляются в ту же таблицу.'),
 ('03-review.png','Уточните пациента','Сравните документ слева с ФИО и датой рождения справа. Выберите совпадение или введите данные.','Нажмите «Подтвердить пациента». Совпадение только даты рождения ещё не подтверждает личность.'),
 ('05-finished.png','Откройте таблицу','Нажмите «Открыть таблицу». Сверьте пациента, даты, значения и единицы с оригиналом.','Для своих записей используйте «Заметки», дополнительные колонки или отдельные листы.'),
 ('06-errors.png','Исправьте ошибку','В результате нажмите «Разобрать». Откройте файл, устраните причину и нажмите «Повторить».','Файл вернётся в очередь. Закройте Excel перед новой обработкой.')]
for name,*_ in steps:
    if not (screens/name).is_file():raise SystemExit('Missing current UI capture: '+name)
font=next((p for p in [Path('C:/Windows/Fonts/arial.ttf'),Path('/System/Library/Fonts/Supplemental/Arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()),None)
if font is None:raise SystemExit('A Cyrillic font is required')
pdfmetrics.registerFont(TTFont('Guide',str(font)))
pdf=canvas.Canvas(str(out/'Краткая инструкция.pdf'),pagesize=(1100,900))
pdf.setTitle('Исследования пациентов — краткая инструкция '+__version__)
html='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Помощь — Исследования пациентов</title><style>*{box-sizing:border-box}body{margin:0;background:#f6f8fb;color:#17212f;font:18px/1.5 "Segoe UI",Arial,sans-serif}main{max-width:1160px;margin:auto;padding:36px 28px}h1{font-size:34px;line-height:1.2}h2{font-size:27px;line-height:1.25}header{padding-bottom:22px;border-bottom:1px solid #dbe2ea}nav{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}a{color:#075bbe}section{margin:40px 0;padding-bottom:28px;border-bottom:1px solid #dbe2ea}img{width:100%;height:auto;border:1px solid #dbe2ea}p{max-width:78ch}small{color:#586779}b.number{display:inline-block;background:#075bbe;color:white;padding:4px 14px;margin-right:10px}aside{background:#eef5ff;padding:20px}summary{cursor:pointer;font-weight:bold;padding:12px 0}@media print{nav{display:none}section{break-inside:avoid}main{padding:0}}</style><main>'''
html+=f'<header><small>ОФЛАЙН-ИНСТРУКЦИЯ · ВЕРСИЯ {__version__}</small><h1>От бланков к общей таблице</h1><p>Добавьте документы. Проверьте спорные случаи. Откройте результат.</p></header><nav>'
for i,(_,title,*_) in enumerate(steps,1):html+=f'<a href="#step{i}">{i}. {escape(title)}</a>'
html+='</nav>'
for index,(filename,title,line1,line2) in enumerate(steps,1):
    path=screens/filename
    image_data=base64.b64encode(path.read_bytes()).decode()
    html+=f'<section id="step{index}"><h2><b class="number">{index}</b>{escape(title)}</h2><p>{escape(line1)}</p><img src="data:image/png;base64,{image_data}" alt="{escape(title)}"><p>{escape(line2)}</p></section>'
    pdf.setFillColorRGB(.025,.357,.745);pdf.rect(0,852,1100,48,fill=1,stroke=0)
    pdf.setFillColorRGB(1,1,1);pdf.setFont('Guide',14);pdf.drawString(32,870,f'ИССЛЕДОВАНИЯ ПАЦИЕНТОВ  /  {__version__}')
    pdf.setFillColorRGB(.09,.13,.18);pdf.setFont('Guide',27);pdf.drawString(32,810,f'{index}. {title}')
    pdf.setFont('Guide',15);pdf.drawString(32,777,line1)
    image=ImageReader(str(path));w,h=image.getSize();scale=min(1036/w,620/h)
    pdf.drawImage(image,(1100-w*scale)/2,128+(620-h*scale)/2,width=w*scale,height=h*scale)
    paragraph=Paragraph(escape(line2),ParagraphStyle('Note',fontName='Guide',fontSize=15,leading=21,textColor='#17212f'))
    _,height=paragraph.wrap(1036,80);paragraph.drawOn(pdf,32,100-height)
    pdf.setFont('Guide',11);pdf.setFillColorRGB(.35,.40,.47)
    pdf.drawString(32,30,'На скриншотах — только вымышленные примеры.');pdf.drawRightString(1068,30,f'{index} / {len(steps)+1}')
    pdf.showPage()
html+='''<aside><h2>Обновления и сохранность работы</h2><p>При запуске программа сама проверяет наличие новой версии. Если предложено обновление, можно согласиться или отложить. Таблица, база и исходники сохраняются. Встроенная помощь обновляется вместе с программой.</p><p>Для переноса на другой компьютер копируйте всю папку данных. Её можно открыть через «Папка данных» внизу окна.</p></aside><details><summary>Полезные подробности</summary><p>Крестик у файла убирает его из очереди: копия остаётся в «Отложено», исходник не удаляется. Вернуть файл можно через «Добавить файлы».</p><p>PDF и изображения видны в окне сверки как оригинал. Для DOCX и TXT показан извлечённый текст; кнопка «Оригинал» открывает сам файл. Если документ содержит нескольких пациентов, разделите его перед импортом.</p><p>Таблица содержит листы «Пациенты», «Исследования», «Показатели», «Заключения», «Проверка», «Источники» и «Тексты». Успешный импорт не гарантирует полноту распознавания. Значения с пометкой «Проверить» нужно сверить с бланком.</p><p>Обработанные файлы находятся в «Обработано», спорные — в «Нужно уточнить», повторы — в «Дубли», нечитаемые — в «Ошибки». База и копии оригиналов находятся в «База», резервные копии — в «Резервные копии».</p><p>Обработка выполняется локально. Ваша рабочая папка может содержать медицинские сведения; её не нужно отправлять для установки или обновления программы.</p></details><small>Эта инструкция включена в программу и не требует интернета.</small></main></html>'''
(out/'guide.html').write_text(html,encoding='utf8')
embedded=root/'patient_importer'/'assets'/'help';embedded.mkdir(parents=True,exist_ok=True)
(embedded/'guide.html').write_text(html,encoding='utf8')
pdf.setFillColorRGB(.09,.13,.18);pdf.setFont('Guide',27);pdf.drawString(40,815,'Обновления и сохранность работы')
lines=[('Обновления', 'Программа проверяет новые версии при запуске. Установку можно отложить.', 'Данные сохраняются, встроенная помощь обновляется вместе с EXE.'),('Перенос на другой компьютер','Через «Папка данных» откройте рабочую папку и скопируйте её целиком.','Таблица, база и оригиналы должны оставаться вместе.'),('Если нужно остановиться','Нажмите «Остановить»: текущий документ завершится, результат сохранится.','При следующем запуске можно продолжить обработку оставшихся файлов.'),('Сверка результатов','Проверяйте пациента, даты, значения и единицы по оригиналу.','Пометка «Добавлен» означает сохранение документа, а не проверку всех данных.'),('Подробная помощь','Кнопка «Помощь» открывает актуальную инструкцию без интернета.','Скриншоты в этом руководстве содержат только вымышленные примеры.')]
y=730
for title,a,b in lines:
    pdf.setFillColorRGB(.025,.357,.745);pdf.setFont('Guide',20);pdf.drawString(40,y,title)
    pdf.setFillColorRGB(.09,.13,.18);pdf.setFont('Guide',16);pdf.drawString(40,y-34,a);pdf.drawString(40,y-61,b);y-=133
pdf.setFont('Guide',11);pdf.drawString(40,30,f'Версия {__version__}');pdf.drawRightString(1060,30,'6 / 6');pdf.save()
print('Visual guide generated from current synthetic screenshots')
