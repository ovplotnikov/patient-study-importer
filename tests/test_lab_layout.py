"""Laboratory tables as PDF layout text: fictional values, realistic geometry."""
import unittest

from patient_importer.analysis import analyze
from patient_importer.parsing import laboratory_header, laboratory_rows
from patient_importer.quality import issues

HEADER = (' ' * 26 + 'Показатель' + ' ' * 50 + 'Результат  Ед. изм.  Реф-ые значения'
          + ' ' * 11 + 'Комментарий     Тест-система')
MERGED_HEADER = HEADER.replace('Ед. изм.  Реф-ые', 'Ед. изм. Реф-ые')
FULL_COLUMNS = ['label', 'result', 'unit', 'reference', 'comment', 'test_system']
IDENTITY = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nДата выполнения исследования: 02.03.2025\n'


def row(label, value='', unit='', reference='', flag=''):
    left = label.ljust(62) + (flag.ljust(24) if flag else ' ' * 24)
    return (left + value.rjust(6) + ('    ' + unit if unit else '')
            + ('      ' + reference if reference else '')).rstrip()


def table(*lines, header=HEADER):
    return laboratory_rows(['\n'.join([header, *lines])])


class LayoutHeaderTests(unittest.TestCase):
    def test_full_header_maps_every_column(self):
        self.assertEqual(laboratory_header(HEADER), FULL_COLUMNS)

    def test_cells_merged_in_layout_text_are_split(self):
        self.assertEqual(laboratory_header(MERGED_HEADER), FULL_COLUMNS)

    def test_three_cells_merged_into_one(self):
        header = 'Показатель' + ' ' * 40 + 'Результат Ед. изм. Реф-ые значения    Комментарий    Тест-система'
        self.assertEqual(laboratory_header(header), FULL_COLUMNS)

    def test_cells_merged_without_a_space(self):
        header = 'Показатель  Результат  Ед. изм.Реф-ые значения  Комментарий  Тест-система'
        self.assertEqual(laboratory_header(header), FULL_COLUMNS)

    def test_leading_deviation_column_and_interval_header(self):
        self.assertEqual(laboratory_header('Откл.  Показатель  Результат  Ед.изм.  Реф.интервал'),
                         ['flag', 'label', 'result', 'unit', 'reference'])

    def test_column_named_by_a_date_is_history(self):
        self.assertEqual(laboratory_header('Показатель  Результат  Ед.изм.  Реф.интервал  25.10.2020')[-1], 'history')

    def test_unknown_column_stays_unmapped(self):
        self.assertEqual(laboratory_header('Показатель  Результат  Метод'), ['label', 'result', ''])


