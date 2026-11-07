"""Create version-matched, offline visual help from fresh synthetic UI captures."""
from pathlib import Path
import base64
from html import escape
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patient_importer import __version__
from patient_importer.diagnostics_config import SUPPORT_EMAIL
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle

root=Path(__file__).resolve().parents[1]
screens=Path(sys.argv[1]);out=Path(sys.argv[2]) if len(sys.argv)>2 else root/'build'/'guide'
out.mkdir(parents=True,exist_ok=True)
support_action=(
    f'«Открыть письмо» создаёт черновик на {SUPPORT_EMAIL}. Приложите учебный пример и отправьте письмо сами.'
    if SUPPORT_EMAIL else 'На снимке учебный адрес. В этой сборке адрес поддержки ещё не задан.'
)
steps=[
 ('01-ready.png', 'Добавьте исследования',
  'Добавьте файлы, выберите папку или перетащите документы в окно.',
  'Закройте таблицу в Excel и нажмите «Обработать». Исходники останутся на месте.'),
 ('02-result.png', 'Посмотрите, что требует внимания',
  'Нажмите «Проверить документы», если нужно уточнить пациента или сверить данные.',
  'Если файл не читается, нажмите «Разобрать». После исправления нажмите «Повторить», чтобы вернуть файл в очередь.'),
 ('03-review.png', 'Уточните пациента',
  'Сверьте полные ФИО и дату рождения с оригиналом. Выберите совпадение или введите данные.',
  'Нажмите «Подтвердить пациента». Затем проверьте сами результаты исследования, если они требуют сверки.'),
 ('07-quality-review.png', 'Сверьте исследование',
  'Сравните с оригиналом дату, вид, показатели и текст заключения. Для сканов сверка обязательна.',
  'Нажмите «Сверить текст заключения», если нужно исправить текст. После сверки всех страниц поставьте отметку и подтвердите данные.'),
 ('08-field-editor.png', 'Исправьте показатель',
  'Введите значение, знак, единицу и референс как в оригинале. Добавьте пропущенную строку при необходимости.',
  'Если нужного поля нет в бланке, оставьте его пустым и объясните причину в заметке к сверке.'),
 ('05-finished.png', 'Откройте таблицу',
  'Откройте «Исследования» для статусов, «Показатели» для значений и «Проверка» для открытых замечаний.',
  'Решения сохраняются в листе «Сверки». Для своих записей используйте «Заметки», дополнительные колонки или листы.'),
 ('10-history.png', 'Повторно разберите оригинал',
  'Откройте «Папка данных → Все исследования», выберите документ и нажмите «Распознать заново».',
  'Сравните «Сейчас», «Новое чтение» и «Будет сохранено». Подтверждённые вручную данные сохраняются.'),
 ('11-diagnostics.png', 'Выберите, отправлять ли диагностику',
  'Если сервис подключён, можно разрешить отправку технических счётчиков. Исследования остаются на компьютере.',
  'Выбор сохраняется после обновления. В «Помощь → Диагностика…» можно отключить сбор и очистить очередь.'),
 ('12-template-help.png', 'Предложите поддержку бланка',
  'Нажмите «Как помочь». Подготовьте пустой шаблон без личных данных или новый вымышленный пример.',
  support_action)]
for name,*_ in steps:
    if not (screens/name).is_file():raise SystemExit('Missing current UI capture: '+name)
