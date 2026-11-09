"""Which documents can skip manual review, and what must still reach it. All data is fictional."""
import unittest

from patient_importer.analysis import analyze
from patient_importer.parsing import classify_pages, document_dates, identity, laboratory_rows
from patient_importer.quality import issues
from test_lab_layout import HEADER, row

NAME = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
DAY = 'Дата выполнения исследования: 02.03.2025\n'
ULTRASOUND = 'Ультразвуковое исследование почек\n'
EXAM = 'Осмотр врача\nЖалобы: учебные жалобы.\n'
LAB = 'Общий анализ мочи\nПоказатель  Результат  Единицы\n'
BLOOD = 'Общий анализ крови\nПоказатель  Результат  Единицы\n'


def parsed(*pages, **extraction):
    return analyze({'pages': list(pages), 'ocr_pages': [], **extraction}, 'Учебный бланк.txt')


def codes(result):
    return {issue['code'] for issue in issues(result)}


def values(result):
    return {(item['label'], item['result']) for item in result['observations']}


def only(result, result_value):
    return next(item for item in result['observations'] if item['result'] == result_value)


class NarrativeLabelTests(unittest.TestCase):
    def test_label_is_not_taken_from_inside_another_word(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Простая киста правой почки 25 мм.')
        self.assertEqual(result['observations'], [])
        self.assertIn('25 мм', result['sections'][0]['text'])

    def test_adjective_does_not_become_volume(self):
        self.assertEqual(parsed(NAME + DAY + ULTRASOUND + 'Объемное образование 25 мм.')['observations'], [])

    def test_label_does_not_cross_a_sentence(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Креатинин в норме. Глюкоза 5,5 ммоль/л.')
        self.assertEqual(result['observations'], [])

    def test_label_does_not_cross_a_comma(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Лоханка не расширена, киста верхнего полюса 20 мм.')
        self.assertEqual(result['observations'], [])

    def test_label_in_the_same_clause_is_kept(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Паренхима 15 мм.\nРост 120 см.')
        self.assertEqual(values(result), {('Паренхима', '15'), ('Рост', '120')})

    def test_abbreviation_does_not_end_the_clause(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Паренхима толщ. 15 мм.')
        self.assertEqual(values(result), {('Паренхима', '15')})

    def test_unnamed_number_on_a_lab_form_still_goes_to_review(self):
        result = parsed(NAME + DAY + BLOOD + 'Гемоглобин  120  г/л', 'Другой показатель 5 ммоль/л.')
        self.assertEqual(only(result, '5')['label'], 'Измерение из описания')
        self.assertIn('ambiguous_value', codes(result))


class NarrativeContextTests(unittest.TestCase):
    def test_side_needs_a_whole_word(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Правая почка: признаков заболевания нет, паренхима 15 мм.')
        self.assertEqual(only(result, '15')['side'], 'Правая')

    def test_side_words_with_a_preposition(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Толщина паренхимы слева 13 мм.')
        self.assertEqual(only(result, '13')['side'], 'Левая')

    def test_kidney_side_does_not_reach_the_bladder(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Левая почка 100*50 мм. Мочевой пузырь объем 300 мл.')
        self.assertEqual(only(result, '100*50')['side'], 'Левая')
        self.assertEqual(only(result, '300')['side'], '')

    def test_comparison_date_is_not_the_study_date(self):
        result = parsed(NAME + DAY + ULTRASOUND + 'Сравнение с исследованием от 01.02.2023\nПаренхима 15 мм.')
        self.assertEqual(only(result, '15')['date'], '2025-03-02')

    def test_date_ends_with_its_sentence(self):
        result = parsed(NAME + DAY + EXAM + 'Анамнез: УЗИ от 01.02.2020 без патологии.\nПаренхима 15 мм.')
        self.assertEqual(result['observations'], [])

    def test_date_carries_over_a_wrapped_line(self):
        result = parsed(NAME + DAY + EXAM + 'Анамнез: УЗИ от 01.02.2020:\nпаренхима 15 мм.')
        self.assertEqual(only(result, '15')['date'], '2020-02-01')

    def test_undated_history_stays_in_the_text(self):
        result = parsed(NAME + DAY + EXAM + 'Анамнез: паренхима 15 мм.')
        self.assertEqual(result['observations'], [])
        self.assertTrue(any('15 мм' in section['text'] for section in result['sections']))

    def test_recommendations_with_split_letters_are_excluded(self):
        result = parsed(NAME + DAY + EXAM + 'Реко менд ации:\nОбъем питья 1500 мл.')
        self.assertEqual(result['observations'], [])


class PatientOnEveryPageTests(unittest.TestCase):
    def test_page_with_its_own_study_and_no_name_is_flagged(self):
        result = parsed(NAME + DAY + BLOOD + 'Гемоглобин  120  г/л', DAY + BLOOD + 'Гемоглобин  90  г/л')
        self.assertEqual(result['pages_without_patient'], [2])
        self.assertIn('page_without_patient', codes(result))

    def test_continuation_page_is_not_flagged(self):
        result = parsed(NAME + DAY + BLOOD + 'Гемоглобин  120  г/л', 'Учебное продолжение текста.')
        self.assertEqual(result['pages_without_patient'], [])

    def test_named_pages_are_not_flagged(self):
        page = NAME + DAY + BLOOD + 'Гемоглобин  120  г/л'
        self.assertEqual(parsed(page, page)['pages_without_patient'], [])


class IdentityTests(unittest.TestCase):
    def test_empty_birth_date_does_not_borrow_the_next_date(self):
        self.assertEqual(identity(['Дата рождения:      Дата исследования: 05.03.2024'])[1], [])

    def test_birth_date_in_brackets(self):
        names, births, _ = identity(['ФИО пациента: ТЕСТОВ ТЕСТ ТЕСТОВИЧ (01.01.2000)'])
        self.assertEqual((names, births), (['ТЕСТОВ ТЕСТ ТЕСТОВИЧ'], ['2000-01-01']))

    def test_name_without_colon(self):
        self.assertEqual(identity(['ФИО пациента   ТЕСТОВ ТЕСТ ТЕСТОВИЧ'])[0], ['ТЕСТОВ ТЕСТ ТЕСТОВИЧ'])

    def test_birth_date_with_a_suffix(self):
        names, births, _ = identity(['ФИО, дата рождения(возраст):ТЕСТОВ ТЕСТ ТЕСТОВИЧ, 10 лет (01.01.2000 г.р.)'])
        self.assertEqual((names, births), (['ТЕСТОВ ТЕСТ ТЕСТОВИЧ'], ['2000-01-01']))

    def test_misspelled_birth_label(self):
        self.assertEqual(identity(['Дата рожднения пациента: 01.01.2000'])[1], ['2000-01-01'])

    def test_representative_is_not_the_patient(self):
        self.assertEqual(identity(['Законный представитель ребенка ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ'])[0], [])


class DocumentDateTests(unittest.TestCase):
    def test_label_after_a_column_gap(self):
        self.assertEqual(document_dates('Справка выдана      Дата и время посещения: 02.03.2025 10:00'), ['2025-03-02'])

    def test_label_inside_a_sentence_is_ignored(self):
        self.assertEqual(document_dates('Уточнена дата и время посещения: 02.03.2025'), [])


class DocumentTypeTests(unittest.TestCase):
    def kind(self, *pages):
        return classify_pages(list(pages))[0]

    def test_service_line_below_the_diagnosis(self):
        self.assertEqual(self.kind('Диагноз: учебный.\nУслуга: Микционная цистоуретрография'), 'МЦУГ')
        self.assertEqual(self.kind('Диагноз: учебный.\nУслуга: Внутривенная урография'), 'Урография')

    def test_split_letters_in_a_heading(self):
        self.assertEqual(self.kind('Жало б ы:\nучебные.'), 'Осмотр или выписка')

    def test_form_with_text_right_after_the_word(self):
        self.assertEqual(self.kind('Анамнез жизни, учебный текст.'), 'Осмотр или выписка')

    def test_word_inside_a_longer_one_is_not_history(self):
        self.assertEqual(self.kind('Анамнезе учебное упоминание.'), 'Документ')

    def test_cited_result_is_not_a_title(self):
        self.assertEqual(self.kind('ОАМ от 01.02.2020: белок отрицательно.'), 'Документ')

    def test_one_dated_title_is_kept(self):
        self.assertEqual(self.kind('Биохимический анализ крови от 01.02.2020'), 'Биохимия крови')

    def test_list_of_dated_results_is_not_a_title(self):
        self.assertEqual(self.kind('ОАМ от 01.02.2020\nОАК от 01.02.2020'), 'Документ')

    def test_titles_on_a_continuation_page_are_prose(self):
        self.assertEqual(self.kind(EXAM, 'ОАК\nОАМ\nУЗИ'), 'Осмотр или выписка')

    def test_page_with_its_own_patient_is_a_new_document(self):
        pages = (EXAM, NAME + 'Ультразвуковое исследование почек\n')
        self.assertEqual(classify_pages(list(pages))[1], ['Осмотр или выписка', 'УЗИ'])

    def test_summary_without_a_table_stays_one_summary(self):
        self.assertEqual(self.kind('Выписной эпикриз\nОАК\nУЗИ почек\nЖалобы: учебные.'), 'Осмотр или выписка')

    def test_summary_with_a_table_stays_a_conflict(self):
        pages = 'Выписной эпикриз\nОАК\nЖалобы: учебные.\nПоказатель  Результат\nГемоглобин  120 г/л'
        self.assertEqual(self.kind(pages), 'Документ')


class ReviewRuleTests(unittest.TestCase):
    def lab(self, *rows, head=LAB):
        return parsed(NAME + DAY + head + '\n'.join(rows))

    def test_printed_dash_is_carried_over_without_review(self):
        result = self.lab('Белок  0,1  г/л', 'Глюкоза  -')
        self.assertEqual(only(result, '-')['result'], '-')
        self.assertNotIn('missing_result', codes(result))

    def test_form_of_dashes_only_has_no_results(self):
        self.assertIn('lab_no_results', codes(self.lab('Белок  -', 'Глюкоза  -')))

    def test_known_units(self):
        for unit in ('10Е9/л', '10E3/мкл', '10Е12/л', '1/мкл', '1/поле зр.', 'в п/зр', 'мм.рт.ст.', 'мОсм/кг'):
            with self.subTest(unit=unit):
                self.assertNotIn('unknown_unit', codes(self.lab(f'Учебный тест  5  {unit}', head=BLOOD)))

    def test_unknown_unit_is_still_flagged(self):
        self.assertIn('unknown_unit', codes(self.lab('Учебный тест  5  ктл', head=BLOOD)))

    def test_long_labels_of_unitless_tests(self):
        result = self.lab('Определение концентрации водородных ионов (pH) мочи  6,0',
                          'Определение удельного веса (относительной плотности) мочи  1015')
        self.assertNotIn('missing_unit', codes(result))

    def test_sediment_counts_on_a_urine_form(self):
        self.assertNotIn('missing_unit', codes(self.lab('Лейкоциты  2-4', 'Плоский эпителий  1-2')))

    def test_blood_count_without_a_unit_is_flagged(self):
        self.assertIn('missing_unit', codes(self.lab('Лейкоциты  5,2', head=BLOOD)))

    def test_semi_quantitative_result(self):
        self.assertNotIn('ambiguous_value', codes(self.lab('Бактерии  2+')))

    def test_damaged_number_is_flagged(self):
        self.assertIn('ambiguous_value', codes(self.lab('Бактерии  2с3')))


class ExtractionNoiseTests(unittest.TestCase):
    PAGE = NAME + DAY + BLOOD + 'Гемоглобин  120  г/л'

    def extraction_issue(self, code, *pages, **extraction):
        issue = {'code': code, 'message': 'Учебное.', 'severity': 'review', 'page': 1}
        return parsed(*pages, extraction_issues=[issue], **extraction)

    def test_signature_image_does_not_block(self):
        self.assertEqual(codes(self.extraction_issue('SIGNATURE_IMAGE', self.PAGE)), set())

    def test_blank_image_beside_text_does_not_block(self):
        result = self.extraction_issue('OCR_EMPTY', self.PAGE, '', ocr_pages=[2], page_numbers=[1, 1])
        self.assertEqual(codes(result), set())

    def test_blank_scan_blocks(self):
        result = self.extraction_issue('OCR_EMPTY', '', ocr_pages=[1], page_numbers=[1])
        self.assertIn('OCR_EMPTY', codes(result))

    def test_ocr_that_only_repeated_the_text_layer_does_not_block(self):
        self.assertEqual(codes(self.extraction_issue('OCR_USED', self.PAGE)), set())

    def test_text_read_by_ocr_blocks(self):
        result = self.extraction_issue('OCR_USED', self.PAGE, 'Учебный текст со скана', ocr_pages=[2], page_numbers=[1, 1])
        self.assertTrue({'OCR_USED', 'ocr_text'} <= codes(result))


class TableContinuationTests(unittest.TestCase):
    FIRST = row('Гемоглобин', '120', 'г/л', '(110 - 140)')
    SECOND = row('Эритроциты', '4,5', 'г/л', '(4 - 5)')

    def test_table_continues_on_a_page_without_its_header(self):
        rows, pending = laboratory_rows([HEADER + '\n' + self.FIRST, 'Страница 2\n' + self.SECOND])
        self.assertEqual([item['label'] for item in rows], ['Гемоглобин', 'Эритроциты'])
        self.assertEqual(pending, [])

    def test_page_text_is_not_taken_for_a_row(self):
        rows, pending = laboratory_rows([HEADER + '\n' + self.FIRST, 'Врач            Учебный\nСтраница        2'])
        self.assertEqual([item['label'] for item in rows], ['Гемоглобин'])
        self.assertEqual(pending, [])

    def test_table_does_not_reach_past_a_page_without_rows(self):
        rows, _ = laboratory_rows([HEADER + '\n' + self.FIRST, 'Учебный текст', self.SECOND])
        self.assertEqual([item['label'] for item in rows], ['Гемоглобин'])

    def test_signature_ends_the_table_for_the_next_page(self):
        rows, _ = laboratory_rows([HEADER + '\n' + self.FIRST + '\nПодпись', self.SECOND])
        self.assertEqual([item['label'] for item in rows], ['Гемоглобин'])

    def test_rows_after_a_comment_are_kept(self):
        rows, pending = laboratory_rows(['\n'.join([HEADER, self.FIRST, 'Комментарий: учебный', 'контроль', self.SECOND])])
        self.assertEqual([item['label'] for item in rows], ['Гемоглобин', 'Эритроциты'])
        self.assertEqual(pending, [])

    def test_continuation_page_values_are_not_counted_twice(self):
        result = parsed(NAME + DAY + HEADER + '\n' + self.FIRST, self.SECOND)
        self.assertEqual([item['label'] for item in result['observations']], ['Гемоглобин', 'Эритроциты'])

    def test_exclamation_mark_is_a_deviation_mark(self):
        rows, _ = laboratory_rows(['Откл.  Показатель  Результат  Ед.изм.  Реф.интервал\n'
                                   '!   Гемоглобин   90   г/л   (110 - 140)'])
        self.assertEqual((rows[0]['label'], rows[0]['flag']), ('Гемоглобин', '!'))

    def test_null_character_is_read_as_a_deviation_mark(self):
        rows, _ = laboratory_rows(['Откл.  Показатель  Результат  Ед.изм.  Реф.интервал\n'
                                   '\x00   Гемоглобин   90   г/л   (110 - 140)'])
        self.assertEqual(rows[0]['flag'], '!')
        self.assertNotIn('\x00', rows[0]['raw'])

    def test_title_above_rows_is_a_group(self):
        rows, pending = laboratory_rows(['\n'.join([HEADER, self.FIRST, 'Учебная группа:', self.SECOND])])
        self.assertEqual(rows[1]['section'], 'Учебная группа')
        self.assertEqual(pending, [])

    def test_lone_note_stays_for_review(self):
        _, pending = laboratory_rows(['\n'.join([HEADER, self.FIRST, 'Примечание врача:'])])
        self.assertEqual(len(pending), 1)


if __name__ == '__main__':
    unittest.main()