class SparseRowTests(unittest.TestCase):
    def one(self, *lines, **kwargs):
        rows, pending = table(*lines, **kwargs)
        self.assertEqual(pending, [])
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_empty_right_columns_do_not_reject_the_row(self):
        item = self.one(row('Гемоглобин (HGB)', '120', 'г/л', '(110 - 140)'))
        self.assertEqual((item['label'], item['result'], item['number'], item['unit'], item['reference']),
                         ('Гемоглобин (HGB)', '120', 120, 'г/л', '(110 - 140)'))
        self.assertFalse(item['unmapped_columns'])

    def test_merged_header_reads_the_same_row(self):
        item = self.one(row('Гемоглобин', '120', 'г/л', '(110 - 140)'), header=MERGED_HEADER)
        self.assertEqual((item['number'], item['unit']), (120, 'г/л'))

    def test_standalone_flag_is_not_a_cell(self):
        item = self.one(row('Глюкоза', '7,5', 'ммоль/л', '(3,3 - 5,5)', flag='▲'))
        self.assertEqual((item['result'], item['number'], item['unit'], item['flag']), ('7,5', 7.5, 'ммоль/л', '▲'))

    def test_reference_without_unit_stays_reference(self):
        item = self.one(row('Удельный вес', '1,015', '', '(1,003 - 1,030)'))
        self.assertEqual((item['unit'], item['reference']), ('', '(1,003 - 1,030)'))

    def test_semi_quantitative_and_text_results(self):
        rows, pending = table(row('Белок', '+++'), row('Глюкоза', '-'), row('Эритроциты', '1-3'),
                              row('Цвет', 'желтый'))
        self.assertEqual(pending, [])
        self.assertEqual([r['result'] for r in rows], ['+++', '-', '1-3', 'желтый'])
        self.assertEqual([r['number'] for r in rows], [None, None, None, None])

    def test_unit_glued_to_reference_is_split(self):
        item = self.one(row('Лейкоциты', 'не обнаружено', '1/поле зр.(0-3)'))
        self.assertEqual((item['result'], item['unit'], item['reference']),
                         ('не обнаружено', '1/поле зр.', '(0-3)'))

    def test_filled_comment_column(self):
        item = self.one(row('Креатинин', '40', 'мкмоль/л', '(27 - 62)') + '      контроль')
        self.assertEqual((item['reference'], item['comment']), ('(27 - 62)', 'контроль'))

    def test_inline_unit_conflicting_with_unit_cell_goes_to_review(self):
        rows, pending = table(row('Креатинин', '40 мкмоль/л', 'ммоль/л'))
        self.assertEqual(rows, [])
        self.assertEqual(len(pending), 1)

    def test_second_number_in_unit_position_goes_to_review(self):
        rows, pending = table(row('Креатинин', '40', '41', '(27 - 62)'))
        self.assertEqual(rows, [])
        self.assertEqual(len(pending), 1)

    def test_lost_gap_never_promotes_unit_to_result(self):
        rows, pending = table('Гемоглобин 120' + ' ' * 40 + 'г/л      (110 - 140)')
        self.assertEqual(rows, [])
        self.assertEqual(len(pending), 1)


class DeviationColumnTests(unittest.TestCase):
    HEADER = 'Откл.   Показатель' + ' ' * 40 + 'Результат   Ед.изм.   Реф.интервал'

    def test_rows_with_and_without_a_deviation_arrow(self):
        rows, pending = laboratory_rows(['\n'.join([
            self.HEADER,
            ' ' * 14 + 'Калий в сыворотке' + ' ' * 40 + '4.50' + ' ' * 12 + 'ммоль/л' + ' ' * 8 + '(3.50 - 5.10)',
            ' 🡱🡱🡱' + ' ' * 10 + 'Лейкоциты (WBC)' + ' ' * 40 + '19.9' + ' ' * 12 + '10^9/л' + ' ' * 8 + '(4.5 - 9.0)',
        ])])
        self.assertEqual(pending, [])
        self.assertEqual([(r['label'], r['number'], r['unit'], r['flag']) for r in rows],
                         [('Калий в сыворотке', 4.5, 'ммоль/л', ''), ('Лейкоциты (WBC)', 19.9, '10^9/л', '🡱🡱🡱')])

    def test_earlier_result_in_date_column_is_never_the_result(self):
        header = self.HEADER + '   25.10.2020'
        rows, pending = laboratory_rows(['\n'.join([
            header,
            ' ' * 10 + 'Калий в сыворотке' + ' ' * 31 + '4.50' + ' ' * 11 + 'ммоль/л' + ' ' * 6 + '(3.50 - 5.10)' + ' ' * 7 + '3,90',
        ])])
        self.assertEqual(pending, [])
        self.assertEqual((rows[0]['number'], rows[0]['reference'], rows[0]['comment']), (4.5, '(3.50 - 5.10)', '3,90'))

    def test_three_column_table_with_deviation_column(self):
        rows, pending = laboratory_rows(['Откл.   Показатель      Результат\n' + ' ' * 10 + 'Эритроциты, в поле' + ' ' * 8 + '1-3'])
        self.assertEqual(pending, [])
        self.assertEqual((rows[0]['label'], rows[0]['result']), ('Эритроциты, в поле', '1-3'))


