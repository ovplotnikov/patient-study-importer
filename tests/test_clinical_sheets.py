"""Sheets a doctor reads: studies by kind and a patient over the years. All data is fictional."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook

from patient_importer import clinical_sheets as sheets
from patient_importer.storage import Workspace


def lab(label, result, unit='', number='auto', origin='Лабораторный бланк', date=None):
    if number == 'auto':
        try:
            number = float(result.replace(',', '.'))
        except ValueError:
            number = None
    return {'label': label, 'result': result, 'unit': unit, 'number': number, 'origin': origin, 'date': date, 'side': ''}


def study(kind='ОАК', day='2015-03-02', observations=(), sections=(), patient='P-1', name='ТЕСТОВ ТЕСТ ТЕСТОВИЧ',
          status='Готово', document='S-1'):
    return {'id': document, 'patient_id': patient, 'name': name if patient else '', 'birth': '2010-01-15' if patient else '',
            'date': day, 'kind': kind, 'status': status, 'filename': 'учебный.pdf',
            'observations': list(observations), 'sections': list(sections)}


def table(built, name):
    headers, rows, _dates = built[name]
    return [dict(zip(headers, row)) for row in rows]


class ColumnTests(unittest.TestCase):
    def column(self, label, unit, analytes=sheets.BLOOD_COUNT):
        return sheets.column_for(label, unit, analytes)

    def test_usual_unit_goes_to_the_usual_column(self):
        self.assertEqual(self.column('Гемоглобин (HGB)', 'г/л'), 'HGB (г/л)')

    def test_other_unit_does_not_join_it(self):
        self.assertIsNone(self.column('Гемоглобин (HGB)', 'г/дл'))

    def test_spellings_of_a_power_of_ten_are_one_unit(self):
        for unit in ('10^9/л', '10Е9/л', '10E9/л', '×10^9/л', '10*9/л', 'тыс/мкл', 'тыс./мкл', '10E3/мкл'):
            with self.subTest(unit=unit):
                self.assertEqual(self.column('Лейкоциты', unit), 'WBC (10^9/л)')

    def test_share_and_count_are_told_apart_by_unit(self):
        self.assertEqual(self.column('Нейтрофилы', '%'), 'Нейтрофилы (%)')
        self.assertEqual(self.column('Нейтрофилы', '10^9/л'), 'Нейтрофилы (#)')

    def test_share_or_count_is_not_guessed_without_a_unit(self):
        self.assertIsNone(self.column('Нейтрофилы', ''))

    def test_single_column_analyte_accepts_a_blank_unit(self):
        self.assertEqual(self.column('Гемоглобин', ''), 'HGB (г/л)')

    def test_compound_name_is_not_the_word_inside_it(self):
        self.assertEqual(self.column('Среднее содержание гемоглобина (MCH)', 'пг'), 'MCH (пг)')
        self.assertIsNone(self.column('Среднее содержание гемоглобина (MCH)', 'г/л'))

    def test_uric_acid_is_not_urea(self):
        self.assertEqual(self.column('Мочевая кислота', 'мкмоль/л', sheets.BIOCHEMISTRY), 'Мочевая кислота (мкмоль/л)')
        self.assertEqual(self.column('Мочевина', 'ммоль/л', sheets.BIOCHEMISTRY), 'Мочевина (ммоль/л)')

    def test_unknown_analyte_has_no_usual_column(self):
        self.assertIsNone(self.column('Учебный показатель', 'г/л'))


class StudySheetTests(unittest.TestCase):
    def test_years_are_separate_rows_in_date_order(self):
        built = sheets.build([study(day='2017-05-01', document='S-2', observations=[lab('Гемоглобин', '130', 'г/л')]),
                              study(day='2015-03-02', observations=[lab('Гемоглобин', '120', 'г/л')])])
        self.assertEqual([(row['Дата исследования'], row['HGB (г/л)']) for row in table(built, 'ОАК')],
                         [('2015-03-02', 120.0), ('2017-05-01', 130.0)])

    def test_age_on_the_study_date(self):
        row = table(sheets.build([study(observations=[lab('Гемоглобин', '120', 'г/л')])]), 'ОАК')[0]
        self.assertEqual((row['Возраст, лет'], row['Возраст, мес']), (5, 61))

    def test_age_before_the_birthday_of_the_month(self):
        self.assertEqual(sheets.age('2010-01-15', '2015-01-14'), (4, 59))

    def test_age_needs_both_dates(self):
        self.assertEqual(sheets.age('2010-01-15', None), ('', ''))

    def test_text_result_stays_as_printed(self):
        row = table(sheets.build([study(kind='ОАМ', observations=[lab('Глюкоза', 'не обнаружено')])]), 'ОАМ')[0]
        self.assertEqual(row['Глюкоза'], 'не обнаружено')

    def test_other_unit_gets_its_own_column(self):
        row = table(sheets.build([study(observations=[lab('Гемоглобин', '12', 'г/дл')])]), 'ОАК')[0]
        self.assertIsNone(row['HGB (г/л)'])
        self.assertEqual(row['Гемоглобин (г/дл)'], 12.0)

    def test_second_reading_in_a_form_does_not_overwrite_the_first(self):
        row = table(sheets.build([study(kind='ОАМ', observations=[lab('Лейкоциты', '2'), lab('Лейкоциты', '4')])]), 'ОАМ')[0]
        self.assertEqual((row['Лейкоциты'], row['Лейкоциты ·2']), (2.0, 4.0))

    def test_visits_in_one_file_are_separate_rows(self):
        built = sheets.build([study(observations=[lab('Гемоглобин', '120', 'г/л', date='2015-03-02'),
                                                  lab('Гемоглобин', '90', 'г/л', date='2016-03-02')])])
        self.assertEqual(len(table(built, 'ОАК')), 2)

    def test_document_without_a_patient_is_left_out(self):
        self.assertNotIn('ОАК', sheets.build([study(patient=None, observations=[lab('Гемоглобин', '120', 'г/л')])]))

    def test_results_quoted_in_a_history_are_left_out(self):
        built = sheets.build([study(observations=[lab('Гемоглобин', '120', 'г/л'), lab('Креатинин', '50', 'мкмоль/л', origin='Анамнез')])])
        self.assertEqual(len(built['ОАК'][1][0]), len(sheets.COMMON) + len(sheets.BLOOD_COUNT) + len(sheets.TAIL))

    def test_form_with_portions_goes_to_zimnitsky(self):
        built = sheets.build([study(kind='Лабораторное исследование', observations=[lab('Количество мочи (1-я порция)', '100', 'мл')])])
        self.assertIn('Зимницкий', built)

    def test_unrecognised_lab_form_is_kept_on_its_own_sheet(self):
        self.assertIn(sheets.OTHER_SHEET, sheets.build([study(kind='Документ', observations=[lab('Учебный тест', '5', 'г')])]))

    def test_blank_unit_joins_the_column_with_a_unit(self):
        built = sheets.build([study(kind='Реберг', observations=[lab('Учебный клиренс', '90', 'мл/мин')]),
                              study(kind='Реберг', day='2016-01-01', document='S-2', observations=[lab('Учебный клиренс', '80')])])
        self.assertEqual([row['Учебный клиренс (мл/мин)'] for row in table(built, 'Реберг')], [90.0, 80.0])
        self.assertNotIn('Учебный клиренс', built['Реберг'][0])

    def test_column_that_collides_with_a_common_one_is_renamed(self):
        built = sheets.build([study(kind='Реберг', observations=[lab('Статус', '5')])])
        self.assertEqual(len(set(name.lower() for name in built['Реберг'][0])), len(built['Реберг'][0]))

    def test_very_long_column_name_is_cut(self):
        built = sheets.build([study(kind='Реберг', observations=[lab('Учебный ' * 60, '5')])])
        self.assertTrue(all(len(name) <= 200 for name in built['Реберг'][0]))

    def test_row_ids_stay_the_same_between_runs(self):
        data = [study(observations=[lab('Гемоглобин', '120', 'г/л')])]
        self.assertEqual(sheets.build(data)['ОАК'][1][0][0], sheets.build(data)['ОАК'][1][0][0])


class ReportSheetTests(unittest.TestCase):
    CONCLUSION = [{'section': 'Заключение', 'text': 'Учебное заключение.'}]

    def test_ultrasound_measurements_by_side(self):
        rows = [dict(lab('Размеры почки', '90*40', 'мм', number=None, origin='Описание'), side='Правая'),
                dict(lab('Паренхима', '15', 'мм', origin='Описание'), side='Левая')]
        row = table(sheets.build([study(kind='УЗИ', observations=rows, sections=self.CONCLUSION)]), 'УЗИ')[0]
        self.assertEqual((row['Почка правая'], row['Паренхима левая'], row['Заключение']), ('90*40 мм', '15 мм', 'Учебное заключение.'))

    def test_two_measurements_of_one_place_are_both_kept(self):
        rows = [dict(lab('Лоханка', '5', 'мм', origin='Описание'), side='Правая'),
                dict(lab('Лоханка', '8', 'мм', origin='Описание'), side='Правая')]
        self.assertEqual(table(sheets.build([study(kind='УЗИ', observations=rows)]), 'УЗИ')[0]['Лоханка правая'], '5 мм; 8 мм')

    def test_report_without_any_result_adds_no_row(self):
        self.assertNotIn('МРТ', sheets.build([study(kind='МРТ', sections=[{'section': 'Жалобы', 'text': 'Учебные.'}])]))

    def test_visit_note_has_no_sheet(self):
        self.assertNotIn('Осмотр или выписка', sheets.build([study(kind='Осмотр или выписка', sections=self.CONCLUSION)]))


class SummaryTests(unittest.TestCase):
    def built(self):
        return sheets.build([study(day='2015-03-02', observations=[lab('Гемоглобин', '120', 'г/л')]),
                             study(day='2017-05-01', document='S-2', observations=[lab('Гемоглобин', '130', 'г/л')], status='Проверить данные'),
                             study(kind='Биохимия крови', day='2016-01-01', document='S-3', observations=[lab('Креатинин', '50', 'мкмоль/л')])])

    def test_one_row_per_patient_with_the_latest_study_of_each_kind(self):
        rows = table(self.built(), 'Свод')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['Дата ОАК (посл.)'], rows[0]['ОАК: HGB (г/л)']), ('2017-05-01', 130.0))
        self.assertEqual(rows[0]['Биохимия крови: Креатинин (мкмоль/л)'], 50.0)

    def test_unchecked_source_is_marked(self):
        self.assertEqual(table(self.built(), 'Свод')[0]['Есть несверенные данные'], 'Да')

    def test_study_without_a_date_is_not_the_latest(self):
        built = sheets.build([study(day='2015-03-02', observations=[lab('Гемоглобин', '120', 'г/л')]),
                              study(day=None, document='S-2', observations=[lab('Гемоглобин', '90', 'г/л')])])
        self.assertEqual(table(built, 'Свод')[0]['ОАК: HGB (г/л)'], 120.0)


class DynamicsTests(unittest.TestCase):
    def rows(self, *points):
        data = [study(day=day, document=f'S-{index}', observations=[lab('Гемоглобин', value, 'г/л')]) for index, (day, value) in enumerate(points)]
        return table(sheets.build(data), 'Динамика')

    def test_first_last_and_years(self):
        row = self.rows(('2015-03-02', '120'), ('2017-05-01', '130'))[0]
        self.assertEqual((row['Первая дата'], row['Первое значение'], row['Последняя дата'], row['Последнее значение']),
                         ('2015-03-02', 120.0, '2017-05-01', 130.0))
        self.assertEqual((row['2015'], row['2017'], row['Измерений']), (120.0, 130.0, 2))

    def test_last_value_of_a_year(self):
        self.assertEqual(self.rows(('2015-01-02', '100'), ('2015-11-02', '140'), ('2016-01-02', '120'))[0]['2015'], 140.0)

    def test_one_date_is_not_a_trend(self):
        self.assertEqual(self.rows(('2015-03-02', '120')), [])

    def test_text_results_are_left_out(self):
        data = [study(kind='ОАМ', day=day, document=day, observations=[lab('Глюкоза', 'отрицательно')]) for day in ('2015-01-01', '2016-01-01')]
        self.assertEqual(table(sheets.build(data), 'Динамика'), [])

    def test_other_unit_is_a_separate_line(self):
        data = [study(day='2015-01-01', document='a', observations=[lab('Гемоглобин', '120', 'г/л')]),
                study(day='2016-01-01', document='b', observations=[lab('Гемоглобин', '12', 'г/дл')])]
        self.assertEqual(table(sheets.build(data), 'Динамика'), [])


FORM = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nДата выполнения исследования: {day}\n'
        'Общий анализ крови\nПоказатель  Результат  Единицы\nГемоглобин  {value}  г/л\n')


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')
        self.target = self.workspace.root / 'Исследования.xlsx'

    def add(self, name, day='02.03.2015', value='120', text=None):
        (self.workspace.root / 'Входящие' / name).write_text(text or FORM.format(day=day, value=value), encoding='utf-8')
        self.workspace.import_folder()

    def book(self):
        return load_workbook(self.target)

    def edit(self, change):
        book = self.book()
        change(book)
        book.save(self.target)

    def row_values(self, sheet):
        rows = list(sheet.iter_rows(values_only=True))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def test_doctor_sheets_come_first(self):
        self.add('a.txt')
        self.assertEqual(self.book().sheetnames[:4], ['Обзор', 'Свод', 'Динамика', 'ОАК'])

    def test_two_years_are_two_rows_and_a_trend(self):
        self.add('a.txt')
        self.add('b.txt', '05.06.2017', '130')
        book = self.book()
        self.assertEqual([row['HGB (г/л)'] for row in self.row_values(book['ОАК'])], [120, 130])
        self.assertEqual(self.row_values(book['Динамика'])[0]['Последнее значение'], 130)

    def test_dates_are_real_dates(self):
        self.add('a.txt')
        self.assertEqual(self.book()['ОАК']['D2'].number_format, 'dd.mm.yyyy')

    def test_notes_survive_the_next_export(self):
        self.add('a.txt')
        def note(book):
            sheet = book['ОАК']
            column = next(cell.column for cell in sheet[1] if cell.value == 'Заметки')
            sheet.cell(2, column, 'Учебная заметка')
        self.edit(note)
        self.add('b.txt', '05.06.2017', '130')
        self.assertEqual(self.row_values(self.book()['ОАК'])[0]['Заметки'], 'Учебная заметка')

    def test_users_own_sheet_with_the_same_name_is_left_alone(self):
        self.add('a.txt')
        def replace(book):
            del book['ОАК']
            book.create_sheet('ОАК')['A1'] = 'Моё'
            for row in book['_Программа'].iter_rows(min_row=10, max_col=2):
                if row[0].value == 'clinical:ОАК':
                    row[0].value = row[1].value = None
        self.edit(replace)
        self.add('b.txt', '05.06.2017', '130')
        result = self.book()
        self.assertEqual(result['ОАК']['A1'].value, 'Моё')
        self.assertEqual(len(self.row_values(result['ОАК (авто)'])), 2)

    def test_a_new_kind_shifting_positions_does_not_break_the_export(self):
        self.add('a.txt')
        text = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nДата выполнения исследования: 05.06.2017\nОбщий анализ мочи\nПоказатель  Результат  Единицы\nГлюкоза  отрицательно\n'
        self.add('b.txt', text=text)
        self.assertLess(self.book().sheetnames.index('ОАК'), self.book().sheetnames.index('ОАМ'))
        self.assertEqual(len(self.row_values(self.book()['ОАК'])), 1)

    def test_second_export_adds_no_sheets(self):
        self.add('a.txt')
        before = self.book().sheetnames
        self.add('b.txt', '05.06.2017', '130')
        self.assertEqual(self.book().sheetnames, before)

    def test_old_column_of_the_program_does_not_linger(self):
        self.add('a.txt', text=FORM.format(day='02.03.2015', value='120') + 'Учебный показатель  7  ктл\n')
        self.assertIn('Учебный показатель (ктл)', [cell.value for cell in self.book()['ОАК'][1]])
        record = self.workspace.quality_review_list()[0]
        rows = [row for row in record['observations'] if row['label'] != 'Учебный показатель']
        self.workspace.confirm_quality(record['id'], observations=rows, note='Учебная сверка')
        self.assertNotIn('Учебный показатель (ктл)', [cell.value for cell in self.book()['ОАК'][1]])

    def test_text_from_a_form_never_becomes_a_formula(self):
        text = FORM.format(day='02.03.2015', value='120') + '=HYPERLINK("x")  5  г\n'
        self.add('a.txt', text=text)
        book = self.book()
        cells = [cell for sheet in book for row in sheet.iter_rows() for cell in row]
        self.assertFalse([cell for cell in cells if cell.data_type == 'f'])

    def test_program_data_sheet_stays_hidden_after_rewrite(self):
        self.add('a.txt')
        self.add('b.txt', '05.06.2017', '130')
        self.assertEqual(self.book()['_Программа'].sheet_state, 'veryHidden')


if __name__ == '__main__':
    unittest.main()
