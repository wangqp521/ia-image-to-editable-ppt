---
name: ia-image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 唯一目标与保留能力

本 Skill 的唯一核心职责是把图片高保真地转换为可编辑 PPT。所有测量、规格和门禁只服务当前转换，不得扩展为独立平台、通用系统、OCR 服务或视觉检测框架。

固定优先级：内容事实正确 → 视觉高保真 → 主要文字、数据和基础结构可编辑 → 多页终态合并。不得美化、自动平均、补造隐藏内容、用对象数量冒充质量，或用整页原图加少量文本冒充可编辑页面。照片、Logo、图标、插画、纹理和复杂装饰可保留为当前页最小范围局部 picture。

保留 schema v2、现有 `validate_pptx.py`、`merge_pptx.py`、图标透明/保底色双模式裁切、macOS fontconfig、Text Run、原生 bullet、表格合并、局部边线、圆角 adjustment、图片裁剪和 OOXML 安全规则。

本升级仍使用 schema v2，但旧 schema v2 终态规格若缺少 `review_round`、coverage 或 validator 的 PPTX 哈希绑定，不得直接复用；必须从当前 PPTX 与证据重建 visual gate 和 editability gate，不得伪造迁移字段。

## 主代理与视觉子代理权限

- 主代理是当前页 PPTX、规格、脚本和资产的唯一写入者；修正也只由主代理执行。
- 每轮必须启动全新上下文的独立视觉子代理作只读审查。它不得修改任何文件，不得读取构建脚本、规格、自报状态、上一轮结论或结构门禁结果；只接收原图、当前 preview、对照图、overlay/diff 和区域 200% 证据。
- 视觉子代理返回 P0/P1/P2 与哈希绑定结论；主代理不得替它宣布通过。PPTX 一旦改变，旧 preview、证据、视觉结论和旧对比图立即失效。

## 单页流程

1. 新任务创建独立页目录；除非用户明确续作本会话交付，否则 `session_reuse.mode=fresh_reconstruction`，不得读取工作区历史产物。
2. 用 `preflight_runtime.py` 将依赖身份写入 `work/preflight-runtime.json`；失败不得生成。完成图片 preflight、测量，写规格前通过 commentary 展示当前坐标定位图。
3. 只加载当前页命中的 reference，先写唯一 schema v2 `work/page-reconstruction.json`。若有图标，先确认带 bbox 的局部上下文并将 `roi_context_400` 记为 passed，再逐图标默认先执行 `alpha_isolation`；前景触边时不得回退，必须扩大 bbox 重跑。只有透明结果出现可见轮廓、阴影或色晕损失时，才显式改用 `background_preserved` 并逐图标记录 `fallback_reason`。写回哈希并完成绿幕复核，prebuild 前通过 commentary 展示当前图标裁切绿幕复核图；图标资产与绿幕复核必须在 prebuild 前完成。再首次运行 `validate_reconstruction_spec.py --stage prebuild --output <report.json>`，不得先生成 PPTX 再反补规格。
4. 主代理按规格生成一页 16:9 PPTX；主要对象必须反查到 `elements[]` 或已激活 module 的 `element_id`。单对象的 OOXML `cNvPr@name` 写为 `ia:<element_id>`，多部件对象写为 `ia:<element_id>:<part>`；画布外、隐藏、透明空对象不得充当可编辑证据。
5. 每轮 reviewer 前首次运行 `validate_pptx.py --expected-slides 1 --spec ... --output <report.json>`；写入修正后重验至通过。
6. 按[视觉审计与交付](references/visual-audit-and-delivery.md)的复用合同准备全页证据；未完整命中才重建。预检、commentary 展示后启动第 1 或 2 轮独立 reviewer；第 1 轮通过即停止，`not_reviewable` 也计入一轮。
7. 第 1 轮存在 P0/P1 时，把全部 finding 写入唯一 `modules.high_risk.items`，先批量修 P0，再修 P1。每批只从 accepted 生成唯一 candidate；中间只重建受影响区域证据，未改善、新增 P0/P1 或结构失败时丢弃。全部 P0/P1 在当前 accepted 上 `result=passed` 后，才重跑结构门禁并按复用合同准备一次全页证据；预检通过才可消耗第 2 轮 reviewer。第 2 轮仍有 P0/P1 或 `not_reviewable` 时停止，不得开启第 3 轮或未审查修复。
8. reviewer 对当前哈希通过后不得再写入 PPTX；visual/editability gate 绑定同一终态。终态只显式运行一次 `validate_reconstruction_spec.py --stage final`；final 内重新运行 `validate_pptx.py` 并校验 PPTX、preview、visual-diff、overlay/diff/region 和 reviewer 哈希。不得再追加等价的 `validate_pptx.py`、`unzip -t` 或另一轮 full final。

## 自动 preflight 和测量工具

从 Skill 根目录运行。preflight 只采集当前页事实：绝对路径、SHA-256、像素尺寸/alpha、依赖可用性、直通/清洗判断、工作目录和页序号；不生成额外状态机。

