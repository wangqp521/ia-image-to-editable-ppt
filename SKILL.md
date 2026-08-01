---
name: ia-image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 唯一目标与保留能力

职责：图片高保真转可编辑 PPT；不扩成 OCR/视觉检测系统。

优先级：事实正确 → 视觉高保真 → 主要内容可编辑 → 多页合并。禁美化/平均/补造/对象数冒充/整页图片化；复杂素材仅作当页最小局部 picture。

schema v2 是唯一 Layout IR，compiler 是唯一构建入口；保留结构/字体/图片/OOXML 安全。

旧终态缺 `review_round`/coverage/validator PPTX 哈希不得复用；据当前产物重建 gate，禁伪造。

## 三级验证模式

`verification_profile` 项目/批次固定：默认 `rapid`；仅用户明确“独立复核”/“严格审核”时分别用 `reviewed`/`strict`。首个规格后禁升降级；失败在本模式修正或失败交付。

| 模式 | 触发方式 | 终态成功状态 | 验证边界 |
|---|---|---|---|
| `rapid` | 默认 | `rapid_validated` | 主代理结构/整页差异/终态绑定；无 reviewer/regions 200% 证据 |
| `reviewed` | 明确“独立复核” | `reviewed_passed` | 独立 reviewer≤2 轮；仅必要 regions 200%；禁转 `strict` |
| `strict` | 明确“严格审核” | `strict_gate_passed` | 完整 regions 200%、candidate 下限、≤2 轮独立审查、哈希绑定 |

显式规格状态：构建中 `pending`；失败为 `rapid_validation_failed`/`reviewed_failed`/`strict_gate_failed`。旧规格缺 profile 仅兼容为 `strict`；新任务必填。

## 单页流程

1. 每页独立目录；非续作写 `session_reuse.mode=fresh_reconstruction`。PPTX、规格、脚本、资产仅主代理写入。
2. 首页以稳定 LibreOffice/`pdftoppm`/`pdffonts`/fontconfig 跑 `preflight_runtime.py`，原子写 `work/preflight-runtime.json`；拒绝开发/alpha/beta/rc。后页用 `--expected-runtime` 锁定。测量后、写规格前用 commentary 展示/检查坐标图。
3. 坐标图后运行 initializer（source=clean visual 可省 `--visual`）：只绑身份/尺寸/envelope，不声称内容/coverage。读取同源 schema/`--describe`，只补当页事实且不另造 schema。未知字体用 `Noto Sans CJK SC`。图标 bbox 后 `alpha_isolation`；触边扩框重跑；绿幕仅展示，非门禁/等待。

```bash
python3 scripts/init_reconstruction_spec.py --source <absolute-source> --visual <absolute-clean-visual> --overlay <absolute-overlay> --page-id page-NNN --profile <rapid|reviewed|strict> --output <absolute-output>
```
4. 反复跑只读 authoring 修错。最后一次通过即 no-overwrite 冻结 `work/build-spec-snapshot.json`；prebuild 至 visual diff 只读它，工作 spec 仅作 gate/终态回写。固定顺序为 `authoring → prebuild → compiler → render → rendered text geometry → structure → background postbuild → visual diff → review admission → invocation → independent reviewer → response validation → final`。compiler 仅接 passing prebuild；禁平行 IR/静默降级；full unsupported fail closed。

