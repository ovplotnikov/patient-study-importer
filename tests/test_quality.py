import unittest

from patient_importer.analysis import analyze
from patient_importer.quality import assess, issues, needs_review


IDENTITY = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
DATE = 'Дата выполнения исследования: 02.03.2025\n'
LAB = 'Общий анализ крови\nПоказатель  Результат\n'


def parsed(text, **extraction):
    return analyze({'pages': [text], 'ocr_pages': [], **extraction}, 'Учебный бланк.txt')


def codes(result):
    return {issue['code'] for issue in issues(result)}


class QualityTests(unittest.TestCase):
    def test_unknown_and_conflicting_types_are_visible(self):
        result = parsed(IDENTITY + DATE + 'Учебное письмо.')
        self.assertEqual(result['type'], 'Документ')
        self.assertEqual(codes(result), {'unknown_type'})

        result = parsed(IDENTITY + DATE + 'Ультразвуковое исследование почек\n' + LAB)
        self.assertEqual(result['type'], 'Документ')
        self.assertIn('conflicting_type', codes(result))

    def test_heading_wins_over_incidental_mention_and_text_result_is_valid(self):
        result = parsed(IDENTITY + DATE + 'Ультразвуковое исследование почек\n'
                        'Сравнение с пробой Зимницкого.\nЗаключение: Учебный текст.')
        self.assertEqual(result['type'], 'УЗИ')
        self.assertEqual(result['observations'], [])
        self.assertFalse(needs_review(result))
        empty = parsed(IDENTITY + DATE + 'Ультразвуковое исследование почек\nЗаключение:')
        self.assertIn('missing_narrative_result', codes(empty))

    def test_missing_and_conflicting_study_dates_are_not_invented(self):
        result = parsed(IDENTITY + LAB + 'Гемоглобин  120 г/л')
        self.assertIsNone(result['date'])
        self.assertIn('missing_date', codes(result))
        result = parsed(IDENTITY + DATE + DATE.replace('02.03.2025', '03.03.2025') + LAB + 'Гемоглобин  120 г/л')
        self.assertIsNone(result['date'])
        self.assertIn('conflicting_date', codes(result))

    def test_missing_and_unknown_units_allow_unitless_and_qualitative_values(self):
        result = parsed(IDENTITY + DATE + LAB + 'Гемоглобин  120\nКреатинин  100 ктл\n'
                        'pH  7,4\nУчебный тест  не обнаружено')
        self.assertEqual(codes(result), {'missing_unit', 'unknown_unit'})
        self.assertEqual([item['result'] for item in result['observations']], ['120', '100', '7,4', 'не обнаружено'])
        self.assertEqual([item['observation_index'] for item in issues(result)], [0, 1])

    def test_lost_table_columns_do_not_attach_to_previous_row(self):
        result = parsed(IDENTITY + DATE + LAB + 'Гемоглобин  120 г/л\nЛейкоциты 5 10^9/л')
        self.assertEqual([item['label'] for item in result['observations']], ['Гемоглобин'])
        self.assertEqual(codes(result), {'unparsed_row'})
        self.assertEqual(issues(result)[0]['line'], 7)
        empty = parsed(IDENTITY + DATE + LAB.replace('Показатель  Результат', 'показатель результат')
                       + 'Гемоглобин 120 г/л')
        self.assertEqual(codes(empty), {'lab_no_results', 'unparsed_row'})

    def test_column_mapping_keeps_result_distinct_from_reference(self):
        result = parsed(IDENTITY + DATE + 'Показатель  Единицы  Референс  Результат\n'
                        'Гемоглобин  г/л  100–140  120')
        self.assertEqual(result['type'], 'Лабораторное исследование')
        item, = result['observations']
        self.assertEqual((item['number'], item['unit'], item['reference']), (120, 'г/л', '100–140'))
        self.assertFalse(needs_review(result))

    def test_lost_result_gap_does_not_promote_reference_to_result(self):
        result = parsed(IDENTITY + DATE + LAB + 'Гемоглобин 120  100–140\n'
                        'Гемоглобин 121 г/л  100–140\nТ3  3,1 нмоль/л\nВитамин B12  300 пг/мл')
        self.assertEqual([item['label'] for item in result['observations']], ['Т3', 'Витамин B12'])
        self.assertEqual([issue['line'] for issue in issues(result) if issue['code'] == 'unparsed_row'], [6, 7])

    def test_no_scalar_when_multiple_values_are_present(self):
        result = parsed(IDENTITY + DATE + LAB + 'Гемоглобин  120 121 г/л')
        item, = result['observations']
        self.assertEqual(item['result'], '120 121')
        self.assertIsNone(item['number'])
        self.assertIn('ambiguous_value', codes(result))

    def test_narrative_preserves_signed_and_exponential_values(self):
        result = parsed(IDENTITY + DATE + 'УЗИ\n'
                        'Креатинин −8,5 мкмоль/л.\nКреатинин +8,5 мкмоль/л.\n'
                        'Креатинин <=8,5 мкмоль/л.\nКреатинин 8,5e-3 мкмоль/л.\n'
                        'Правая почка 1,2×10^3 мм.')
        self.assertEqual(
            [(row['result'], row['number']) for row in result['observations']],
            [('−8,5', -8.5), ('+8,5', 8.5), ('<=8,5', None), ('8,5e-3', 0.0085), ('1,2×10^3', None)],
        )
        self.assertIn('ambiguous_value', codes(result))

    def test_longer_unit_does_not_fall_back_to_prefix(self):
        result = parsed(IDENTITY + DATE + 'УЗИ\nУчебный показатель 12 мм/ч.')
        self.assertEqual(result['observations'][0]['unit'], 'мм/ч')

    def test_units_and_metadata_do_not_count_as_lab_results(self):
        result = parsed(IDENTITY + DATE + LAB + 'Гемоглобин  г/л\n'
                        'Номер исследования  120\nРеференс  100–140\nДата  02.03.2025')
        self.assertEqual(result['observations'], [])
        self.assertEqual(codes(result), {'lab_no_results', 'unparsed_row'})

    def test_manual_result_counts_as_completed_lab_payload(self):
        result = parsed(IDENTITY + DATE + LAB)
        self.assertIn('lab_no_results', codes(result))
        result['observations'] = [{
            'origin': 'Введено при сверке', 'label': 'Гемоглобин',
            'result': '120', 'number': 120, 'unit': 'г/л',
        }]
        self.assertEqual(assess(result)['issues'], [])

    def test_manual_text_can_complete_a_non_numeric_form(self):
        result = parsed(IDENTITY + DATE + 'УЗИ\nЗаключение:')
        self.assertIn('missing_narrative_result', codes(result))
        result['sections'] = [{'section': 'Заключение', 'text': 'Учебная формулировка.', 'reviewed': True}]
        self.assertEqual(assess(result)['issues'], [])
        result['sections'] = []
        self.assertEqual({item['code'] for item in assess(result)['issues']}, {'missing_narrative_result'})

    def test_ocr_and_extraction_evidence_are_retained(self):
        evidence = [{'page': 4, 'words': []}]
        result = parsed(IDENTITY + DATE + LAB + 'Гемоглобин  120 г/л', ocr_pages=[1], page_numbers=[4],
                        ocr_evidence=evidence, extraction_issues=[{'code': 'blank_scan', 'page': 5, 'message': 'Скан не прочитан.'}])
        self.assertTrue(result['identity_from_ocr'])
        self.assertEqual(result['ocr_evidence'], evidence)
        self.assertEqual({issue['code']: issue['page'] for issue in issues(result)}, {'ocr_value': 4, 'blank_scan': 5})
        result['quality']['confirmed'] = True
        self.assertFalse(needs_review(result))

    def test_legacy_analysis_is_assessed_without_mutation(self):
        result = parsed(IDENTITY + LAB + 'Гемоглобин  120')
        for field in ('quality', 'type_candidates', 'date_candidates', 'table_issues'):
            del result[field]
        result['observations'][0].pop('unit_column')
        self.assertEqual(codes(result), {'missing_date', 'missing_unit'})
        self.assertEqual(assess(result)['issues'], issues(result))
        self.assertNotIn('quality', result)
