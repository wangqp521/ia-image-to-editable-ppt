from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCES = ROOT / "references"
AUTHORITATIVE_REFERENCES = {
    "measurement-and-layout.md",
    "text-and-editability.md",
    "graphics-and-diagrams.md",
    "pictures-and-icons.md",
    "visual-audit-and-delivery.md",
}


def chars(name: str) -> int:
    return len((REFERENCES / name).read_text(encoding="utf-8"))


class SkillRuntimeContractTests(unittest.TestCase):
    def test_three_fixed_verification_profiles_are_explicit(self):
        runtime = SKILL.read_text(encoding="utf-8")
        for phrase in (
            "`verification_profile`",
            "`rapid`",
            "`reviewed`",
            "`strict`",
            "默认使用 `rapid`",
            "不得自动升级或降级",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime)

    def test_rapid_contract_is_lightweight_and_not_independently_reviewed(self):
        combined = SKILL.read_text(encoding="utf-8") + (
            REFERENCES / "visual-audit-and-delivery.md"
        ).read_text(encoding="utf-8")
        for phrase in (
            "`rapid_validated`",
            "`not_independently_reviewed`",
            "不启动独立 reviewer",
            "不生成 regions 200% 证据",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_prebuild_input_quality_checks_apply_to_all_profiles(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        combined = runtime + audit
        for phrase in (
            "写规格前通过 commentary 展示当前坐标定位图并检查",
            "通过 commentary 展示一次当前页最终图标绿幕汇总图",
            "仅展示，不设审核门禁",
            "profile 只控制终态证明成本，不得降低构建前输入质量",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)
        self.assertNotRegex(runtime, r"`strict`[^。\n]*坐标定位图")
        self.assertNotRegex(runtime, r"`strict`[^。\n]*图标裁切绿幕复核图")

    def test_reviewed_contract_is_bounded_and_never_enters_strict(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "`reviewed_passed`",
            "最多 2 轮",
            "不得进入 `strict`",
            "必要区域 200% 证据",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_strict_contract_retains_full_evidence_and_candidate_floor(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "`strict_gate_passed`",
            "完整 regions 200% 证据",
            "accepted 是质量下限",
            "唯一 `candidate.pptx`",
            "完整哈希绑定",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_multi_page_project_rejects_mixed_profiles(self):
        combined = SKILL.read_text(encoding="utf-8") + (
            REFERENCES / "visual-audit-and-delivery.md"
        ).read_text(encoding="utf-8")
        self.assertIn("项目级固定模式", combined)
        self.assertIn("拒绝混合模式", combined)

    def test_exactly_five_authoritative_references_exist(self):
        self.assertEqual(
            AUTHORITATIVE_REFERENCES,
            {path.name for path in REFERENCES.glob("*.md")},
        )

    def test_runtime_load_budgets(self):
        skill = len(SKILL.read_text(encoding="utf-8"))
        measurement, text, graphics, pictures, audit = (
            chars(name)
            for name in (
                "measurement-and-layout.md",
                "text-and-editability.md",
                "graphics-and-diagrams.md",
                "pictures-and-icons.md",
                "visual-audit-and-delivery.md",
            )
        )
        self.assertLessEqual(skill + measurement + text + audit, 15000)
        self.assertLessEqual(skill + measurement + graphics + audit, 15000)
        self.assertLessEqual(
            skill + measurement + text + graphics + pictures + audit, 25000
        )

    def test_skill_keeps_existing_schema_and_structure_tools(self):
        runtime = SKILL.read_text(encoding="utf-8")
        self.assertIn("schema v2", runtime)
        self.assertIn("validate_pptx.py", runtime)
        self.assertNotIn("schema v3", runtime)
        self.assertIn("旧 schema v2 终态规格", runtime)
        self.assertIn("重建 visual gate", runtime)

    def test_graphics_reference_neutralizes_theme_effects_without_deleting_style(self):
        graphics = (REFERENCES / "graphics-and-diagrams.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "不得删除整个 `p:style`",
            "`a:effectRef` 的 `idx` 设为 `0`",
            "清除 `spPr` 下的显式 `effectLst/effectDag`",
            "仅用于目标 Shape/Line",
            "不得作用于表格、图片或 `graphicFrame`",
            "阴影消失",
            "裁切",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, graphics)

    def test_single_font_fallback_replaces_candidate_trials(self):
        runtime = SKILL.read_text(encoding="utf-8")
        typography = (REFERENCES / "text-and-editability.md").read_text(
            encoding="utf-8"
        )
        combined = runtime + typography
        for phrase in (
            "`Noto Sans CJK SC`",
            "`NotoSansCJKsc-Regular`",
            "项目级只验证一次",
            "每个最终 PDF",
            "`pdffonts`",
            "特殊字符",
            "意外 fallback",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)
        for removed in (
            "candidates",
            "candidate_trials",
            "render_metrics",
            "font_trial_report",
            "render_font_trials.py",
            "2–5 个候选",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, combined)

    def test_independent_reviewer_contract_is_explicit(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "只读",
            "不得修改任何文件",
            "不得读取构建脚本",
            "P0",
            "P1",
            "P2",
            "source_sha256",
            "preview_sha256",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_visual_review_is_bounded_to_two_fresh_rounds(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in ("最多 2 轮", "第 2 轮", "不得开启第 3 轮"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime + audit)
        for phrase in ("全新上下文", "`not_reviewable` 也计入一轮"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_current_comparison_is_complete_before_each_reviewer(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "进入 reviewer 前",
            "全页 preview、对照图、overlay、diff 和全部 regions",
            "上一版本的 preview、全页证据和 reviewer 结论立即失效",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime + audit)

    def test_structure_is_stable_before_each_visual_review(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "每轮视觉审查前",
            "validate_pptx.py",
            "终态 reviewer 通过后不得再写入 PPTX",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_reviewer_must_report_complete_blocking_inventory(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "一次返回全部可见 P0/P1",
            "不得只报告首个问题",
            "P0/P1 不设数量上限",
            "coverage",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_core_scope_stays_image_to_editable_ppt_only(self):
        runtime = SKILL.read_text(encoding="utf-8")
        self.assertIn("唯一核心职责", runtime)
        self.assertIn("图片高保真地转换为可编辑 PPT", runtime)
        for prohibited in ("独立平台", "通用系统"):
            self.assertIn(prohibited, runtime)

    def test_icon_reference_defines_lossless_alpha_only_contract(self):
        pictures = (REFERENCES / "pictures-and-icons.md").read_text(encoding="utf-8")
        for phrase in (
            "图标裁切只允许 `alpha_isolation`",
            "`alpha_isolation`",
            "只允许改变 alpha",
            "RGB 必须逐像素一致",
            "前景不得触边",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, pictures)
        for phrase in (
            "background_preserved",
            "crop_review_evidence",
            "icon_manifest_sha256",
            "source_400",
            "asset_400",
            "placement_400",
            "fallback_reason",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, pictures)

    def test_icon_workflow_uses_one_lightweight_xywh_extractor_before_prebuild(self):
        runtime = SKILL.read_text(encoding="utf-8")
        pictures = (REFERENCES / "pictures-and-icons.md").read_text(encoding="utf-8")
        for phrase in (
            "extract_icon_asset.py",
            "--bbox-xywh X,Y,W,H",
            "展示后不等待确认，直接运行 `validate_reconstruction_spec.py --stage prebuild`",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime)
        for phrase in (
            "唯一图标资产生成入口",
            "`source_bbox=[x,y,w,h]`",
            "新任务固定 `padding=0`",
            "开放线框",
            "封闭区域",
            "RGB 必须逐像素一致",
            "背景固定为 `#00FF00`",
            "不得编写页面专用裁切脚本",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, pictures)

    def test_icon_workflow_has_one_fixed_alpha_path_without_fallback(self):
        runtime = SKILL.read_text(encoding="utf-8")
        pictures = (REFERENCES / "pictures-and-icons.md").read_text(encoding="utf-8")
        combined = runtime + pictures
        for phrase in (
            "固定执行 `alpha_isolation`",
            "带 bbox 的局部上下文",
            "前景触边时扩大 bbox",
            "不存在第二种裁切模式",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_green_preview_is_displayed_once_without_becoming_a_gate(self):
        runtime = SKILL.read_text(encoding="utf-8")
        measurement = (REFERENCES / "measurement-and-layout.md").read_text(
            encoding="utf-8"
        )
        pictures = (REFERENCES / "pictures-and-icons.md").read_text(encoding="utf-8")
        for phrase in (
            "写规格前通过 commentary 展示当前坐标定位图",
            "通过 commentary 展示一次当前页最终图标绿幕汇总图",
            "展示后不等待确认",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime)
        for phrase in (
            "[第 N/总页数] 坐标定位图",
            "同一来源 SHA-256 下每页只展示一次",
            "旧坐标定位图立即失效",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, measurement)
        for phrase in (
            "[第 N/总页数] 图标透明效果展示（仅展示，不设审核门禁）",
            "无图标时不生成、不展示",
            "每页最终图标资产集合只展示一次",
            "图标资产发生变化时，以新的最终资产集合重新展示一次",
            "绿幕展示不写入 schema",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, pictures)

    def test_green_preview_tool_replaces_crop_review_tool(self):
        runtime = SKILL.read_text(encoding="utf-8")
        self.assertIn("create_icon_green_preview.py", runtime)
        self.assertNotIn("create_icon_crop_review.py", runtime)
        self.assertTrue((ROOT / "scripts" / "create_icon_green_preview.py").is_file())
        self.assertFalse((ROOT / "scripts" / "create_icon_crop_review.py").exists())

    def test_local_repairs_use_one_candidate_and_preserve_quality_floor(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "唯一 `candidate.pptx`",
            "accepted 是质量下限",
            "未改善目标问题",
            "不得覆盖 accepted",
            "受影响区域及相邻边界",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_round_two_requires_every_round_one_p0_p1_to_be_closed(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "第二轮准入",
            "`modules.high_risk.items`",
            "全部 P0/P1",
            "`result=passed`",
            "不消耗第二轮 reviewer",
            "不得伪造第二轮记录",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_repair_evidence_is_local_but_reviewer_evidence_is_complete(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        combined = runtime + audit
        for phrase in (
            "中间修复只重建受影响区域证据",
            "进入 reviewer 前",
            "全页 preview、对照图、overlay、diff 和全部 regions",
            "终态只显式运行一次",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_round_two_failure_outputs_current_artifacts_with_clear_labels(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        combined = runtime + audit
        for phrase in (
            "继续输出当前可用产物",
            "未通过视觉门禁，含 P0，当前 PPTX 可能不可用",
            "未通过视觉门禁的可编辑草稿",
            "当前 PPTX 未完成视觉审核，证据不可审查",
            "不得称为完整完成或审核通过",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_failed_pages_continue_and_remain_in_merged_deck(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        combined = runtime + audit
        for phrase in (
            "失败页输出当前产物后继续处理后续页面",
            "失败页仍按上传顺序参与合并",
            "未通过视觉门禁版",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_failed_output_does_not_add_a_new_gate_or_schema(self):
        runtime = SKILL.read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        combined = runtime + audit
        self.assertIn("失败分支不新增 schema、validator 或状态机", combined)
        self.assertNotIn("--stage handoff", combined)
        self.assertNotIn("schema v3", combined)

    def test_integrity_gates_bind_current_source_objects_and_evidence(self):
        runtime = SKILL.read_text(encoding="utf-8")
        measurement = (REFERENCES / "measurement-and-layout.md").read_text(encoding="utf-8")
        pictures = (REFERENCES / "pictures-and-icons.md").read_text(encoding="utf-8")
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(encoding="utf-8")
        combined = runtime + measurement + pictures + audit
        for phrase in (
            "`ia:<element_id>`",
            "`ia:<element_id>:<part>`",
            "媒体 SHA-256",
            "final 内重新运行 `validate_pptx.py`",
            "不得从 clean visual 父目录推导",
            "--spec <page>/work/page-reconstruction.json",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_runtime_preflight_and_atomic_validator_reports_are_required(self):
        runtime = SKILL.read_text(encoding="utf-8")
        for phrase in (
            "preflight_runtime.py",
            "work/preflight-runtime.json",
            "--output",
            "首次运行",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime)

    def test_same_run_reuse_requires_complete_composite_identity(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "仅限当前页目录内",
            "PPTX SHA-256",
            "source SHA-256",
            "spec SHA-256",
            "fontconfig SHA-256",
            "渲染器身份",
            "渲染尺寸与裁切参数",
            "证据脚本 SHA-256",
            "区域定义 SHA-256",
            "字段齐全且完全一致",
            "任一字段缺失或不一致",
            "不得跨任务复用",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)

    def test_typography_p1_closure_uses_three_representative_checks(self):
        audit = (REFERENCES / "visual-audit-and-delivery.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "密集正文",
            "数字与单位",
            "换行敏感区域",
            "同根因差异",
            "保持未关闭",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, audit)


if __name__ == "__main__":
    unittest.main()
