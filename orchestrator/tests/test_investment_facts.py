import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_facts import extract_ixbrl_facts, extract_report_text_facts


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


if __name__ == "__main__":
    unittest.main()
