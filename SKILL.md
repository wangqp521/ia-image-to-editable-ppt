---
name: ia-image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 唯一目标与保留能力

本 Skill 的唯一核心职责是把图片高保真地转换为可编辑 PPT。不得扩展成独立平台、通用系统、OCR 或视觉检测框架。

固定优先级：内容事实正确 → 视觉高保真 → 主要文字、数据和基础结构可编辑 → 多页终态合并。不得美化、自动平均、补造隐藏内容、用对象数量冒充质量，或用整页原图加少量文本冒充可编辑页面。照片、Logo、图标、插画、纹理和复杂装饰可保留为当前页最小范围局部 picture。

保留 schema v2、现有 `validate_pptx.py`、`merge_pptx.py`、图标透明/保底色双模式裁切、macOS fontconfig、Text Run、原生 bullet、表格合并、局部边线、圆角 adjustment、图片裁剪和 OOXML 安全规则。

本升级仍使用 schema v2，但旧 schema v2 终态规格若缺少 `review_round`、coverage 或 validator 的 PPTX 哈希绑定，不得直接复用；必须从当前 PPTX 与证据重建 visual gate 和 editability gate，不得伪造迁移字段。

## 三级验证模式

`verification_profile` 是项目级固定模式；同一批输入的所有页面只允许使用一个值。默认使用 `rapid`，只有用户明确提出“独立复核”时选择 `reviewed`，明确提出“严格审核”时选择 `strict`。模式一经写入首个页面规格，本项目不得自动升级或降级；即使校验失败也只在当前模式内修正或按失败状态交付。

| 模式 | 触发方式 | 终态成功状态 | 验证边界 |
|---|---|---|---|
| `rapid` | 默认 | `rapid_validated` | 主代理完成结构校验、整页视觉差异和终态绑定；不启动独立 reviewer，不生成 regions 200% 证据 |
| `reviewed` | 用户明确“独立复核” | `reviewed_passed` | 独立 reviewer 最多 2 轮，只为必要区域生成 200% 证据，不得进入 `strict` |
| `strict` | 用户明确“严格审核” | `strict_gate_passed` | 保留完整 regions 200% 证据、candidate 质量下限、最多 2 轮独立审查和完整哈希绑定 |

显式规格必须同时写入匹配状态：构建中统一为 `pending`；失败为 `rapid_validation_failed`、`reviewed_failed` 或 `strict_gate_failed`。旧规格缺少 `verification_profile` 时仅为兼容而按 `strict` 校验；新任务不得省略该字段。

## 单页流程

1. 每页建独立目录；非续作时写 `session_reuse.mode=fresh_reconstruction`。主代理是 PPTX、规格、脚本和资产的唯一写入者。
2. 首次运行 `preflight_runtime.py`，创建页级 `work/libreoffice-profile` 并在原子输出 `work/preflight-runtime.json` 中记录 `libreoffice_profile.uri`；失败不得生成。首次预览直接使用该 URI，不得先访问默认用户配置目录再重试。完成测量后，写规格前通过 commentary 展示当前坐标定位图并检查。
3. 只加载命中的 reference，先写唯一 schema v2 `work/page-reconstruction.json`、`verification_profile` 和 `pending`。字体试排不是固定阶段；原字体可用时直接使用，不试排；不可用时固定使用 `Noto Sans CJK SC`，版式异常只按文字 reference 有界缩字。`render_font_trials.py` 仅在用户明确要求字体对比时使用。图标固定“一次精确测量 → 一次批量裁切 → 一次绿幕展示与确认 → prebuild”：一次确定全部 bbox 与初始 crop mode，以 `extract_icon_asset.py --spec/--output-dir` 生成并回填资产。质量验收只改临时资产路径，保留裁切决策；失败停。默认先执行 `alpha_isolation`，透明结果有损才逐项改用 `background_preserved` 并写 `fallback_reason`。图标页面在 prebuild 前通过 commentary 展示当前图标裁切绿幕复核图并检查。随后首次运行 `validate_reconstruction_spec.py --stage prebuild --output <report.json>`，不得事后反补规格。
4. 生成一页 16:9 PPTX。主要对象反查 `element_id`；OOXML 名称写 `ia:<element_id>`，多部件写 `ia:<element_id>:<part>`。画布外、隐藏或透明空对象不得充当可编辑证据。
5. 运行 `validate_pptx.py --expected-slides 1 --spec ... --output <report.json>`，修正后重验。再按[视觉审计与交付](references/visual-audit-and-delivery.md)执行当前模式：`rapid` 运行 `create_visual_diff.py --profile rapid`；`reviewed` 生成必要区域证据并独立复核；`strict` 执行完整证据与 candidate 收敛。中间修复只重建受影响区域证据。
6. 终态只显式运行一次 `validate_reconstruction_spec.py --stage final`。失败不切换模式、不伪造通过状态，按当前模式交付现有产物。

## 自动 preflight 和测量工具

从 Skill 根目录运行；工具只采集当前页事实，不扩展状态机。

```bash
python3 scripts/create_coordinate_overlay.py <source> --output <page>/work/coordinate-overlay.png
python3 scripts/inspect_image_region.py <source> --output-dir <page>/work/measurements --point X,Y --bbox LEFT,TOP,RIGHT,BOTTOM
python3 scripts/extract_icon_asset.py --spec <page>/work/page-reconstruction.json --output-dir <page>/assets/icons
python3 scripts/create_icon_crop_review.py <page>/work/page-reconstruction.json --output <page>/comparisons/icon-crop-review.png
FONTCONFIG_FILE="$PWD/assets/fontconfig-macos.conf" soffice "-env:UserInstallation=<preflight-runtime.json:libreoffice_profile.uri>" --headless --convert-to pdf --outdir <preview-dir> <page.pptx>
```

区域测量命令输入 LTRB，规格 `source_bbox` 固定 XYWH。不得请求外部 OCR 服务、API 或 Token；无法确认的内容记录为未验证项，不得补造。

## 条件 reference 路由

普通页面不得全量读取未命中模块。

| 条件 | 读取 |
|---|---|
| 每个非空页面 | [测量与布局](references/measurement-and-layout.md) |
| 有普通/特殊文字、列表、表格文字 | [文字与可编辑性](references/text-and-editability.md) |
| 有表格、矩阵、状态条、图示、连接线或图表 | [图形与图示](references/graphics-and-diagrams.md) |
| 有图标、照片、Logo、截图、蒙版、背景或图片效果 | [图片与图标](references/pictures-and-icons.md) |
| 每页视觉审查、结构校验与交付 | [视觉审计与交付](references/visual-audit-and-delivery.md) |

## 多页顺序与合并

默认逐页串行并保持项目级固定模式。失败页输出当前产物后继续处理后续页面，失败页仍按上传顺序参与合并。最终只用 `merge_pptx.py`；合并器必须拒绝混合模式。标签为“快速校验版 / 快速校验未通过版”“独立复核通过版 / 独立复核未通过版”“完整视觉门禁通过版 / 完整视觉门禁未通过版”，后两种失败版亦属“未通过视觉门禁版”。

合并时每个 `--input <page>.pptx` 必须按相同顺序配对 `--spec <page>/work/page-reconstruction.json`；merger 逐页重算 PPTX SHA-256、结构报告和 reviewer 绑定，不接受旧页或错页。

交付可编辑 PPTX、当前 preview/对照/diff、结构与 final 报告、当前模式要求的区域或 reviewer 证据，以及 P2、字体 fallback 和未验证项。缺证据、旧哈希、结构/final 失败或 tripwire 触发时不得称当前模式完成；失败分支不新增 schema、validator 或状态机。
