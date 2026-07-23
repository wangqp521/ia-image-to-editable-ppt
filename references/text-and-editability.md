# 文字与可编辑性

## 来源文本容器

内容逐字服从 `content_reference`。一个来源容器对应一个 `TextBox/TextFrame`；自然段、同源列表和混合样式不按视觉行或条目拆框，自动折行不写硬回车，只有来源本来独立时才拆框。

`modules.typography.items[]` 以唯一 `element_id` 绑定文字 element，保存 `text/source_font_guess/selected_font/fallback_reason/fallback_trace/runs/paragraphs/text_box/internal_font_declaration/font_declaration_verified`；每项只有一个 `selected_font`，runs/paragraphs 连续覆盖全文，`text_box` 使用 EMU 并记录 bbox、margin、对齐、wrap、overflow 和 breaks。生成前不写最终 OOXML ID。

## Text Run 与原生列表

字体、字号、字重、颜色、斜体、下划线、删除线、上下标和局部字号变化精确到 Text Run；标题、标签和强调范围不得退化为整框样式，Paragraph 与 Run 不互相替代。

同源列表只用一个 TextBox，每项一个原生 Paragraph；bullet 使用 `buChar`、`buAutoNum` 或 `buBlip`，不用 Shape、图标或正文字符模拟。每段保存 bullet 身份、层级、样式及 EMU `margin_left/indent`；`paragraph_breaks` 等于除末段外的 paragraph end。最终由 `validate_pptx.py --spec` 核对 TextBox、段落、字符范围、bullet 和缩进。

## 字体与字号

来源字体明确时写实际 family；未知时固定 `source_font_guess=unknown`、`selected_font=Noto Sans CJK SC`、`fallback_reason=source_font_uncertain`。PPTX family 为 `Noto Sans CJK SC`，PDF resolved name 为 `NotoSansCJKsc-Regular`（可有六位大写子集前缀）。

`runs[].font_size` 固定使用 point（pt），文本坐标使用 EMU；自定义字号字段以 `_font_size_pt` 结尾。初值按页面实际比例估算，不使用固定 96 DPI：

```text
scale_pt_per_source_px =
  min(slide_width_emu / 12700 / page_frame_width_px,
      slide_height_emu / 12700 / page_frame_height_px)
```

比例只映射物理长度，不把 glyph 高度当作字体 em。先确认页面映射、`selected_font`、显式 margin 和关闭 AutoFit，再生成首次整页预览。预览无明显字号、换行或溢出差异时继续；有系统性差异时，从标题、正文、数字/KPI、列表/表格等实际存在组别各选一个代表性高风险 TextBox，以 `new_font_pt = current_font_pt × target_glyph_px / current_glyph_px` 生成一个 candidate，目标框及相邻边界改善后应用于同组。不逐框试排，不做自动字号搜索，不新增字体优化状态机。

不做字体比较或独立试排。首次未知字体用 `render_preview.py` 的 `pdffonts` 确认；同一 LibreOffice、fontconfig 和字体文件 SHA-256 下项目级只验证一次，`fallback_trace` 绑定请求/实际字体、当前 PDF 和运行身份。每个最终 PDF 都检查 `pdffonts`；仅特殊字符、生僻字、公式、多语言、缺字、意外 fallback、换行或溢出触发局部调查。

调整顺序：字体 → 字号 → box → margin → 字距 → 行/段距；不以硬换行、拆框、过度缩字、改写或图片化掩盖问题。candidate 只重渲染目标框和相邻边界，检查孤字、溢出、截断与 Text Run；回退即拒绝，reviewer 前仍生成全页证据。`validate_pptx.py --spec` 必须核对 OOXML Text Run 字号与规格 point 值。

## 特殊文本与最低可编辑性

旋转、竖排、堆叠、上下标、公式、化学式和 WordArt 写入 `modules.special_text` 并绑定 element；优先原生 TextBox/Run 或可编辑组合。只有无法可靠识别且原生表示明显失真时，才把最小公式字形图片化，周围文字仍可编辑。保持阅读顺序、rotation、方向、bbox 和基线；上下标绑定 token，分数线、根号横线、括号/矩阵/绝对值和反应箭头保持完整，效果只按可见事实复刻，禁止补造字符或裁切 glyph。

主要文字、数字、表格数据、矩阵语义和基础结构须可独立选择；照片和复杂装饰只覆盖最小必要范围。结构对象数不证明质量，最终仍检查选择粒度、Text Run、Paragraph、bullet 和图片化风险。
