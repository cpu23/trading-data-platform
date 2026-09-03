"""Shared support fixtures and helpers for investment service tests."""

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import investment_service as service


@contextmanager
def session_context(session):
    yield session


def metric(value, unit="USDm", period="FY2025", evidence="report evidence"):
    return {"value": value, "unit": unit, "period": period, "evidence": evidence}


def sec_index_page(*rows):
    """Build an EDGAR ``*-index.htm`` Document Format Files table body.

    ``rows`` are ``(document_name, doc_type)`` pairs.
    """
    body = "".join(
        f"<tr><td>{index}</td><td>{doc_type}</td>"
        f"<td><a href='/Archives/edgar/data/x/{name}'>{name}</a></td>"
        f"<td>{doc_type}</td><td>1000</td></tr>"
        for index, (name, doc_type) in enumerate(rows, start=1)
    )
    return (
        "<html><body><table class='tableFile' summary='Document Format Files'>"
        "<tr><th scope='col'>Seq</th><th scope='col'>Description</th>"
        "<th scope='col'>Document</th><th scope='col'>Type</th>"
        "<th scope='col'>Size</th></tr>" + body + "</table></body></html>"
    )


def sec_directory_fake_request(index_response, index_page_response, primary_response):
    """Route SEC recovery requests: index.json, then ``*-index.htm``, then the
    selected primary document."""

    def route(method, url, **kwargs):
        if url.endswith("index.json"):
            return index_response
        if "-index.htm" in url or "-index.html" in url:
            return index_page_response
        return primary_response

    return route


def investment_report_payload():
    """Build one schema-valid investment-report model payload."""
    qualitative = {
        name: {"present": False, "strength": "none", "evidence": ""}
        for name in service.QUALITATIVE_NAMES
    }
    qualitative["ai_demand"] = {
        "present": True,
        "strength": "strong",
        "evidence": "Demand remained durable",
    }
    return {
        "classification": {
            "document_type": "annual report",
            "sector": "Technology",
            "industry": "Semiconductors",
            "region": "US",
            "confidence": "high",
        },
        "qualitative": qualitative,
        "summary": "Revenue rose while demand held.",
        "thesis": "Thesis stands unless orders reverse.",
        "counter_thesis": "Orders could reverse and invalidate the thesis.",
        "materiality_assessment": {
            topic: {
                "status": "not_disclosed",
                "observation": "",
                "implication": "",
                "evidence": "",
            }
            for topic in (
                "forward_guidance",
                "reported_variance_driver",
                "margin_economics",
                "capital_commitment_duration",
            )
        },
        "drivers": [],
        "catalysts": [],
        "risks": [],
        "relationship_reconciliations": [],
        "watch_items": [],
        "numeric_claims": [],
    }


def epistemic_risk():
    return {
        "sourced_observation": "Demand remained durable",
        "inference": "Order volume may remain resilient",
        "epistemic_state": "supported",
        "uncertainty": "The duration of demand is uncertain",
        "likelihood": "medium",
        "impact": "high",
        "mitigation": "Monitor order conversion",
        "evidence": "Demand remained durable",
    }


def epistemic_catalyst():
    return {
        "trigger": "Demand remained durable",
        "expected_outcome": "Order volume may remain resilient",
        "horizon": "within the next year",
        "epistemic_state": "supported",
        "uncertainty": "The duration of demand is uncertain",
        "evidence": "Demand remained durable",
    }


def relationship_metric(value, *, role, metric_family, cash_basis):
    return {
        "value": value,
        "unit": "usd_millions",
        "currency": "USD",
        "period": "FY2025",
        "evidence": ["Neutral filing disclosure"],
        "source": "reported",
        "relationship_tags": {
            "role": role,
            "metric_family": metric_family,
            "leaf": "standard_metric",
            "scope": "consolidated",
            "comparison_basis": "none",
            "temporal_basis": "period_flow",
            "cash_basis": cash_basis,
        },
    }



