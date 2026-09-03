import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_facts import (
    _extract_external_effect_facts,
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
Net cash from operating activities              13       6,000      5,500
Expenditure on property, plant and equipment    14      (3,340)    (3,974)
"""
        current, prior, metadata = extract_report_text_facts(text)
        self.assertEqual(metadata["periods"], ["2025", "2024"])
        self.assertEqual(current["revenue"]["value"], 18_546)
        self.assertEqual(prior["revenue"]["value"], 17_745)
        self.assertEqual(current["total_liabilities"]["value"], 31_877)
        self.assertEqual(current["capex"]["value"], 3_340)
        self.assertEqual(current["operating_cash_flow"]["value"], 6_000)
        self.assertEqual(current["free_cash_flow"]["value"], 2_660)
        for metric, family, cash_basis in (
            ("revenue", "revenue", "not_applicable"),
            ("operating_cash_flow", "operating_cash_flow", "cash"),
            ("capex", "capital_investment", "cash"),
            ("free_cash_flow", "free_cash_flow", "cash"),
        ):
            with self.subTest(metric=metric):
                tags = current[metric]["relationship_tags"]
                self.assertEqual(tags["leaf"], "standard_metric")
                self.assertEqual(tags["metric_family"], family)
                self.assertEqual(tags["scope"], "consolidated")
                self.assertEqual(tags["comparison_basis"], "none")
                self.assertEqual(tags["temporal_basis"], "period_flow")
                self.assertEqual(tags["cash_basis"], cash_basis)
        self.assertNotIn("lease_inclusive_investment", current)

    def test_layout_text_tags_cash_and_broader_investment_bases_generically(self):
        text = """CONSOLIDATED CASH FLOW STATEMENT
for the year ended 31 December 2025
US$ million                          Note       2025       2024
Net cash from operating activities              10      8,000      7,000
Purchase of property, plant and equipment        11     (2,500)    (2,000)
Capital expenditures including finance lease additions  12      3,100      2,600
"""

        current, prior, metadata = extract_report_text_facts(text)

        self.assertEqual(metadata["status"], "success")
        self.assertEqual(current["operating_cash_flow"]["value"], 8_000)
        self.assertEqual(current["capex"]["value"], 2_500)
        self.assertEqual(current["lease_inclusive_investment"]["value"], 3_100)
        self.assertEqual(prior["capex"]["value"], 2_000)
        self.assertEqual(prior["lease_inclusive_investment"]["value"], 2_600)
        cash_tags = current["capex"]["relationship_tags"]
        broader_tags = current["lease_inclusive_investment"]["relationship_tags"]
        self.assertEqual(cash_tags["metric_family"], "capital_investment")
        self.assertEqual(broader_tags["metric_family"], "capital_investment")
        self.assertEqual(cash_tags["cash_basis"], "cash")
        self.assertEqual(
            broader_tags["cash_basis"], "cash_plus_finance_leases"
        )
        self.assertEqual(cash_tags["scope"], broader_tags["scope"])
        self.assertEqual(cash_tags["temporal_basis"], "period_flow")
        self.assertEqual(broader_tags["temporal_basis"], "period_flow")
        self.assertEqual(current["free_cash_flow"]["value"], 5_500)
        self.assertEqual(
            current["free_cash_flow"]["concept"],
            "derived:operating_cash_flow-capex",
        )
        self.assertNotIn(
            "finance lease",
            current["free_cash_flow"]["evidence"].lower(),
        )
        self.assertEqual(
            set(current),
            {
                "operating_cash_flow",
                "capex",
                "lease_inclusive_investment",
                "free_cash_flow",
            },
        )
        self.assertFalse(
            any(
                fact.get("source") == "derived"
                for name, fact in current.items()
                if name != "free_cash_flow"
            )
        )

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


    def test_external_effect_extraction_accepts_generic_contribution_and_drag(self):
        current = {
            "revenue": {
                "period": "2025",
                "relationship_tags": {
                    "scope": "consolidated",
                    "duration_days": 365,
                },
            },
            "net_income": {
                "period": "2025",
                "relationship_tags": {
                    "scope": "consolidated",
                    "duration_days": 365,
                },
            },
        }
        text = (
            "For the year ended 2025, a neutral event contributed 2 percentage "
            "points to revenue year-over-year growth due to a business combination. "
            "For the year ended 2025, a separate neutral event was a 1 point drag "
            "on net income year-over-year growth due to restructuring."
        )
        effects = _extract_external_effect_facts(text, current, ["2025", "2024"])

        self.assertEqual(len(effects), 2)
        contribution, drag = effects.values()
        self.assertEqual(contribution["value"], 2.0)
        self.assertEqual(drag["value"], -1.0)
        self.assertEqual(contribution["unit"], "percentage_points")
        self.assertEqual(drag["unit"], "percentage_points")
        self.assertEqual(contribution["period"], "2025")
        self.assertEqual(drag["period"], "2025")
        self.assertEqual(contribution["relationship_tags"]["duration_days"], 365)
        self.assertEqual(drag["relationship_tags"]["duration_days"], 365)
        self.assertEqual(
            contribution["relationship_tags"]["effect_kind"], "contribution"
        )
        self.assertEqual(drag["relationship_tags"]["effect_kind"], "drag")
        self.assertEqual(
            contribution["relationship_tags"]["category"], "business_combination"
        )
        self.assertEqual(drag["relationship_tags"]["category"], "restructuring")
        self.assertEqual(
            contribution["relationship_tags"]["recipient_path"], "current.revenue"
        )
        self.assertEqual(
            drag["relationship_tags"]["recipient_path"], "current.net_income"
        )
        self.assertEqual(
            contribution["relationship_tags"]["comparison_basis"],
            "year_over_year_gaap",
        )
        self.assertEqual(
            contribution["relationship_tags"]["temporal_basis"], "rate_over_period"
        )
        self.assertEqual(
            contribution["relationship_tags"]["compatibility"], "compatible"
        )

        period_silent = _extract_external_effect_facts(
            "A neutral event contributed 2 percentage points to revenue "
            "year-over-year growth due to a business combination.",
            current,
            ["2025", "2024"],
        )
        silent_tags = next(iter(period_silent.values()))["relationship_tags"]
        self.assertEqual(silent_tags["compatibility"], "incompatible")
        self.assertIn("period_mismatch", silent_tags["incompatibility_reasons"])

    def test_external_reclassification_groups_both_explicit_recipient_legs(self):
        current = {
            metric: {
                "period": "2025",
                "relationship_tags": {"scope": "consolidated"},
            }
            for metric in ("revenue", "operating_income")
        }

        effects = _extract_external_effect_facts(
            "$5 million was reclassified from revenue to operating income "
            "in 2025.",
            current,
            ["2025", "2024"],
        )

        self.assertEqual(len(effects), 2)
        from_leg, to_leg = effects.values()
        self.assertEqual((from_leg["value"], to_leg["value"]), (-5.0, 5.0))
        from_tags = from_leg["relationship_tags"]
        to_tags = to_leg["relationship_tags"]
        self.assertEqual(from_tags["group_id"], to_tags["group_id"])
        self.assertEqual(from_tags["effect_kind"], "reclassification")
        self.assertEqual(to_tags["effect_kind"], "reclassification")
        self.assertEqual(from_tags["recipient_path"], "current.revenue")
        self.assertEqual(to_tags["recipient_path"], "current.operating_income")
        self.assertIn("includes_reclassification", from_tags["qualifiers"])
        self.assertIn("includes_reclassification", to_tags["qualifiers"])

    def test_external_effect_with_absent_exact_recipient_is_incompatible(self):
        effects = _extract_external_effect_facts(
            "On a consolidated basis, a neutral event contributed 2 percentage "
            "points to revenue year-over-year growth in 2025.",
            {},
            ["2025", "2024"],
        )

        self.assertEqual(len(effects), 1)
        tags = effects["external_effect_1"]["relationship_tags"]
        self.assertIsNone(tags["recipient_path"])
        self.assertEqual(tags["compatibility"], "incompatible")
        self.assertEqual(tags["incompatibility_reasons"], ["unresolved_recipient"])

    def test_external_effect_ignores_names_without_explicit_quantified_grammar(self):
        effects = _extract_external_effect_facts(
            "The acquisition contribution improved results and a currency "
            "headwind affected growth.",
            {
                "revenue": {
                    "period": "2025",
                    "relationship_tags": {"scope": "consolidated"},
                }
            },
            ["2025", "2024"],
        )
        self.assertEqual(effects, {})

if __name__ == "__main__":
    unittest.main()
