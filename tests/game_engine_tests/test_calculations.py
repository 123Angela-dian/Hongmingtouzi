from __future__ import annotations

import unittest
from decimal import Decimal

from database_game.calculations import calculate_annualized_irr, calculate_deal_box, calculate_roi
from database_game.models import MasterMandate, RoleBid, RoleName, ScenarioName, TermRange


def term(key: str, minimum: str, preferred: str, maximum: str, unit: str = "ratio") -> TermRange:
    return TermRange(
        term_key=key,
        unit=unit,
        minimum=Decimal(minimum),
        preferred=Decimal(preferred),
        maximum=Decimal(maximum),
    )


class GameCalculationTest(unittest.TestCase):
    def test_deal_box_finds_overlap_and_gap(self) -> None:
        mandate = MasterMandate(
            positions=[
                term("debtor_settlement_cash", "0", "50", "100", "CNY"),
                term("creditor_recovery_ratio", "0.50", "0.65", "0.75"),
            ],
            minimum_irr=Decimal("0.15"),
            maximum_holding_months=36,
            minimum_downside_recovery_ratio=Decimal("0.70"),
        )
        bids = [
            RoleBid(
                role=RoleName.DEBTOR,
                scenario=ScenarioName.BASELINE,
                position_summary="要求补偿后配合交割。",
                positions=[term("debtor_settlement_cash", "80", "100", "120", "CNY")],
                cooperation_score=Decimal("0.5"),
                scenario_success_score=Decimal("0.5"),
            ),
            RoleBid(
                role=RoleName.CREDITOR,
                scenario=ScenarioName.BASELINE,
                position_summary="要求较高比例回收。",
                positions=[term("creditor_recovery_ratio", "0.80", "0.90", "1")],
                cooperation_score=Decimal("0.5"),
                scenario_success_score=Decimal("0.4"),
            ),
        ]

        deal_box = calculate_deal_box(mandate, bids)
        baseline = next(item for item in deal_box.overlaps if item.scenario == ScenarioName.BASELINE)
        settlement = next(item for item in baseline.terms if item.term_key == "debtor_settlement_cash")
        recovery = next(item for item in baseline.terms if item.term_key == "creditor_recovery_ratio")
        self.assertTrue(settlement.feasible)
        self.assertEqual(settlement.lower_bound, Decimal("80"))
        self.assertEqual(settlement.upper_bound, Decimal("100"))
        self.assertFalse(recovery.feasible)
        self.assertEqual(recovery.gap, Decimal("0.05"))
        self.assertFalse(baseline.feasible)

    def test_roi_and_irr_are_deterministic(self) -> None:
        self.assertEqual(calculate_roi(Decimal("100"), Decimal("121")), Decimal("0.21"))
        flows = [Decimal("-100"), *([Decimal("0")] * 11), Decimal("121")]
        irr = calculate_annualized_irr(flows)
        self.assertIsNotNone(irr)
        self.assertAlmostEqual(float(irr or 0), 0.21, places=5)


if __name__ == "__main__":
    unittest.main()
