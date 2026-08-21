import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_facts import (
    _parse_number,
    _parse_number_pair,
    extract_ixbrl_facts,
    extract_report_text_facts,
)


class InvestmentFactsTest(unittest.TestCase):
    def test_uk_inline_xbrl_zip_selects_annual_current_and_prior(self):
        markup = """
        <xbrli:unit id="GBP"><xbrli:measure>iso4217:GBP</xbrli:measure></xbrli:unit>
        <xbrli:context id="prior"><xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="current"><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="quarter"><xbrli:period><xbrli:startDate>2024-10-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
        <ix:nonFraction name="uk-gaap:Turnover" contextRef="prior" unitRef="GBP" scale="6">100</ix:nonFraction>
        <ix:nonFraction name="uk-gaap:Turnover" contextRef="current" unitRef="GBP" scale="6">120</ix:nonFraction>
        <ix:nonFraction name="custom:Turnover" contextRef="current" unitRef="GBP" scale="6">999</ix:nonFraction>
        <ix:nonFraction name="uk-gaap:Turnover" contextRef="quarter" unitRef="GBP" scale="6">35</ix:nonFraction>
        <ix:nonFraction name="ifrs-full:ProfitLoss" contextRef="prior" unitRef="GBP" scale="6">25</ix:nonFraction>
        """
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as package:
            package.writestr("annual-report.xhtml", markup)
        current, prior, metadata = extract_ixbrl_facts(content.getvalue())
        self.assertEqual(current["revenue"]["value"], 120)
        self.assertEqual(prior["revenue"]["value"], 100)
        self.assertEqual(current["revenue"]["unit"], "GBPm")
        self.assertEqual(current["revenue"]["period"], "2024-12-31")
        self.assertEqual(current["revenue"]["source"], "uk_ixbrl")
        self.assertEqual(current["revenue"]["concept"], "uk-gaap:Turnover")
        self.assertNotIn("net_income", current)
        self.assertEqual(prior["net_income"]["value"], 25)
        self.assertEqual(metadata["extracted_fact_count"], 3)

    def test_layout_text_requires_anchor_and_aligned_columns(self):
        text = """CONSOLIDATED INCOME STATEMENT
£ million 2024 2023
Revenue                         120 100
Gross profit                    60 50
"""
        current, prior, _ = extract_report_text_facts(text)
        self.assertEqual(current["revenue"]["value"], 120)
        self.assertEqual(prior["gross_profit"]["value"], 50)

        ambiguous, _, metadata = extract_report_text_facts("Revenue 120 100")
        self.assertEqual(ambiguous, {})
        self.assertEqual(metadata["status"], "unavailable")

    def test_layout_text_uses_document_date_when_ocr_damages_year_header(self):
        text = """CONSOLIDATED INCOME STATEMENT
for the year ended 31 December
US$ million
Revenue                         120 100
Gross profit                     60  50
Total liabilities                100 1000
Total equity                     400 100
Current assets                   Treasury shares 28 0
"""
        current, prior, metadata = extract_report_text_facts(text, "2025-12-31")
        self.assertEqual(current["revenue"]["value"], 120)
        self.assertEqual(prior["gross_profit"]["value"], 50)
        self.assertEqual(metadata["periods"], ["2025", "2024"])
        self.assertEqual(metadata["period_source"], "document_report_date")
        self.assertNotIn("total_liabilities", current)
        self.assertNotIn("equity", current)

        self.assertNotIn("current_assets", current)

    def test_layout_text_selects_explicit_total_columns_and_ignores_future_ocr_year(
        self,
    ):
        text = """CONSOLIDATED INCOME STATEMENT
for the year ended 31 December 2025
2028                                                    2024
US$ million       Note      Before special   Special   Total      Before special   Special   Total
Revenue              2             18,533        13   18,546              17,809      (64)   17,745
CONSOLIDATED BALANCE SHEET
as at 31 December 2025
US$ million                          Note       2025       2024
Total assets                                  55,994     64,866
Total liabilities                            (31,877)   (36,333)
Total equity                                  24,117     28,533
CONSOLIDATED CASH FLOW STATEMENT
for the year ended 31 December 2025
US$ million                          Note       2025       2024
Expenditure on property, plant and equipment    14      (3,340)    (3,974)
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertEqual(metadata["periods"], ["2025", "2024"])
        self.assertEqual(current["revenue"]["value"], 18_546)
        self.assertEqual(prior["revenue"]["value"], 17_745)
        self.assertEqual(current["total_liabilities"]["value"], 31_877)
        self.assertEqual(current["capex"]["value"], 3_340)

    def test_layout_text_preserves_unknown_report_currency_for_total_columns(self):
        text = """CONSOLIDATED INCOME STATEMENT