```bash
python3 scripts/create_coordinate_overlay.py <source> --output <page>/work/coordinate-overlay.png
python3 scripts/inspect_image_region.py <source> --output-dir <page>/work/measurements --point X,Y --bbox LEFT,TOP,RIGHT,BOTTOM
python3 scripts/extract_icon_asset.py <source> --icon-id <id> --bbox-xywh X,Y,W,H --crop-mode alpha_isolation --output <page>/assets/icons/<id>.png
python3 scripts/create_icon_crop_review.py <page>/work/page-reconstruction.json --output <page>/comparisons/icon-crop-review.png
python3 scripts/render_font_trials.py --text '<text>' --font '<font>' --size-pt 18 --box-in W,H --output-dir <page>/work/font-trials --fontconfig assets/fontconfig-macos.conf
FONTCONFIG_FILE="$PWD/assets/fontconfig-macos.conf" soffice --headless --convert-to pdf --outdir <preview-pdf-dir> <page.pptx>
```

坐标网格和显式区域测量用于所有页面；区域测量命令接收 LTRB 边界，但报告给规格的 `source_bbox` 固定为 XYWH。`extract_icon_asset.py` 只处理一个已测量图标，不做自动检测或批量资产系统。`render_font_trials.py` 只在标题、字体 fallback、密集小字、换行敏感或字体判断不稳时使用。工具不做 OCR、对象识别或自动选胜者。

不得请求、配置或使用任何外部 OCR 服务、API 或 Token（包括 PaddleOCR），也不得因缺少此类凭据暂停或询问用户；直接依据输入图片和当前视觉能力处理，无法确认的内容记录为未验证项，不得补造。

## 条件 reference 路由

普通页面不得全量读取未命中模块。

| 条件 | 读取 |
|---|---|
| 每个非空页面 | [测量与布局](references/measurement-and-layout.md) |
| 有普通/特殊文字、列表、表格文字 | [文字与可编辑性](references/text-and-editability.md) |
| 有表格、矩阵、状态条、图示、连接线或图表 | [图形与图示](references/graphics-and-diagrams.md) |
| 有图标、照片、Logo、截图、蒙版、背景或图片效果 | [图片与图标](references/pictures-and-icons.md) |
| 每页视觉审查、结构校验与交付 | [视觉审计与交付](references/visual-audit-and-delivery.md) |

## 独立视觉门禁与自动结构门禁

只有独立 reviewer 对当前 source/preview 哈希返回 `passed`、coverage 完整且没有 P0/P1，视觉门禁才可通过；终态 `visual_gate.review_round` 必须为 1 或 2。相似度、foreground similarity 和 edge F1 只作差异定位；有批准基线时 tripwire 可单向阻断，无批准基线时必须写 `available=false, triggered=null`。

结构门禁继续由 `validate_pptx.py` 自动执行，检查单页、16:9、对象、Text Run、原生列表、图片化风险和规格一致性。结构通过不能替代视觉通过；每次 reviewer 前结构必须稳定通过，终态 reviewer 后不再进行写入修正。

## 未通过后的当前产物输出

失败分支不新增 schema、validator 或状态机。第 1 轮 P0/P1 无安全改善路径或第 2 轮未通过时，输出与最近 reviewer 哈希一致的 PPTX、preview、对照图、overlay、diff、`visual-diff.json`、区域证据、结构报告和已发生的审查记录；缺失项如实列出，不补造证据。

- 含 P0：标注“未通过视觉门禁，含 P0，当前 PPTX 可能不可用”。
- 仅有 P1：标注“未通过视觉门禁的可编辑草稿”。
- `not_reviewable`：标注“当前 PPTX 未完成视觉审核，证据不可审查”。

不得在第 2 轮 reviewer 后修改 PPTX 或生成与该 reviewer 哈希不一致的新证据。严格 `final` 只用于 reviewer 已通过的页面；失败分支不得伪造 `passed` 或强求 `final` 通过。

## 多页顺序与合并

默认严格串行：第 N 页完成 prebuild、生成、结构校验和最多 2 轮独立视觉审查后，通过页完成 `final`，失败页完成当前产物输出，再开始第 N+1 页。失败页输出当前产物后继续处理后续页面；失败页仍按上传顺序参与合并。最终只用 `merge_pptx.py` 按上传顺序合并，并复核页数、顺序和每页审核结论；任一页面存在 P0/P1 或 `not_reviewable` 时，整份合并 PPTX 标注为“未通过视觉门禁版”，结构通过不得描述为视觉通过。用户明确要求并行时，每页仍须独立目录、唯一写入者和完整门禁；失败页不得用占位页伪造成功。

合并时每个 `--input <page>.pptx` 必须按相同顺序配对 `--spec <page>/work/page-reconstruction.json`；merger 逐页重算 PPTX SHA-256、结构报告和 reviewer 绑定，不接受旧页或错页。

## 最小交付清单

- 可编辑 PPTX；
- 当前 PPTX 的 preview；
- 对应双联图或三联图；
- 当前 overlay、diff、`visual-diff.json` 和必要区域证据；
- 结构校验、通过页面的 final 规格校验结果和失败页面已发生的 reviewer 记录；
- P2 近似、字体 fallback 或未验证项说明。

缺证据、旧 preview、任一 P0/P1、结构失败、final 校验失败或哈希不一致时，不得称为完整完成；若已到第 2 轮，仍按失败分支输出当前可用产物。
