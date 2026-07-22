---
name: ia-image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 唯一目标与保留能力

本 Skill 的唯一核心职责是把图片高保真地转换为可编辑 PPT；不得扩展为独立平台或通用系统。

优先级：事实正确 → 视觉高保真 → 主要内容可编辑 → 多页合并。禁止美化、补造或用整页原图冒充可编辑页；复杂视觉可用最小局部 picture。

schema v2 是唯一事实源。保留 `validate_pptx.py`、`merge_pptx.py`、图标双模式裁切、macOS fontconfig、Text Run、原生 bullet、表格合并/局部边线、圆角、图片裁剪和 OOXML 安全规则。

旧 schema v2 终态规格缺 `review_round`、coverage 或当前 PPTX 哈希时，必须重建 visual gate/editability gate，不得伪造迁移字段。

## 共同复刻核心

三个验证模式必须完整执行同一套 `reconstruction_core`，不得改变识别、测量、字体、裁切、representation、构建或结构标准：

1. 首次运行 `preflight_runtime.py`，原子输出 `work/preflight-runtime.json`；`create_coordinate_overlay.py` 生成定位图，写规格前通过 commentary 展示当前坐标定位图。
2. 主代理写 schema v2 语义骨架；`scaffold_reconstruction.py` 只补确定性字段。高风险字体用 `render_font_trials.py` 试排；不得自动换字体、缩字或硬换行。
3. `extract_assets.py` 抽取素材；`create_asset_crop_review.py` 复核 context/source/绿幕 asset：图标 400%，其他图片 200%–300%。图标 alpha 委托 `extract_icon_asset.py`；图标资产与绿幕复核必须在 prebuild 前完成，并在 prebuild 前通过 commentary 展示当前图标裁切绿幕复核图。
4. 运行 `validate_reconstruction_spec.py --stage prebuild --asset-review-report <assets-review.json> --output <report.json>`；无素材时省略素材报告。`build_pptx_from_spec.py` 生成 PPTX/`build-report.json`；不支持属性 fail closed，禁止静默忽略或自动图片化。
5. `validate_pptx.py --spec ... --build-report ...` 独立查真实 PPTX；渲染 preview，检查整页 diff、文字/对象位置和图片 crop/mask/alpha/placement。差异只改 schema。

模式分支只能发生在共同复刻核心完成之后，只控制 region 证据、独立 reviewer、审查轮次和哈希绑定强度。

## 三级验证模式

`verification_profile` 是项目级固定模式。默认使用 `rapid`；用户明确“独立复核”才用 `reviewed`，明确“严格审核”才用 `strict`。写入首个页面规格后不得自动升级或降级；失败留在当前模式。

| 模式 | 触发方式 | 终态成功状态 | 验证边界 |
|---|---|---|---|
| `rapid` | 默认 | `rapid_validated` | 主代理完成结构校验、整页视觉差异和终态绑定；不启动独立 reviewer，不生成 regions 200% 证据 |
| `reviewed` | 用户明确“独立复核” | `reviewed_passed` | 独立 reviewer 最多 2 轮，只为必要区域生成 200% 证据，不得进入 `strict` |
| `strict` | 用户明确“严格审核” | `strict_gate_passed` | 保留完整 regions 200% 证据、candidate 质量下限、最多 2 轮独立审查和完整哈希绑定 |

规格构建中为 `pending`；失败状态依次为 `rapid_validation_failed`、`reviewed_failed`、`strict_gate_failed`。旧规格缺 profile 时兼容按 `strict` 校验；新任务不得省略。

## 单页流程

每页独立目录；非续作写 `session_reuse.mode=fresh_reconstruction`。主代理唯一写入，只加载命中 reference。核心后按[视觉审计与交付](references/visual-audit-and-delivery.md)进入 profile；终态仅一次 `validate_reconstruction_spec.py --stage final`。OOXML 名为 `ia:<element_id>` 或 `ia:<element_id>:<part>`；不得为每页编写临时构建代码。

## 常用命令

```bash
python3 scripts/extract_icon_asset.py <source> --icon-id <id> --bbox-xywh X,Y,W,H --crop-mode alpha_isolation --output <page>/assets/icons/<id>.png
FONTCONFIG_FILE="$PWD/assets/fontconfig-macos.conf" soffice --headless --convert-to pdf --outdir <preview-dir> <page.pptx>
```

工具用 `--help` 查看参数。测量输入 LTRB，规格 bbox 固定 XYWH。不得请求外部 OCR/API/Token；不确定内容列为未验证项。

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