```bash
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage authoring --output work/authoring-validation.json
python3 scripts/freeze_reconstruction_spec.py work/page-reconstruction.json --purpose build --output work/build-spec-snapshot.json
python3 scripts/validate_reconstruction_spec.py work/build-spec-snapshot.json --stage prebuild --output work/prebuild-validation.json
python3 scripts/build_pptx_from_spec.py --spec work/build-spec-snapshot.json --prebuild-report work/prebuild-validation.json --output work/page.pptx --build-report work/build-report.json
python3 scripts/render_preview.py work/page.pptx --runtime work/preflight-runtime.json --output-dir preview/<pptx-sha256>
python3 scripts/create_rendered_text_geometry.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --render-report preview/<pptx-sha256>/render-report.json --runtime work/preflight-runtime.json --output work/rendered-text-geometry.json
python3 scripts/validate_pptx.py work/page.pptx --expected-slides 1 --spec work/build-spec-snapshot.json --build-report work/build-report.json --output work/structure-validation.json
python3 scripts/validate_background_contract.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report work/structure-validation.json --output work/background-contract.json
python3 scripts/create_visual_diff.py <source> --render-report preview/<pptx-sha256>/render-report.json --spec work/build-spec-snapshot.json --output-dir comparisons/visual-diff --profile <rapid|reviewed|strict>
python3 scripts/freeze_reconstruction_spec.py work/page-reconstruction.json --purpose pre-review --output work/pre-review-spec-snapshot.json
python3 scripts/review_admission.py issue --spec work/pre-review-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report work/structure-validation.json --render-report preview/<pptx-sha256>/render-report.json --text-geometry work/rendered-text-geometry.json --background-report work/background-contract.json --visual-diff comparisons/visual-diff/visual-diff.json --review-round <1|2> --output-dir work/review-admission
python3 scripts/review_admission.py invoke --admission work/review-admission/review-admission.json --invocation-dir work/review-invocations
python3 scripts/review_admission.py validate-response --admission work/review-admission/review-admission.json --invocation work/review-invocations/page-NNN-round-N-invocation.json --response work/review-response.json --output work/review-response-validation.json
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage final --output work/final-validation.json
```

5. visual diff 后回写 background/text/structure/render/visual、tripwire、editability、P0/P1 关闭事实，再冻结 pre-review snapshot；之后仅写 post-review 字段。`rapid` 跳过该快照和 review 四步；`reviewed|strict` 的 issue 只读快照，只把 admission prompt 交给全新只读 reviewer，保存原始 JSON 后 validate-response。禁手写 prompt/page ID。文字系统差异按[文字与可编辑性](references/text-and-editability.md)校准；门禁按[视觉审计与交付](references/visual-audit-and-delivery.md)。
6. final 必须绑定 production background/text 证据；`reviewed|strict` 还必须绑定 admission/invocation/response-validation。终态只运行上述一次 final；失败不切换模式、不伪造通过状态。

## 自动 preflight 和测量工具

从 Skill 根目录运行。

```bash
python3 scripts/create_coordinate_overlay.py <source> --output <page>/work/coordinate-overlay.png
python3 scripts/inspect_image_region.py <source> --output-dir <page>/work/measurements --point X,Y --bbox LEFT,TOP,RIGHT,BOTTOM
python3 scripts/extract_icon_asset.py <source> --icon-id <id> --bbox-xywh X,Y,W,H --output <page>/assets/icons/<id>.png
python3 scripts/create_icon_green_preview.py <page>/work/page-reconstruction.json --output <page>/comparisons/icon-alpha-preview.png
python3 scripts/preflight_runtime.py --soffice <stable-soffice> --pdftoppm <pdftoppm> --pdffonts <pdffonts> --fontconfig assets/fontconfig-macos.conf --output <page>/work/preflight-runtime.json
```

区域测量输入 LTRB，`source_bbox` 用 XYWH。禁止外部 OCR/API/Token；未知内容标为未验证，不补造。

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

逐页串行并固定模式。prebuild/compiler 失败只留证据、继续后页，不占位/降级/合并并披露缺页；仅已有同事务 PPTX 的 visual/final 失败页可按序合并。只用 `merge_pptx.py`，拒绝混合模式、LibreOffice/fontconfig 身份或预览尺寸；标签须明确快速/独立复核/完整门禁及是否通过。

每个 `--input <page>.pptx` 按同序配对 `--spec <page>/work/page-reconstruction.json`；merger 重算各页 PPTX SHA-256、结构报告和 reviewer 绑定，拒绝旧页/错页。

交付可编辑 PPTX、当前 preview/diff、结构/final 报告、模式要求的区域/reviewer 证据，并披露 P2、字体 fallback、未验证项。缺证据、旧哈希、结构/final 失败或 tripwire 触发不得称完成；失败分支不新增 schema/validator/状态机。
