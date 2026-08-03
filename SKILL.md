---
name: image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 目标与边界

把输入图片高保真复刻为可编辑 16:9 PPTX。事实正确优先于视觉高保真，视觉高保真优先于主要内容可编辑；禁止美化、自动平均、补造内容和整页图片化。普通文字、基础图形、表格、连接线与图表应原生可编辑；照片、Logo、图标、插画、纹理、艺术字和复杂装饰只保留为当页最小局部 picture。

schema v2 是唯一 Layout IR，`build_pptx_from_spec.py` 是唯一构建入口。精简流程不得削弱 Text Run、Paragraph、原生 bullet、表格 merge、connector、crop、background、字体 fallback 与 OOXML 安全规则。

## 选择验证模式

`verification_profile` 必须显式写入每页规格，并在一个批次内固定。用户未指定时写 `rapid`；明确要求独立复核时写 `reviewed`；明确要求严格审核时写 `strict`。不得依赖脚本隐式默认值。

- rapid：主代理是唯一正式语义审核者；每页一次判断，最多一次批量修复。
- reviewed|strict：独立 reviewer 是唯一正式语义审核者；主代理只准备确定性证据、转交 prompt、接收原始 JSON 和执行获准的批量修复。
- `reviewed` 的 round 1 通过即停止；只有 round 1 要求修复且新证据链通过，才进入 round 2。
- `strict` 必须生成全部 regions 的 200% 证据；最多两轮。
- round 2 是终局，不再修复，也没有第三轮。

成功状态分别为 `rapid_validated`、`reviewed_passed`、`strict_gate_passed`；否则诚实写入同模式失败状态并披露 P0/P1、P2 和未验证项。

## 批次初始化

从 Skill 根目录执行。每个批次在处理任何页面前运行一次 runtime preflight，并让所有页面复用同一份 passing report。只有 `soffice`、Poppler 工具或 fontconfig 的路径、版本或文件身份发生变化时才重跑；页面修复不重跑。

```bash
python3 scripts/preflight_runtime.py --soffice /Applications/LibreOffice.app/Contents/MacOS/soffice --pdftoppm pdftoppm --pdffonts pdffonts --pdftotext pdftotext --fontconfig assets/fontconfig-macos.conf --output batch/runtime-preflight.json
```

三个 Poppler 参数默认分别为 `pdftoppm`、`pdffonts`、`pdftotext`，可省略；脚本通过当前 `PATH` 解析并在报告中锁定实际绝对路径、版本与 SHA-256。显式传入绝对路径时必须严格使用该路径，缺失即失败，不得自动切换运行时。可由 PATH 自动恢复的路径差异只在内部处理；通过后再报告预检完成。命令名仍缺失时才定位本机或工作区捆绑运行时，并以同一输出路径重跑。

## 直接编写完整规格

每页只维护 `work/page-reconstruction.json` 与 `work/page.pptx` 两个当前对象。展示 source 与 coordinate overlay 后，一次盘点全部元素和关系；把全部明确的点与框合并为一次批量测量。直接写完整 `page-reconstruction.json`，一次填齐 canvas、regions、elements、reading order、activated modules、representation、background、typography 以及条件模块；未知内容标未验证，不补造。

图标 bbox 固定后可并发提取独立资产，只重做失败或触边项。`source_bbox` 使用像素 XYWH，`slide_bbox` 使用 EMU。输入、overlay、资产、字体和量测细节按条件读取下方 references。

简单 2D 单系列 `pie|doughnut` 在分类、数值、纯色和标签合同明确时使用原生 Chart；圆环中心 KPI 仍是独立 TextBox。复杂或证据不足的图表只保留当页最小局部 picture，不在 Renderer 内回退。

## 单页核心链

从 Skill 根目录执行并复用批次级 passing runtime report。下列命令描述无修复时的一次完整页面执行；`PPTX_SHA256` 是构建后实际 PPTX SHA-256 对应的目录名，不是字面路径。`create_reviewer_prompt.py` 仅供 `reviewed|strict` 执行，`rapid` 跳过该命令。

