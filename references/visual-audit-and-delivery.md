# 视觉审计与交付

## 模式职责

`verification_profile` 必须显式写入每页规格并在批次内固定。用户未指定时使用 `rapid`；只有明确要求独立复核时使用 `reviewed`。

- `rapid`：草稿优先。prebuild、compiler、structure 和 background 是主链；LibreOffice preview 是一次可选诊断。
- `reviewed`：保留严格 runtime、render、visual diff、reviewer 与 final 身份闭包。round 1 可触发一次集中修复；round 2 是终局。

两种模式共享 Layout、构建、结构、背景、Text Run、图片和 OOXML 合同，但不共享渲染证据强度。不得为了让 `rapid` 看起来通过而伪造 strict runtime/final 证据。

## rapid 草稿链

`rapid` 不运行 `preflight_runtime.py`，prebuild 不接收 `--runtime`。compiler 从规格中的第一项 `selected_font` 取得 `preferred_font`；字体存在性、fontconfig、TTC face 与LibreOffice实际解析结果不参与build许可。

结构和背景报告必须绑定当前 PPTX 哈希。以下问题是硬失败：PPTX无法打开、页数/比例错误、结构损坏、核心内容缺失、数据编造、主要内容不可编辑、TextBox/Run覆盖错误、图片化范围违反表示计划。硬失败页面不得进入草稿合并。

结构通过后，PPTX即可作为草稿交付。预览仅在有助于视觉检查时运行一次：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

直接预览在调用时解析LibreOffice与Poppler，不生成batch runtime report，不设置fontconfig。macOS从第一次调用就必须在允许启动应用的执行环境中运行；脚本仍使用独立可写profile和进程锁。

直接预览只尝试一次。以下结果均为软失败：

- LibreOffice/Poppler不存在或版本不可读；
- command error、`SIGABRT`、未生成PDF或preview；
- `pdffonts` 与 `preferred_font` 不匹配；
- preview存在renderer差异但PPTX结构和内容仍有效。

软失败时不重试、不切换locale、不改fontconfig、不换字体、不重建PPTX、不运行visual diff。记录错误或 `matched=false/mismatches[]`，交付当前草稿并披露“预览不可用”或“字体回退”。

## 一次检查与集中修复

有preview时按以下顺序一次返回全部差异：画布/mapping → regions/层级 → 对象数量与几何 → 文字、Text Run、Paragraph、bullet、数字与单位 → 表格/矩阵 → 图形、connector、diagram、chart → picture crop、icon、mask、alpha → background。

P0包括PPTX不可用、核心内容缺失、主要内容不可编辑和数据编造。P1包括数量、比例、结构、fill、字号/换行、行/段距、框内位置、Text Run、bullet、crop、connector、图表或关键装饰错误。P2只包括轻微色差、线宽或renderer近似。字体fallback单独作为运行时警告，不归入P0/P1，也不是重建理由。

存在可修复P0/P1时，把全部问题按共同根因修改同一 `prepare_spec.py` 一次。以新哈希从prebuild起重建build、structure和background；如仍需要视觉检查，最多再运行一次preview。不得逐项边看边改、沿用旧哈希报告或只重跑有利指标。

## reviewed 严格链

`reviewed` 运行 `preflight_runtime.py`，并在prebuild与render传入passing `--runtime`。继续生成当前哈希的PDF、font report、preview、visual diff、region evidence、reviewer raw response与只读final。严格链仍要求runtime、工具、字体文件和证据哈希一致；任何缺失都使reviewed失败，但不得扣留已经生成且结构通过的PPTX草稿。

reviewer使用全新只读上下文，只返回契约JSON，不修改文件。round 1通过即停止；可修复P0/P1允许一次集中修复后进入round 2；round 2只有`passed`才写`reviewed_passed`，其他结果写`reviewed_failed`并交付当前草稿。

## 多页与交付

单页prebuild/build/structure失败时保留诊断并继续后页。`rapid`结构通过页使用draft merger，不要求spec/final/runtime/render三元组：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

draft merger重新验证每个输入为结构有效的单页16:9 PPTX，并验证合并后的整份deck。`reviewed`成功成品继续使用input/spec/final-report严格三元组。

最终提供可编辑PPTX、结构报告以及实际存在的preview/diff/raw response；披露缺页、开放P0/P1、字体回退和未验证项。只要结构通过，不得因LibreOffice或字体证据缺失而隐藏或拒绝交付草稿。
