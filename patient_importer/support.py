"""Manual template contribution: never accepts a document, path or extracted text."""
import re
from urllib.parse import quote, urlencode

from . import __version__

SERIOUS_ISSUES = frozenset({
    'unknown_type', 'lab_no_results', 'missing_narrative_result', 'unparsed_row',
    'unmapped_columns', 'OCR_EMPTY', 'OCR_FAILED', 'PDF_TEXT_UNREADABLE',
    'EMBEDDED_IMAGE_UNREADABLE', 'EMBEDDED_IMAGE_UNSUPPORTED',
})

SUPPORT_STEPS = (
    'Возьмите пустой шаблон клиники или создайте новый пример с вымышленными данными. '
    'Сохраните порядок полей и колонок, который программа не смогла прочитать.',
    'Для картинки сделайте скриншот этого учебного примера. Проверьте, что в кадр '
    'не попали другие окна, имя пациента, путь к файлу, подписи и QR-коды.',
    'Откройте письмо и приложите подготовленный пример самостоятельно. '
    'Кратко опишите ожидаемый результат. Это поможет проверить поддержку формы для будущих версий.',
)
SUPPORT_CAUTION = (
    'Не отправляйте исходное исследование или его скриншот с закрашенным ФИО: '
    'в документе могут остаться сведения, по которым можно узнать пациента. '
    'Программа ничего не обезличивает и не прикладывает к письму.'
)


def has_serious_issue(codes):
    return any(type(code) is str and code in SERIOUS_ISSUES for code in codes)


def support_mailto(address):
    """Only fixed, public application text enters a mail draft; user sends it."""
    if type(address) is not str or not re.fullmatch(
            r'[A-Za-z0-9.!#$%&\'*+/=_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', address):
        raise ValueError('Адрес поддержки не настроен.')
    body = (
        'Хочу предложить поддержку нового бланка.\n'
        f'Версия программы: {__version__}\n\n'
        'Тип исследования и структура формы:\n\n'
        'Что должно получиться:\n\n'
        'Приложу пустой шаблон или заново созданный пример с вымышленными данными. '
        'Реальные документы пациентов, журналы и рабочую базу не отправляю.\n'
    )
    return 'mailto:' + quote(address, safe='@.+') + '?' + urlencode({
        'subject': 'Новый бланк — Исследования пациентов', 'body': body,
    })