font=next((p for p in [Path('C:/Windows/Fonts/arial.ttf'),Path('/System/Library/Fonts/Supplemental/Arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()),None)
if font is None:raise SystemExit('A Cyrillic font is required')
pdfmetrics.registerFont(TTFont('Guide',str(font)))
pdf=canvas.Canvas(str(out/'Краткая инструкция.pdf'),pagesize=(1100,900))
pdf.setTitle('Исследования пациентов — краткая инструкция '+__version__)
body_style=ParagraphStyle('Guide text',fontName='Guide',fontSize=16,leading=22,textColor='#17212f')


def draw_text(message, left, top, width):
    paragraph=Paragraph(escape(message),body_style)
    _,height=paragraph.wrap(width,100)
    paragraph.drawOn(pdf,left,top-height)


html='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Помощь — Исследования пациентов</title><style>*{box-sizing:border-box}body{margin:0;background:#f6f8fb;color:#17212f;font:18px/1.5 "Segoe UI",Arial,sans-serif}main{max-width:1160px;margin:auto;padding:36px 28px}h1{font-size:34px;line-height:1.2}h2{font-size:27px;line-height:1.25}header{padding-bottom:22px;border-bottom:1px solid #dbe2ea}nav{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}a{color:#075bbe}section{margin:40px 0;padding-bottom:28px;border-bottom:1px solid #dbe2ea}img{width:100%;height:auto;border:1px solid #dbe2ea}p{max-width:78ch}small{color:#586779}b.number{display:inline-block;background:#075bbe;color:white;padding:4px 14px;margin-right:10px}aside{background:#eef5ff;padding:20px}summary{cursor:pointer;font-weight:bold;padding:12px 0}@media print{nav{display:none}section{break-inside:avoid}main{padding:0}}</style><main>'''
html+=f'<header><small>ОФЛАЙН-ИНСТРУКЦИЯ · ВЕРСИЯ {__version__}</small><h1>От бланков к общей таблице</h1><p>Добавьте документы. Проверьте спорные случаи. Откройте результат.</p></header><nav>'
for i,(_,title,*_) in enumerate(steps,1):html+=f'<a href="#step{i}">{i}. {escape(title)}</a>'
html+='</nav>'
for index,(filename,title,line1,line2) in enumerate(steps,1):
    path=screens/filename
    image_data=base64.b64encode(path.read_bytes()).decode()
    image=ImageReader(str(path));w,h=image.getSize()
    html+=f'<section id="step{index}"><h2><b class="number">{index}</b>{escape(title)}</h2><p>{escape(line1)}</p><img src="data:image/png;base64,{image_data}" alt="{escape(title)}" style="max-width:{w}px"><p>{escape(line2)}</p></section>'
    pdf.setFillColorRGB(.025,.357,.745);pdf.rect(0,852,1100,48,fill=1,stroke=0)
    pdf.setFillColorRGB(1,1,1);pdf.setFont('Guide',14);pdf.drawString(32,870,f'ИССЛЕДОВАНИЯ ПАЦИЕНТОВ  /  {__version__}')
    pdf.setFillColorRGB(.09,.13,.18);pdf.setFont('Guide',27);pdf.drawString(32,810,f'{index}. {title}')
    draw_text(line1,32,785,1036)
    scale=min(1036/w,595/h,1.3)
    image_x=(1100-w*scale)/2
    image_y=135+(595-h*scale)/2
    pdf.drawImage(image,image_x,image_y,width=w*scale,height=h*scale)
    pdf.setStrokeColorRGB(.86,.89,.92)
    pdf.rect(image_x,image_y,w*scale,h*scale,fill=0,stroke=1)
    pdf.setFillColorRGB(.933,.961,1);pdf.rect(32,51,1036,68,fill=1,stroke=0)
    draw_text(line2,48,104,1004)
    pdf.setFont('Guide',11);pdf.setFillColorRGB(.35,.40,.47)
    pdf.drawString(32,30,'Примеры исследований на скриншотах вымышлены.');pdf.drawRightString(1068,30,f'{index} / {len(steps)}')
    pdf.showPage()
html+='''<aside><h2>Обновления и перенос данных</h2><p>Программа проверяет обновления при запуске и предлагает установить новую версию. Можно согласиться или отложить. Таблица, база и оригиналы сохраняются. Встроенная помощь обновляется вместе с программой.</p><p>Для переноса на другой компьютер копируйте всю рабочую папку. Откройте её через «Папка данных» внизу окна.</p></aside>
<details><summary>Оригиналы и очередь</summary><p>Крестик у файла убирает копию из очереди в «Отложено». Вернуть файл можно через «Добавить файлы». При остановке программа завершает текущий файл и сохраняет результат.</p><p>Для PDF и изображений в окне сверки виден оригинал. Для DOCX и TXT показан извлечённый текст. Кнопка «Оригинал» открывает сам файл. Документ с несколькими пациентами разделите перед импортом.</p></details>
<details><summary>Возможные дубли</summary><p>Точные копии файла пропускаются. Если найден возможный дубль, сравните оба оригинала. Для отметки дубля должны совпасть исследование и подтверждённый пациент. Оба источника сохраняются, а повторные показатели убираются из рабочего списка.</p></details>
<details><summary>Помощь и диагностика</summary><p>Подсказку для нового бланка можно открыть через «Помощь → Помочь с новым бланком…». Для скриншота используйте вымышленный пример. Даже после закрашивания ФИО в исходном бланке могут остаться другие личные данные.</p><p>Для диагностики нужен настроенный сервис и ваше согласие. Без сервиса сбор отключён. На снимке диагностики показан учебный сценарий.</p></details>
<details><summary>Рабочая папка</summary><p>Обработанные файлы находятся в «Обработано», требующие сверки — в «Нужно уточнить», повторы — в «Дубли», нечитаемые — в «Ошибки». База и копии оригиналов лежат в «База», резервные копии — в «Резервные копии».</p><p>Обработка выполняется локально. Рабочая папка может содержать медицинские сведения. Для установки или обновления отправлять её не требуется.</p></details>
<small>Встроенная инструкция доступна без интернета.</small></main></html>'''
(out/'guide.html').write_text(html,encoding='utf8')
embedded=root/'patient_importer'/'assets'/'help';embedded.mkdir(parents=True,exist_ok=True)
(embedded/'guide.html').write_text(html,encoding='utf8')
pdf.save()
print('Visual guide generated from current synthetic screenshots')
