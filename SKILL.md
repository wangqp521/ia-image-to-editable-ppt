---
name: image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 唯一目标与保留能力

职责：把图片高保真转换为可编辑 PPT，不扩成 OCR 或通用视觉检测系统。

优先级：事实正确 → 视觉高保真 → 主要内容可编辑 → 多页合并。禁止美化、自动平均、补造、以对象数冒充质量或整页图片化；照片、Logo、图标、插画、纹理与复杂装饰只保留为当页最小局部 picture。

schema v2 是唯一 Layout IR，compiler 是唯一构建入口；保留结构、字体、图片、Text Run、原生 bullet、表格、连接线、裁剪和 OOXML 安全规则。

## 三级验证模式

`verification_profile` 在项目/批次内固定：默认 `rapid`；仅用户明确要求“独立复核”或“严格审核”时分别使用 `reviewed`、`strict`。首个规格写入后不得升降级；失败只在本模式内修正或按失败状态交付。

三个模式共用同一套复刻与构建逻辑，差别仅是后置视觉证据与 reviewer 深度。

| 模式 | 触发方式 | 终态成功状态 | 验证边界 |
|---|---|---|---|
| `rapid` | 默认 | `rapid_validated` | 主代理完成整页视觉差异与终态绑定；无独立 reviewer、无 regions 200% 证据 |
| `reviewed` | 明确“独立复核” | `reviewed_passed` | 独立只读 reviewer 最多 2 轮；只生成必要 regions 200% 证据；不得进入 `strict` |
| `strict` | 明确“严格审核” | `strict_gate_passed` | 完整 regions 200% 证据、独立只读 reviewer 最多 2 轮、完整哈希绑定 |

显式规格构建中统一为 `pending`；失败状态为 `rapid_validation_failed`、`reviewed_failed`、`strict_gate_failed`。旧规格缺 profile 仅兼容为 `strict`；新任务必填。

## 单一当前版本流程

每页只有一份当前工作规格 `work/page-reconstruction.json` 和一份当前成品 `work/page.pptx`。不得建立并行版本、晋级链或版本比较状态机。历史证据可按 PPTX SHA-256 隔离保存，但它不是另一份可交付 PPTX，终态只能绑定当前哈希。

1. 每页建独立目录；非续作写 `session_reuse.mode=fresh_reconstruction`，仅主代理写产物。
2. 并行启动稳定 runtime preflight、coordinate overlay、source hash/尺寸；输出隔离，任一失败不消费部分结果。后页锁定 runtime；坐标图照常展示。
3. 展示后一次盘点全页，把全部明确的 `--point/--bbox` 合为一次测量；仅触边、污染、遮挡/低清或报告无效时二测。
4. 全部图标 bbox 固定后并发运行独立单图标 extractor；只重跑失败/触边项。runtime、overlay、source identity 就绪后运行 initializer，完成唯一规格并绑定最终图标，再生成一次绿幕。未知字体使用 `Noto Sans CJK SC`。

```bash
python3 scripts/init_reconstruction_spec.py --source <absolute-source> --visual <absolute-clean-visual> --overlay <absolute-overlay> --page-id page-NNN --profile <rapid|reviewed|strict> --output <absolute-output>
```

### 前置结构校验与当前版本构建

先完整写好唯一规格，再运行正式 `prebuild`。未通过时只修改同一规格并重验；通过后刷新 build snapshot，并由 compiler 原子发布或替换同一路径的当前 PPTX 与 build report。禁止绕过 prebuild、另建平行 IR 或降级为整页图片。

```bash
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage prebuild --output work/prebuild-validation.json
python3 scripts/freeze_reconstruction_spec.py work/page-reconstruction.json --purpose build --replace-current --output work/build-spec-snapshot.json
python3 scripts/build_pptx_from_spec.py --spec work/build-spec-snapshot.json --prebuild-report work/prebuild-validation.json --output work/page.pptx --build-report work/build-report.json --replace-current
python3 scripts/render_preview.py work/page.pptx --runtime work/preflight-runtime.json --output-dir preview/<pptx-sha256>
python3 scripts/create_rendered_text_geometry.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --render-report preview/<pptx-sha256>/render-report.json --runtime work/preflight-runtime.json --output evidence/<pptx-sha256>/rendered-text-geometry.json
python3 scripts/validate_pptx.py work/page.pptx --expected-slides 1 --spec work/build-spec-snapshot.json --build-report work/build-report.json --output evidence/<pptx-sha256>/structure-validation.json
```