```bash
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage prebuild --snapshot work/build-spec-snapshot.json --output work/prebuild-validation.json
python3 scripts/build_pptx_from_spec.py --spec work/build-spec-snapshot.json --prebuild-report work/prebuild-validation.json --output work/page.pptx --build-report work/build-report.json --replace-current
python3 scripts/render_preview.py work/page.pptx --runtime batch/runtime-preflight.json --output-dir preview/PPTX_SHA256
python3 scripts/create_rendered_text_geometry.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --render-report preview/PPTX_SHA256/render-report.json --runtime batch/runtime-preflight.json --output evidence/PPTX_SHA256/rendered-text-geometry.json
python3 scripts/validate_pptx.py work/page.pptx --expected-slides 1 --spec work/build-spec-snapshot.json --build-report work/build-report.json --output evidence/PPTX_SHA256/structure-validation.json
python3 scripts/validate_background_contract.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report evidence/PPTX_SHA256/structure-validation.json --output evidence/PPTX_SHA256/background-contract.json
python3 scripts/create_visual_diff.py source.png --render-report preview/PPTX_SHA256/render-report.json --spec work/build-spec-snapshot.json --output-dir evidence/PPTX_SHA256/visual-diff --profile reviewed
python3 scripts/create_reviewer_prompt.py work/page-reconstruction.json --review-round 1
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage final --output work/final-validation.json
```

按实际 profile 替换 visual diff 的 `--profile`。规格必须先写齐，再由一次 `prebuild --snapshot` 同时验证并冻结 exact bytes；compiler 只读 snapshot。任何确定性门禁失败都不得进入语义审核。

每个 producer 完成后，将当前证据的绝对路径与 SHA-256 回写到同一工作规格的 `runtime_preflight`、`visual_gate`、`editability_gate`；只回写证据与终态字段，不改 build content。`visual_gate.pptx` 与 `editability_gate.pptx` 必须指向同一当前 PPTX。

## 审核、修复与终态

先核对整页 mapping、regions/层级、文字与 TextBox、图形/连接线/图表、图片 crop 与图标、背景及细节，并一次列全 P0/P1。同根因问题合并为一个修复批次，不边看边改。

`rapid` 的一次正式判断若通过，直接补齐终态字段；若存在可由确定性证据关闭的 P0/P1，可批量修改同一规格一次。修复后不再做第二次语义判断，只按新哈希从 `prebuild --snapshot` 起重建 build、render、text geometry、structure、background 与 visual diff；批次 runtime 身份未变时复用原 preflight。主观 P0/P1 无法由确定性证据关闭时必须失败。

`reviewed|strict` 仅在 background、text geometry、structure 与 visual evidence 完整后生成当前轮 prompt。把生成的 prompt 原样交给全新只读 reviewer；reviewer 不改文件，只返回契约 JSON。raw response 是唯一持久化的 reviewer 产物。缺失或无效均按 not_reviewable，并消耗本轮。round 1 要求修复时，一次映射全部 P0/P1、批量修复、按新哈希从 `prebuild --snapshot` 起重建下游确定性证据，再生成 round 2 prompt；批次 runtime 身份未变时复用原 preflight，round 2 结束即终止。

final 只读：只重新计算并核对当前规格、PPTX、runtime、render、text、structure、background、visual diff、region evidence 与 raw response 的身份和语义，不运行 producer、不修文件、不补证据。final 通过后禁止写入 PPTX；任何改动都使终态失效。

## 条件 reference 路由

只读取命中页面内容的 reference，所有 reference 保持一层：

| 页面条件 | 必读 reference |
|---|---|
| 每个非空页面 | [测量与布局](references/measurement-and-layout.md) |
| 普通/特殊文字、列表、表格文字 | [文字与可编辑性](references/text-and-editability.md) |
| 表格、矩阵、状态条、图示、连接线或图表 | [图形与图示](references/graphics-and-diagrams.md) |
| 图标、照片、Logo、截图、蒙版、背景或图片效果 | [图片与图标](references/pictures-and-icons.md) |
| 每页证据、视觉审核、终态与交付 | [视觉审计与交付](references/visual-audit-and-delivery.md) |

## 多页处理

逐页执行并保持同一 profile。单页 `prebuild` 或 build 失败时保留诊断、继续后页且不占位；已有 PPTX 但 final 失败的页面只能作为明确标注的未通过草稿单独交付，不进入合并。合并时每页必须按同序提供 input/spec/final-report 三元组；merger 只核对单页实际哈希与 final 绑定，不重跑单页 validator，合并后对临时 merged PPTX 只做一次整份结构验证。

```bash
python3 scripts/merge_pptx.py --input page-001/work/page.pptx --spec page-001/work/page-reconstruction.json --final-report page-001/work/final-validation.json --input page-002/work/page.pptx --spec page-002/work/page-reconstruction.json --final-report page-002/work/final-validation.json --output final/deck.pptx
```

交付可编辑 PPTX、当前 preview/diff、结构/final 报告及模式要求的 region/raw reviewer response。缺证据、错哈希、tripwire 触发或开放 P0/P1 时不得称完成。
