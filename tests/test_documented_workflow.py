import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


class DocumentedWorkflowTests(unittest.TestCase):
    def test_runtime_docs_do_not_offer_removed_mode(self):
        for relative in (
            "SKILL.md",
            "README.md",
            "references/visual-audit-and-delivery.md",
        ):
            text = (SKILL_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("`strict`", text, relative)
            self.assertNotIn("strict_gate_", text, relative)

    def test_skill_defines_reviewed_as_rapid_plus_reviewer(self):
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`reviewed` 先完整执行 `rapid` 基础流程", text)
        self.assertIn("reviewer round 2 是终局", text)

    def test_failed_review_must_still_deliver_current_pptx(self):
        reference = (
            SKILL_ROOT / "references/visual-audit-and-delivery.md"
        ).read_text(encoding="utf-8")
        readme = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "`reviewed_failed` 只表示审核门禁未通过，不得阻止交付当前 PPTX",
            reference,
        )
        self.assertIn("只要当前 PPTX 已生成，即使 `reviewed_failed` 也必须交付", readme)
        self.assertIn("不得进入成功合并成品", reference)


if __name__ == "__main__":
    unittest.main()