结构校验未通过不得进入视觉校验。初次 preview 后按 mapping → regions/层级 → 系统文字 → TextBox → 图示 → 图片/图标 → 细节一次列全 P0/P1，把同根因问题合为一组修正。

### 后置视觉校验与原地修正重验

对当前 PPTX 补齐 background、visual diff 与当前 profile 的视觉证据。发现需要修正时，直接修改同一工作规格，原子替换 `work/page.pptx`，并从 `prebuild → build → render → text geometry → structure → background → visual` 全链重验；PPTX 哈希一变，旧证据全部失效。不得只重跑有利指标、复用旧哈希或保留另一份 PPTX 作为质量下限。

```bash
python3 scripts/validate_background_contract.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report evidence/<pptx-sha256>/structure-validation.json --output evidence/<pptx-sha256>/background-contract.json
python3 scripts/create_visual_diff.py <source> --render-report preview/<pptx-sha256>/render-report.json --spec work/build-spec-snapshot.json --output-dir evidence/<pptx-sha256>/visual-diff --profile <rapid|reviewed|strict>
python3 scripts/freeze_reconstruction_spec.py work/page-reconstruction.json --purpose pre-review --output work/review/round-N/pre-review-spec-snapshot.json
python3 scripts/review_admission.py issue --spec work/review/round-N/pre-review-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report evidence/<pptx-sha256>/structure-validation.json --render-report preview/<pptx-sha256>/render-report.json --text-geometry evidence/<pptx-sha256>/rendered-text-geometry.json --background-report evidence/<pptx-sha256>/background-contract.json --visual-diff evidence/<pptx-sha256>/visual-diff/visual-diff.json --review-round <1|2> --output-dir work/review/round-N/admission
python3 scripts/review_admission.py invoke --admission work/review/round-N/admission/review-admission.json --invocation-dir work/review/round-N/invocation
python3 scripts/review_admission.py validate-response --admission work/review/round-N/admission/review-admission.json --invocation work/review/round-N/invocation/page-NNN-round-N-invocation.json --response work/review/round-N/response.json --output work/review/round-N/response-validation.json
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage final --output work/final-validation.json
```

`rapid` 跳过 freeze/reviewer；`reviewed|strict` 把 admission 生成的 prompt 原样交给全新只读 reviewer。若 reviewer 要求修正，先原地修正并重跑全部自动证据，再以新哈希进入下一轮；最多 2 轮。reviewer 通过后禁改 PPTX。final 只绑定当前版本及其当前证据；任何绑定不一致均失败。

## 自动 preflight 和测量工具

从 Skill 根目录运行。

```bash
python3 scripts/create_coordinate_overlay.py <source> --output <page>/work/coordinate-overlay.png
python3 scripts/inspect_image_region.py <source> --output-dir <page>/work/measurements --point X1,Y1 --point X2,Y2 --bbox L1,T1,R1,B1 --bbox L2,T2,R2,B2
python3 scripts/extract_icon_asset.py <source> --icon-id <id> --bbox-xywh X,Y,W,H --output <page>/assets/icons/<id>.png
python3 scripts/create_icon_green_preview.py <page>/work/page-reconstruction.json --output <page>/comparisons/icon-alpha-preview.png
python3 scripts/preflight_runtime.py --soffice <stable-soffice> --pdftoppm <pdftoppm> --pdffonts <pdffonts> --fontconfig assets/fontconfig-macos.conf --output <page>/work/preflight-runtime.json
```

区域测量输入 LTRB，`source_bbox` 使用 XYWH。禁止外部 OCR/API/Token；未知内容标为未验证，不补造。

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

逐页串行并固定模式。prebuild/compiler 失败只留证据、继续后页，不占位、不降级、不合并，并披露缺页；已有 PPTX 的 visual/final 失败页可按原序合并。只用 `merge_pptx.py`，拒绝混合模式、LibreOffice/fontconfig 身份或预览尺寸；标签须明确快速/独立复核/完整门禁及是否通过。

每个 `--input <page>.pptx` 按同序配对 `--spec <page>/work/page-reconstruction.json`；merger 重算各页 PPTX SHA-256、结构报告和 reviewer 绑定，拒绝旧页或错页。

交付可编辑 PPTX、当前 preview/diff、结构/final 报告、当前模式要求的区域/reviewer 证据，并披露 P2、字体 fallback 和未验证项。缺证据、旧哈希、结构/final 失败或 tripwire 触发时不得称完成；失败分支不新增 schema、validator 或状态机。