class NumericClaimLedgerTestBase(unittest.TestCase):
    """Shared base class providing helper methods for numeric claim ledger tests."""

    def _row(self, **overrides):
        row = {
            "claim_id": "capex_fy24q4",
            "path": "summary",
            "value": "$19B",
            "metric": "capital expenditures including finance leases",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "text",
            "quote": (
                "Capital expenditures including finance leases were $19 "
                "billion, in line with expectations"
            ),
        }
        row.update(overrides)
        return row


    def _payload(self, rows):
        payload = investment_report_payload()
        payload["summary"] = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        if rows is not None:
            payload["numeric_claims"] = rows
        return payload


    def _aggregation_payload(self, *, corrected):
        row = self._row(
            claim_id=(
                "capex-grounded"
                if corrected
                else "RAW_PRIVATE_CLAIM_IDENTIFIER"
            ),
            path="summary" if corrected else "drivers[99]",
            value="$19B" if corrected else {"raw": "RAW_PRIVATE_VALUE"},
            source_kind="fact",
            fact_path=(
                "deterministic_current.capital_expenditures.value"
                if corrected
                else "deterministic_current.raw_private_missing_metric.value"
            ),
        )
        del row["quote"]
        payload = self._payload([row])
        payload["summary"] = (
            "Capital expenditures were $19B in FY2024 Q4 while demand "
            "remained durable."
        )
        payload["classification"]["confidence"] = "high"
        payload["qualitative"]["pricing_power"] = {
            "present": True,
            "strength": "strong",
            "evidence": (
                "Demand remained durable"
                if corrected
                else "RAW_PRIVATE_UNGROUNDED_EVIDENCE"
            ),
        }
        return payload


    @contextmanager
    def _live_aggregation_harness(
        self,
        responses,
        *,
        deterministic_current=None,
        excerpt=None,
    ):
        document_id = "77777777-7777-7777-7777-777777777777"
        analysis_id = "88888888-8888-8888-8888-888888888888"
        excerpt = excerpt or (
            "Capital expenditures were $19B in FY2024 Q4. Demand remained "
            "durable through the period."
        )
        document = {
            "document_id": document_id,
            "company": "Example Co",
            "symbol": "EX",
            "region": "US",
            "industry": "Technology",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": excerpt,
        }
        deterministic_current = (
            deterministic_current
            if deterministic_current is not None
            else {
                "capital_expenditures": {
                    "value": 19.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                }
            }
        )
        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (document_id,)
        persist_or_failure_session = MagicMock()
        insert_result = MagicMock()
        insert_result.fetchone.return_value = (analysis_id,)
        persist_or_failure_session.execute.side_effect = [
            insert_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]
        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna",
            validation_retries=1,
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=100,
            tokens_output_total=50,
            cost_usd_total=0.001,
            first_attempt_duration_ms=10,
            validation_retry_duration_ms=10,
            validation_warnings=[],
        )
        stage.call.side_effect = [
            {"content": json.dumps(response)} for response in responses
        ]
        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(persist_or_failure_session),
                ],
            ),
            patch.object(
                service, "_ensure_extracted_text", return_value="stored_document"
            ),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(
                service,
                "load_deterministic_facts",
                return_value=(deterministic_current, {}, {}),
            ),
            patch.object(
                service,
                "_load_report_excerpt",
                return_value=(excerpt, "stored_document"),
            ),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", return_value=stage),
            patch.object(
                service,
                "finalize_investment_analysis",
                wraps=service.finalize_investment_analysis,
            ) as finalize,
            patch.object(
                service,
                "get_analysis",
                return_value={"analysis_id": analysis_id},
            ),
        ):
            yield SimpleNamespace(
                analysis_id=analysis_id,
                deterministic_current=deterministic_current,
                document_id=document_id,
                excerpt=excerpt,
                finalize=finalize,
                stage=stage,
            )


    def _tuple_fact_sources(self):
        source_url = "https://example.com/investor/q4-outlook"
        return {
            "microsoft_cloud_gross_margin_guidance": {
                "value": 70,
                "unit": "percent",
                "currency": None,
                "period": "FY2025-Q1 guidance issued 2024-07-30",
                "source_url": source_url,
                "source_location": "outlook: Microsoft Cloud gross margin",
            },
            "microsoft_cloud_gross_margin_reported": {
                "value": 69,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: Microsoft Cloud gross margin",
            },
            "azure_and_other_cloud_services_growth_gaap_percent": {
                "value": 29,
                "unit": "percent_yoy",
                "currency": None,
                "period": "FY2024-Q4 (three months ended 2024-06-30)",
                "source_url": source_url,
                "source_location": (
                    "results: Azure and other cloud services revenue growth"
                ),
            },
            "azure_growth_from_ai_services_points": {
                "value": 8,
                "unit": "percentage_points",
                "currency": None,
                "period": "FY2024-Q4 (three months ended 2024-06-30)",
                "source_url": source_url,
                "source_location": (
                    "results: Azure growth contribution from AI services"
                ),
            },
            "azure_growth_from_ai_services_percent": {
                "value": 8,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4 (three months ended 2024-06-30)",
                "source_url": source_url,
                "source_location": (
                    "results: Azure growth contribution from AI services"
                ),
            },
            "commercial_bookings_growth_contribution_points": {
                "value": 8,
                "unit": "percentage_points",
                "currency": None,
                "period": "FY2024-Q4 (three months ended 2024-06-30)",
                "source_url": source_url,
                "source_location": (
                    "results: commercial bookings growth contribution"
                ),
            },
            "azure_and_other_cloud_services_revenue_growth_guidance": {
                "value": "28% to 29%",
                "unit": "percent_yoy_range",
                "currency": None,
                "period": "FY2025-Q1 guidance issued 2024-07-30",
                "source_url": source_url,
                "source_location": (
                    "outlook: Azure and other cloud services revenue growth"
                ),
            },
            "free_cash_flow": {
                "value": 23.3,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source": "derived",
                "concept": (
                    "derived: operating_cash_flow - "
                    "cash_paid_for_property_and_equipment"
                ),
                "source_url": source_url,
                "source_location": "results: free cash flow",
            },
            "operating_cash_flow": {
                "value": 37.2,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: operating cash flow",
            },
            "cash_paid_for_property_and_equipment": {
                "value": 13.9,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: cash paid for property and equipment",
            },
            "free_cash_flow_growth_percent": {
                "value": 18,
                "unit": "percent_yoy",
                "currency": None,
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: free cash flow growth",
            },
            "capital_expenditures": {
                "value": 19,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: capital expenditures",
            },
            "microsoft_cloud_revenue": {
                "value": 36.8,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: Microsoft Cloud revenue",
            },
            "microsoft_cloud_gross_margin_percent": {
                "value": 69,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4",
                "source_url": source_url,
                "source_location": "results: Microsoft Cloud gross margin",
            },
        }


    def _capex_alias_fact_sources(self):
        sources = self._tuple_fact_sources()
        sources["cash_paid_for_property_and_equipment"]["cash_basis"] = "cash"
        common = {
            "value": 13.9,
            "unit": "usd_billions",
            "currency": "USD",
            "period": "FY2024-Q4",
            "source_url": "https://example.com/investor/q4-outlook",
        }
        sources["same_valued_revenue"] = {
            **common,
            "source_location": "results: revenue",
        }
        sources["capital_expenditures_including_finance_leases"] = {
            **common,
            "cash_basis": "cash_plus_finance_leases",
            "source_location": (
                "results: capital expenditures including finance leases"
            ),
        }
        return sources


    def _tuple_fact_row(self, **overrides):
        fields = {
            "claim_id": "cloud-margin-guide",
            "path": "summary",
            "value": "70%",
            "metric": "Microsoft Cloud gross margin guidance",
            "period": "FY2025-Q1 guidance issued 2024-07-30",
            "unit": "percent",
            "currency": None,
            "source_kind": "fact",
            "fact_path": (
                "deterministic_current."
                "microsoft_cloud_gross_margin_guidance.value"
            ),
        }
        fields.update(overrides)
        row = self._row(**fields)
        del row["quote"]
        return row


    def _cluster_fact_row(
        self,
        *,
        claim_id,
        path,
        value,
        metric,
        unit,
        fact_name,
        currency=None,
    ):
        return self._tuple_fact_row(
            claim_id=claim_id,
            path=path,
            value=value,
            metric=metric,
            period="FY2024-Q4",
            unit=unit,
            currency=currency,
            fact_path=f"deterministic_current.{fact_name}.value",
        )


    def _pass3_azure_rows(self):
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        return [
            self._tuple_fact_row(
                claim_id="azure-growth-gaap",
                path="drivers[0]",
                value="29%",
                metric="Azure and other cloud services year-over-year growth",
                period=period,
                unit="percent",
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_growth_gaap_percent.value"
                ),
            ),
            self._tuple_fact_row(
                claim_id="azure-ai-growth-contribution",
                path="drivers[0]",
                value=8,
                metric="Azure growth contribution from AI services",
                period=period,
                unit="percentage_points",
                fact_path=(
                    "deterministic_current."
                    "azure_growth_from_ai_services_points.value"
                ),
            ),
        ]


    def _candidate_summary_azure_rows(self):
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        return [
            self._tuple_fact_row(
                claim_id="summary-azure-growth",
                path="/summary",
                value="29%",
                metric="Azure and other cloud services year-over-year growth",
                period=period,
                unit="percent",
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_growth_gaap_percent.value"
                ),
            ),
            self._tuple_fact_row(
                claim_id="summary-ai-growth-contribution",
                path="/summary",
                value=8,
                metric="Azure growth contribution from AI services",
                period=period,
                unit="percentage_points",
                fact_path=(
                    "deterministic_current."
                    "azure_growth_from_ai_services_points.value"
                ),
            ),
        ]


    def _year_over_year_ai_contribution_row(self):
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        return self._tuple_fact_row(
            claim_id="summary-ai-growth-contribution",
            path="/summary",
            value=8,
            metric="AI services contribution to year-over-year Azure growth",
            period=period,
            unit="percentage_points",
            fact_path=(
                "deterministic_current."
                "azure_growth_from_ai_services_points.value"
            ),
        )


    def _period_text_row(
        self,
        *,
        claim_id,
        quote,
        value,
        metric,
        period,
        unit,
        currency=None,
    ):
        return self._row(
            claim_id=claim_id,
            path="summary",
            value=value,
            metric=metric,
            period=period,
            unit=unit,
            currency=currency,
            source_kind="text",
            quote=quote,
        )


    def _validated_period_text_rows(self, quote, rows):
        payload = self._payload(rows)
        payload["summary"] = quote
        return service._validated_investment_facts(
            json.dumps(payload),
            excerpt=f"Demand remained durable. {quote}",
            news_items=service._freeze_json_value([]),
            deterministic_current=service._freeze_json_value({}),
            deterministic_prior=service._freeze_json_value({}),
        )


    def _effect_fact_sources(self, *, impact_value=-0.06, eps_value=2.95):
        sources = self._tuple_fact_sources()
        source_url = "https://example.com/investor/q4-outlook"
        common = {
            "unit": "usd_per_share",
            "currency": "USD",
            "period": "FY2024-Q4 (three months ended 2024-06-30)",
            "source_url": source_url,
        }
        sources.update(
            {
                "activision_net_impact_diluted_eps": {
                    **common,
                    "value": impact_value,
                    "source_location": (
                        "results: Activision net impact on consolidated "
                        "diluted EPS"
                    ),
                },
                "diluted_eps": {
                    **common,
                    "value": eps_value,
                    "source_location": "results: consolidated diluted EPS",
                },
            }
        )
        return sources


    def _effect_fact_rows(
        self,
        *,
        impact_value="-$0.06",
        eps_value="$2.95",
    ):
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        return [
            self._tuple_fact_row(
                claim_id="activision-eps-impact",
                path="drivers[0]",
                value=impact_value,
                metric="Activision net impact on consolidated diluted EPS",
                period=period,
                unit="usd_per_share",
                currency="USD",
                fact_path=(
                    "deterministic_current."
                    "activision_net_impact_diluted_eps.value"
                ),
            ),
            self._tuple_fact_row(
                claim_id="reported-diluted-eps",
                path="drivers[0]",
                value=eps_value,
                metric="consolidated diluted EPS",
                period=period,
                unit="usd_per_share",
                currency="USD",
                fact_path="deterministic_current.diluted_eps.value",
            ),
        ]


