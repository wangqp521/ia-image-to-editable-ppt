# 文字与可编辑性

## 来源文本容器

内容逐字服从 `content_reference`。一个来源文本容器对应一个 `TextBox/TextFrame`；自然段、同源列表和混合样式不得按视觉行或条目拆框。视觉自动折行留在原 Paragraph，不写硬回车。只有来源本来是独立对象才拆框。

`modules.typography.items[]` 用唯一 `element_id` 绑定文字 element，保存 `text/source_font_guess/source_font_available/candidates/selected_font/resolved_font/fallback_reason/fallback_trace/runs/paragraphs/text_box/internal_font_declaration/font_declaration_verified`。`runs` 与 `paragraphs` 均以连续 `start/end` 无空洞覆盖全文；`text_box` 保存 EMU x/y/w/h、四边 margin、水平/垂直对齐、wrap、overflow、soft/paragraph breaks。生成前不写最终 OOXML ID。

## Text Run 与原生列表

字体、字号、字重、颜色、斜体、下划线、删除线、上下标和局部字号变化精确到 Text Run；标题、标签和强调范围不得退化为整框统一 `font_weight: 400`。Paragraph 与 Run 不得互相替代。

同源列表只用一个 TextBox，每项一个原生 Paragraph；bullet 使用 `buChar`、`buAutoNum` 或 `buBlip`，不得用圆点 Shape、图标或正文字符模拟。每个列表 paragraph 保存 `is_list/level/bullet_type/bullet/bullet_font/bullet_size_mode/bullet_size_value/bullet_color` 及 EMU `margin_left/indent`；`paragraph_breaks` 等于除最后段外的 paragraph end。最终必须用 `validate_pptx.py --spec` 一一核对 TextBox、段落数、字符范围、bullet 身份、层级和缩进。

## 字体与字号

各类文字分别判断原字体是否可用并写 `source_font_available`；只有当前 fontconfig 精确解析时才可写 true，必要时把原字体作为额外 `--required-font` 交给 preflight。原字体可用时固定 `candidates=[source_font_guess]`、`selected_font=source_font_guess`，不执行字体试排；保留字号、字重和 Text Run，进入 PPTX 构建、结构校验和整页 preview/diff，并把实际解析字体写入 `resolved_font`。`validate_pptx.py --spec` 必须核对 selected font、内部字体声明和全部真实 Text Run 字体声明。

原字体不可用时固定 `source_font_available=false`、`candidates=["Noto Sans CJK SC"]`、`selected_font="Noto Sans CJK SC"`、`resolved_font="Noto Sans CJK SC"`，记录原字体、fallback 原因和 trace，不得增加其他候选。`preflight_runtime.py` 必须通过当前 fontconfig 的 `fc-match` 精确解析 Noto 家族并记录字体文件；无法精确解析时 preflight 失败，不得寻找第二候选。自动流程不运行 `render_font_trials.py`；仅在用户明确要求字体对比时用于手动诊断，不得据此扩展自动候选或选择字体。

自动流程省略 `candidate_trials/render_metrics/font_trial_report`。用户明确要求字体对比且真实运行诊断后，三字段同时存在并绑定真实 `font-trials.json`，不得编造数值；未真实渲染不得声称已完成字体试排验证。

macOS preview 显式使用 `FONTCONFIG_FILE="$PWD/assets/fontconfig-macos.conf"`。该文件只增加字体发现路径，不保证字体或中文字形存在。renderer fallback 与 PPTX 内部声明分开记录；不得只因 LibreOffice 错误 fallback 就擅自改变目标字体。

Noto 因字宽差异造成换行、溢出或截断时，保持文字内容、段落结构和 TextBox 几何不变，以 0.5 pt 为步长缩小字号，累计上限取原字号的 10% 与 2 pt 中更严格者。达到下限仍未解决时保留 P1 并停止，不得继续缩字、硬换行、拆框、图片化或增加字体候选。字形风格、字重或书法感差异不得通过缩小字号关闭 P1。表格/矩阵文字仍须核对 cell margin、行高、基线、水平/垂直对齐和边线距离，不得为防压线缩小整表字号。

文字修复先用一个代表性高风险 TextBox 验证字号、行距、hard/soft break 和 margin，通过后才把同根因修复用于同组对象，禁止未经试验进行全页统一换行批改。candidate 只重渲染受影响文本框及相邻段落边界，检查孤字、单字换行、溢出、截断和 Text Run；出现新回退即拒绝。进入 reviewer 前仍生成全页证据。

## 特殊文本

旋转、竖排、堆叠、上下标、公式、化学式和 WordArt 写入 `modules.special_text` 并绑定 element。优先原生 TextBox/Run 或可编辑公式组合；只有无法可靠识别且原生表示显著失真时，才把公式字形最小范围图片化，周围标题/编号/说明仍可编辑。

保持阅读顺序、rotation 中心、方向、bbox 和基线；glyph、shadow/glow 不得被裁切。上下标绑定正确 token；分数线、根号横线、括号/矩阵/绝对值、反应箭头连续完整。不得用错误 Unicode、随机拆字或无证据字符补造内容。文字 fill/outline/shadow/glow/渐变/path 只按可见效果复刻，不得添加 halo 或来源不存在的效果。

## 最低可编辑标准

主要文字、数字、表格数据、矩阵语义和基础结构可独立选择；照片和复杂装饰只能覆盖最小必要范围，其上的主要文字不得一起栅格化。结构 validator 的对象数只是证据；最终仍要检查实际选择粒度、Text Run、Paragraph、bullet 和图片化风险。