class WrappedCellTests(unittest.TestCase):
    def test_wrapped_text_reference(self):
        rows, pending = table(row('Глюкоза', 'норма', '', '(отрица-'), ' ' * 110 + 'тельно)',
                              row('Белок', 'норма', '', '((не обнаружено,'), ' ' * 110 + 'следы))')
        self.assertEqual(pending, [])
        self.assertEqual([r['reference'] for r in rows], ['(отрицательно)', '((не обнаружено, следы))'])

    def test_wrapped_text_result(self):
        rows, pending = table(row('Кетоны', 'отрица-'), ' ' * 78 + 'тельно', row('Белок', '-'))
        self.assertEqual(pending, [])
        self.assertEqual([r['result'] for r in rows], ['отрицательно', '-'])
        self.assertEqual(rows[1]['section'], '')

    def test_symbolic_result_ending_in_dash_is_not_a_wrapped_word(self):
        rows, pending = table(row('Белок в моче', '+-'), 'суточной', row('Глюкоза', '-'))
        self.assertEqual(pending, [])
        self.assertEqual([(r['label'], r['result']) for r in rows], [('Белок в моче суточной', '+-'), ('Глюкоза', '-')])

    def test_two_word_result_wrapped_with_its_label(self):
        rows, pending = table(row('Плоский эпителий в', 'не', 'кл/мкл'), row('моче', 'обнаружено'), row('Глюкоза', '-'))
        self.assertEqual(pending, [])
        self.assertEqual([(r['label'], r['result'], r['unit']) for r in rows],
                         [('Плоский эпителий в моче', 'не обнаружено', 'кл/мкл'), ('Глюкоза', '-', '')])

    def test_dash_result_does_not_absorb_the_next_title(self):
        rows, pending = table(row('Белок', '-'), ' ' * 57 + 'Клинические исследования', row('Глюкоза', '-'))
        self.assertEqual(pending, [])
        self.assertEqual([r['result'] for r in rows], ['-', '-'])
        self.assertEqual(rows[1]['section'], 'Клинические исследования')


