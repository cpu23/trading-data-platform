from __future__ import annotations

import os
import sys
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEPLOYMENT_MODE", "test")

from research_control_plane.skills import (  # noqa: E402
    SkillInput,
    _assess_materiality,
    _option_skew_sign,
    _positioning_divergence,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class MaterialityPolicyTests(unittest.TestCase):
    def test_v1_materiality_is_category_based_and_unknown_versions_fail(self):
        self.assertTrue(
            _assess_materiality(
                policy_version="v1",
                status="resolved",
                effect_type="core_evidence",
                detail={"deltas": [{"change_kind": "changed"}]},
            )
        )
        self.assertFalse(
            _assess_materiality(
                policy_version="v1",
                status="noop",
                effect_type="justified_noop",
                detail={"divergence_detected": False},
            )
        )
        with self.assertRaisesRegex(ValueError, "materiality policy"):
            _assess_materiality(
                policy_version="v2",
                status="resolved",
                effect_type="forecast",
                detail={},
            )

    def test_put_call_skew_keeps_bearish_and_bullish_semantics_explicit(self):
        self.assertEqual(
            _option_skew_sign(
                [
                    {
                        "analytics": {
                            "expiries": [
                                {
                                    "put_call_skew": {
                                        "state": "ok",
                                        "value": 0.03,
                                    }
                                }
                            ]
                        }
                    }
                ]
            ),
            -1,
        )
        self.assertIsNone(_option_skew_sign([{"analytics": {"expiries": []}}]))


class PositioningDivergenceSkillTests(unittest.TestCase):
    def _item(self) -> SkillInput:
        return SkillInput(
            work_order_id=UUID("10000000-0000-4000-8000-000000000001"),
            question_id=UUID("10000000-0000-4000-8000-000000000002"),
            question_type="positioning_divergence",
            atomic_question="Do accepted measures disagree?",
            target_kind="thesis",
            target_ref="10000000-0000-4000-8000-000000000003",
            accepted_cutoff=NOW,
            skill_version_id=UUID("10000000-0000-4000-8000-000000000004"),
            skill_key="expectations.positioning_divergence",
            skill_version=1,
            skill_fingerprint="a" * 64,
        )

    def _session(self, *, positioning_pct: float) -> Mock:
        session = Mock()
        session.execute.side_effect = [
            _Rows(
                [
                    {
                        "thesis_id": self._item().target_ref,
                        "company": "Example",
                        "symbol": "EXM",
                        "theme_id": None,
                        "direction": "long",
                    }
                ]
            ),
            _Rows([]),
            _Rows(
                [
                    {
                        "source": "cftc",
                        "market_id": "EXM",
                        "report_date": date(2026, 8, 21),
                        "category": "managed_money",
                        "long_positions": 40,
                        "short_positions": 60,
                        "net_position": -20,
                        "open_interest": 100,
                        "net_pct_open_interest": positioning_pct,
                    }
                ]
            ),
            _Rows(
                [
                    {
                        "period": "current",
                        "source": "public",
                        "timestamp": NOW,
                        "close": 110.0,
                    },
                    {
                        "period": "prior",
                        "source": "public",
                        "timestamp": datetime(2026, 7, 24, tzinfo=UTC),
                        "close": 100.0,
                    },
                ]
            ),
        ]
        return session

    def test_opposing_accepted_measures_are_material(self):
        result = _positioning_divergence(
            self._session(positioning_pct=-20.0), self._item()
        )

        self.assertEqual(result.status, "resolved")
        self.assertTrue(result.material)
        self.assertTrue(result.detail["divergence_detected"])
        self.assertEqual(result.detail["signals"]["thesis_direction"], 1)
        self.assertEqual(result.detail["signals"]["reported_positioning"], -1)
        self.assertIn("expectations_state", result.detail)

    def test_aligned_measures_are_a_justified_noop(self):
        result = _positioning_divergence(
            self._session(positioning_pct=20.0), self._item()
        )

        self.assertEqual(result.status, "noop")
        self.assertFalse(result.material)
        self.assertEqual(
            result.justified_noop_reason,
            "no_directional_positioning_divergence",
        )


if __name__ == "__main__":
    unittest.main()
