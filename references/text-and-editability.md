# 文字与可编辑性

## 来源文本容器

内容逐字服从 `content_reference`。一个来源文本容器对应一个 `TextBox/TextFrame`；自然段、同源列表和混合样式不得按视觉行或条目拆框。视觉自动折行留在原 Paragraph，不写硬回车。只有来源本来是独立对象才拆框。

`modules.typography.items[]` 用唯一 `element_id` 绑定文字 element，保存 `text/source_font_guess/candidates/selected_font/fallback_reason/fallback_trace/runs/paragraphs/text_box/internal_font_declaration/font_declaration_verified`。`runs` 与 `paragraphs` 均以连续 `start/end` 无空洞覆盖全文；`text_box` 保存 EMU x/y/w/h、四边 margin、水平/垂直对齐、wrap、overflow、soft/paragraph breaks。生成前不写最终 OOXML ID。

## Text Run 与原生列表

字体、字号、字重、颜色、斜体、下划线、删除线、上下标和局部字号变化精确到 Text Run；标题、标签和强调范围不得退化为整框统一 `font_weight: 400`。Paragraph 与 Run 不得互相替代。

同源列表只用一个 TextBox，每项一个原生 Paragraph；bullet 使用 `buChar`、`buAutoNum` 或 `buBlip`，不得用圆点 Shape、图标或正文字符模拟。每个列表 paragraph 保存 `is_list/level/bullet_type/bullet/bullet_font/bullet_size_mode/bullet_size_value/bullet_color` 及 EMU `margin_left/indent`；`paragraph_breaks` 等于除最后段外的 paragraph end。最终必须用 `validate_pptx.py --spec` 一一核对 TextBox、段落数、字符范围、bullet 身份、层级和缩进。

## 字体与字号

标题、正文、表头、矩阵小字、说明和页码分别判断，不把单一字体结论套全页。先看字形骨架、宽高比和视觉重量，再核对本机真实字体；建立 2–5 个候选，以同文字、同 box、同段落结构试排。判断顺序：行数/换行点 → 字面宽高 → 基线 → 视觉重量 → 相邻留白。字号可用小数；OCR 高度和层级名称只作初值。

普通低风险文字可省略 `candidate_trials/render_metrics/font_trial_report`。标题、fallback、密集小字、换行敏感或判断不稳时必须运行 `render_font_trials.py`；三字段同时存在并绑定真实 `font-trials.json`，不得编造数值。原字体不可用时记录候选、替代理由、实际 resolved font 和剩余 P2；未真实渲染不得声称已验证。

macOS preview 显式使用 `FONTCONFIG_FILE="$PWD/assets/fontconfig-macos.conf"`。该文件只增加字体发现路径，不保证字体或中文字形存在。renderer fallback 与 PPTX 内部声明分开记录；不得只因 LibreOffice 错误 fallback 就擅自改变目标字体。

调整顺序：字体 → 字号 → box 宽高 → margin → 字间距 → 行/段间距。不得优先硬换行、拆框、过度缩字、改写内容或把可编辑文字图片化。表格/矩阵文字另查 cell margin、行高、基线、水平/垂直对齐和边线距离，不得为防压线缩小整表字号。

文字修复先用一个代表性高风险 TextBox 验证字号、行距、hard/soft break 和 margin，通过后才把同根因修复用于同组对象，禁止未经试验进行全页统一换行批改。candidate 只重渲染受影响文本框及相邻段落边界，检查孤字、单字换行、溢出、截断和 Text Run；出现新回退即拒绝。进入 reviewer 前仍生成全页证据。

## 特殊文本

旋转、竖排、堆叠、上下标、公式、化学式和 WordArt 写入 `modules.special_text` 并绑定 element。优先原生 TextBox/Run 或可编辑公式组合；只有无法可靠识别且原生表示显著失真时，才把公式字形最小范围图片化，周围标题/编号/说明仍可编辑。

保持阅读顺序、rotation 中心、方向、bbox 和基线；glyph、shadow/glow 不得被裁切。上下标绑定正确 token；分数线、根号横线、括号/矩阵/绝对值、反应箭头连续完整。不得用错误 Unicode、随机拆字或无证据字符补造内容。文字 fill/outline/shadow/glow/渐变/path 只按可见效果复刻，不得添加 halo 或来源不存在的效果。

## 最低可编辑标准

主要文字、数字、表格数据、矩阵语义和基础结构可独立选择；照片和复杂装饰只能覆盖最小必要范围，其上的主要文字不得一起栅格化。结构 validator 的对象数只是证据；最终仍要检查实际选择粒度、Text Run、Paragraph、bullet 和图片化风险。