class ContinuationTests(unittest.TestCase):
    def test_wrapped_label_joins_the_row_above(self):
        rows, pending = table(row('Среднее содержание гемоглобина в', '28', 'пг', '(26 - 34)'), 'эритроците (MCH)',
                              row('Гематокрит', '40', '%', '(35 - 45)'))
        self.assertEqual(pending, [])
        self.assertEqual([r['label'] for r in rows],
                         ['Среднее содержание гемоглобина в эритроците (MCH)', 'Гематокрит'])
        self.assertIn('эритроците (MCH)', rows[0]['raw'])

    def test_line_with_a_number_is_not_a_continuation(self):
        rows, pending = table(row('Гемоглобин', '120', 'г/л', '(110 - 140)'), 'Лейкоциты 5 10^9/л')
        self.assertEqual([r['label'] for r in rows], ['Гемоглобин'])
        self.assertEqual([p[2] for p in pending], ['Лейкоциты 5 10^9/л'])

    def test_line_ending_with_colon_is_not_a_continuation(self):
        rows, pending = table(row('Гемоглобин', '120', 'г/л', '(110 - 140)'), 'Примечание врача:')
        self.assertEqual(rows[0]['label'], 'Гемоглобин')
        self.assertEqual(len(pending), 1)

    def test_label_does_not_continue_across_a_blank_line(self):
        rows, pending = table(row('Гемоглобин', '120', 'г/л', '(110 - 140)'), '', 'эритроцитов')
        self.assertEqual(rows[0]['label'], 'Гемоглобин')
        self.assertEqual(len(pending), 1)

    def test_two_column_tables_keep_strict_review(self):
        rows, pending = laboratory_rows(['Показатель  Результат\nГемоглобин  120 г/л\nэритроцитов'])
        self.assertEqual(rows[0]['label'], 'Гемоглобин')
        self.assertEqual(len(pending), 1)

    def test_deep_indented_title_becomes_section_of_following_rows(self):
        rows, pending = table(' ' * 57 + 'Клинические исследования',
                              row('Гемоглобин', '120', 'г/л', '(110 - 140)'))
        self.assertEqual(pending, [])
        self.assertEqual(rows[0]['section'], 'Клинические исследования')

    def test_long_centered_title_between_rows_is_a_section_not_a_label(self):
        title = 'Общий анализ мочи на автоматическом анализаторе (без микроскопии осадка)'
        rows, pending = table(row('Гемоглобин', '120', 'г/л', '(110 - 140)'), ' ' * 19 + title,
                              row('Белок', '-'))
        self.assertEqual(pending, [])
        self.assertEqual([r['label'] for r in rows], ['Гемоглобин', 'Белок'])
        self.assertEqual(rows[1]['section'], title)

    def test_first_line_under_the_header_is_a_title_at_any_margin(self):
        rows, pending = table(' ' * 19 + 'Клинические исследования', row('Белок', '-'))
        self.assertEqual(pending, [])
        self.assertEqual(rows[0]['section'], 'Клинические исследования')

    def test_comment_column_wrap_is_not_a_section(self):
        position = HEADER.index('Комментарий')
        rows, pending = table(row('Гемоглобин', '120', 'г/л', '(110 - 140)'), ' ' * position + 'анализатор модели',
                              row('Гематокрит', '40', '%', '(35 - 45)'))
        self.assertEqual(pending, [])
        self.assertEqual([r['section'] for r in rows], ['', ''])
        self.assertEqual(rows[0]['comment'], 'анализатор модели')


class DocumentLevelTests(unittest.TestCase):
    def test_full_form_produces_results_without_no_result_issue(self):
        text = IDENTITY + '\n'.join([HEADER, row('Гемоглобин', '120', 'г/л', '(110 - 140)'),
                                     row('Гематокрит', '40', '%', '(35 - 45)')])
        result = analyze({'pages': [text], 'ocr_pages': []}, 'Учебный бланк.pdf')
        self.assertEqual([item['number'] for item in result['observations']], [120, 40])
        self.assertEqual({item['origin'] for item in result['observations']}, {'Лабораторный бланк'})
        codes = {issue['code'] for issue in issues(result)}
        self.assertNotIn('lab_no_results', codes)
        self.assertNotIn('unmapped_columns', codes)
        self.assertNotIn('unparsed_row', codes)

    def test_title_printed_right_after_the_header_sets_the_study_type(self):
        text = IDENTITY + '\n'.join([HEADER, ' ' * 57 + 'Общий анализ крови',
                                     row('Гемоглобин', '120', 'г/л', '(110 - 140)')])
        result = analyze({'pages': [text], 'ocr_pages': []}, 'Учебный бланк.pdf')
        self.assertEqual(result['type'], 'ОАК')

    def test_long_title_at_a_small_margin_sets_the_study_type(self):
        text = IDENTITY + '\n'.join([HEADER, ' ' * 19 + 'Общий анализ мочи на анализаторе',
                                     row('Белок', '-')])
        result = analyze({'pages': [text], 'ocr_pages': []}, 'Учебный бланк.pdf')
        self.assertEqual(result['type'], 'ОАМ')

    def test_analyte_named_in_a_row_does_not_set_the_study_type(self):
        text = IDENTITY + '\n'.join([HEADER, row('Гемоглобин', '120', 'г/л', '(110 - 140)'),
                                     ' ' * 57 + 'Общий анализ мочи'])
        result = analyze({'pages': [text], 'ocr_pages': []}, 'Учебный бланк.pdf')
        self.assertEqual(result['type'], 'Лабораторное исследование')


if __name__ == '__main__':
    unittest.main()
