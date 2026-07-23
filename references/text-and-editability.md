# 文字与可编辑性

## 来源文本容器

内容逐字服从 `content_reference`。一个来源文本容器对应一个 `TextBox/TextFrame`；自然段、同源列表和混合样式不得按视觉行或条目拆框。视觉自动折行留在原 Paragraph，不写硬回车。只有来源本来是独立对象才拆框。

`modules.typography.items[]` 用唯一 `element_id` 绑定文字 element，保存 `text/source_font_guess/selected_font/fallback_reason/fallback_trace/runs/paragraphs/text_box/internal_font_declaration/font_declaration_verified`。每项只有一个 `selected_font`；`runs`、`paragraphs` 连续覆盖全文；`text_box` 保存 EMU bbox、margin、对齐、wrap、overflow 和 breaks。生成前不写最终 OOXML ID。

## Text Run 与原生列表

字体、字号、字重、颜色、斜体、下划线、删除线、上下标和局部字号变化精确到 Text Run；标题、标签和强调范围不得退化为整框统一 `font_weight: 400`。Paragraph 与 Run 不得互相替代。

同源列表只用一个 TextBox，每项一个原生 Paragraph；bullet 使用 `buChar`、`buAutoNum` 或 `buBlip`，不得用圆点 Shape、图标或正文字符模拟。每个列表 paragraph 保存 `is_list/level/bullet_type/bullet/bullet_font/bullet_size_mode/bullet_size_value/bullet_color` 及 EMU `margin_left/indent`；`paragraph_breaks` 等于除最后段外的 paragraph end。最终必须用 `validate_pptx.py --spec` 一一核对 TextBox、段落数、字符范围、bullet 身份、层级和缩进。

## 字体与字号

各类文字分别判断。来源明确时写实际 family；未知时固定 `source_font_guess=unknown`、`selected_font=Noto Sans CJK SC`、`fallback_reason=source_font_uncertain`。PPTX family 是 `Noto Sans CJK SC`；PDF 预期 resolved name 是 `NotoSansCJKsc-Regular`，可带六位大写字母加 `+` 的子集前缀。不得混写两种名称。

不做字体比较或独立试排。首次未知字体 preview 用 `pdffonts` 确认解析结果；同一 LibreOffice、fontconfig 和字体文件 SHA-256 下项目级只验证一次，并在 `fallback_trace` 记录请求/实际字体、PDF SHA-256 和运行身份。身份变化即重验。

每个最终 PDF 均用 `pdffonts` 检查字体清单；意外 fallback、缺字或额外字体须先修复或披露。仅特殊字符、生僻字、公式、多语言、缺字、意外 fallback、换行、溢出或截断触发局部调查；不逐框验证 resolved font。

macOS preview 显式使用 `FONTCONFIG_FILE="$PWD/assets/fontconfig-macos.conf"`。该文件只增加字体发现路径，不保证字体或中文字形存在。renderer fallback 与 PPTX 内部声明分开记录；不得只因 LibreOffice 错误 fallback 就擅自改变目标字体。

调整顺序：字体 → 字号 → box → margin → 字距 → 行/段距。不得优先硬换行、拆框、过度缩字、改写或图片化文字。表格/矩阵另查 cell margin、行高、基线、对齐和边线距离。

文字修复先验证一个代表性高风险 TextBox，再应用于同组；不得全页盲改换行。candidate 只重渲染受影响框及相邻边界，检查孤字、溢出、截断和 Text Run；出现回退即拒绝。reviewer 前仍生成全页证据。

## 特殊文本

旋转、竖排、堆叠、上下标、公式、化学式和 WordArt 写入 `modules.special_text` 并绑定 element。优先原生 TextBox/Run 或可编辑公式组合；只有无法可靠识别且原生表示显著失真时，才把公式字形最小范围图片化，周围标题/编号/说明仍可编辑。

保持阅读顺序、rotation 中心、方向、bbox 和基线；glyph、shadow/glow 不得被裁切。上下标绑定正确 token；分数线、根号横线、括号/矩阵/绝对值、反应箭头连续完整。不得用错误 Unicode、随机拆字或无证据字符补造内容。文字 fill/outline/shadow/glow/渐变/path 只按可见效果复刻，不得添加 halo 或来源不存在的效果。

## 最低可编辑标准

主要文字、数字、表格数据、矩阵语义和基础结构可独立选择；照片和复杂装饰只能覆盖最小必要范围，其上的主要文字不得一起栅格化。结构 validator 的对象数只是证据；最终仍要检查实际选择粒度、Text Run、Paragraph、bullet 和图片化风险。