Year ended 30 September 2025
2025                                           2024
Headline                 Total Headline                 Total
Notes million million million million million million
Total revenue       81 10,106 = 10,106 | 9,309 = 9,309
Profit for the year    499 x 494 | 459 7 452
"""

        current, prior, metadata = extract_report_text_facts(text)

        self.assertEqual(metadata["status"], "success")
        self.assertEqual(current["revenue"]["value"], 10_106)
        self.assertEqual(prior["net_income"]["value"], 452)
        self.assertEqual(current["revenue"]["unit"], "report_millions")

    def test_parse_number_rejects_truncated_thousands_group(self):
        self.assertIsNone(_parse_number("1,30"))
        self.assertEqual(_parse_number("2,200"), 2_200.0)
        self.assertEqual(_parse_number("(3,340)"), -3_340.0)
        self.assertEqual(_parse_number("2,071.3"), 2_071.3)
        self.assertEqual(_parse_number("3.870"), 3.87)

    def test_parse_number_pair_recovers_ocr_separator_loss(self):
        # GSK-style: a comma misread as a dot is recovered from the aligned peer.
        self.assertEqual(_parse_number_pair("3,397", "3.870"), (3_397.0, 3_870.0))
        # Antofagasta-style: separator loss plus trailing OCR padding.
        self.assertEqual(_parse_number_pair("6,184.5", "5.66390"), (6_184.5, 5_663.9))
        # St James-style: valid aligned decimals are preserved untouched.
        self.assertEqual(_parse_number_pair("2,071.3", "1,316.0"), (2_071.3, 1_316.0))
        self.assertEqual(_parse_number_pair("12.5", "10.0"), (12.5, 10.0))
        # Negatives survive recovery.
        self.assertEqual(_parse_number_pair("(3,397)", "(3.870)"), (-3_397.0, -3_870.0))
        # Antofagasta-style: a pure integer peer that lost both separators
        # ('1,316.0' -> '13160') is recovered from the decimal-precision peer.
        self.assertEqual(_parse_number_pair("2,071.3", "13160"), (2_071.3, 1_316.0))
        # A small ungrouped decimal peer never triggers integer recovery:
        # '25' next to '2.5' stays a genuine large value, not a flattened 2.5.
        self.assertEqual(_parse_number_pair("2.5", "25"), (2.5, 25.0))
        # Diageo-style truncation is irrecoverable: fail closed.
        self.assertIsNone(_parse_number_pair("2,200", "1,30"))
        # A genuine implausible decimal pair is ambiguous, never fabricated:
        # a misplaced decimal point is only thousands when the aligned peer
        # formatting and magnitude both agree on exactly one reading.
        self.assertIsNone(_parse_number_pair("0.025", "2.500"))
        self.assertIsNone(_parse_number_pair("1.234", "5.5"))

    def test_layout_text_recovers_antofagasta_style_separator_loss(self):
        text = """CONSOLIDATED INCOME STATEMENT
US$ million 2024 2023
Revenue                         6,184.5     5.66390
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(current["revenue"]["value"], 6_184.5)
        self.assertEqual(prior["revenue"]["value"], 5_663.9)
        self.assertEqual(current["revenue"]["unit"], "USDm")

    def test_layout_text_recovers_gsk_style_separator_loss(self):
        text = """CONSOLIDATED INCOME STATEMENT
£ million 2024 2023
Profit for the year             3,397       3.870
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(current["net_income"]["value"], 3_397)
        self.assertEqual(prior["net_income"]["value"], 3_870)
        self.assertEqual(current["net_income"]["unit"], "GBPm")

    def test_layout_text_preserves_st_james_style_decimals(self):
        text = """CONSOLIDATED INCOME STATEMENT
£ million 2024 2023
Revenue                         2,071.3     1,316.0
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(current["revenue"]["value"], 2_071.3)
        self.assertEqual(prior["revenue"]["value"], 1_316.0)
        self.assertEqual(current["revenue"]["unit"], "GBPm")

    def test_layout_text_recovers_separator_free_integer_peer(self):
        text = """CONSOLIDATED INCOME STATEMENT
£ million 2024 2023
Revenue                         2,071.3 | 13160
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(current["revenue"]["value"], 2_071.3)
        self.assertEqual(prior["revenue"]["value"], 1_316.0)
        self.assertEqual(current["revenue"]["unit"], "GBPm")

    def test_layout_text_omits_irrecoverable_truncated_comparative_row(self):
        text = """CONSOLIDATED INCOME STATEMENT
£ million 2024 2023
Revenue                         2,200 | 1,30
Gross profit                    1,100       700
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertNotIn("revenue", current)
        self.assertNotIn("revenue", prior)
        self.assertEqual(current["gross_profit"]["value"], 1_100)
        self.assertEqual(prior["gross_profit"]["value"], 700)


if __name__ == "__main__":
    unittest.main()
