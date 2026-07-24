from __future__ import annotations

import unittest

from graph_rag import _retrieval_config


class RetrievalPlanTest(unittest.TestCase):
    def test_master_directive_changes_retrieval_config_with_whitelist(self) -> None:
        config = _retrieval_config(
            "legal_agent",
            {
                "objective": "重点核验工程款优先受偿权。",
                "event_types": ["claim_formation", "not_allowed"],
                "evidence_types": ["claim", "not_allowed"],
                "keywords": ["工程款优先权", "%"],
                "focus_entities": ["某总包公司"],
            },
        )
        self.assertEqual(config["task"], "重点核验工程款优先受偿权。")
        self.assertIn("claim_formation", config["event_types"])
        self.assertNotIn("not_allowed", config["event_types"])
        self.assertIn("工程款优先权", config["keywords"])
        self.assertIn("某总包公司", config["keywords"])
        self.assertNotIn("%", config["keywords"])


if __name__ == "__main__":
    unittest.main()
