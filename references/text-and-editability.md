# 文字与可编辑性

## 来源文本容器

内容逐字服从 `content_reference`。一个来源容器对应一个 `TextBox/TextFrame`；自然段、同源列表和混合样式不按视觉行或条目拆框，自动折行不写硬回车，只有来源本来独立时才拆框。

`modules.typography.items[]` 用唯一 `element_id` 绑定文字，保存 text、runs、paragraphs、TextBox 与字体声明；每项只有一个 `selected_font`，runs/paragraphs 连续覆盖全文，坐标用 EMU。生成前不写最终 OOXML ID。

所有 `kind=text` 对象只交给统一 Text renderer；不得另写页面级 TextBox 生成函数。renderer 从 typography 索引取得 Text Run、段落、边距、对齐、wrap 与 overflow，规格缺项或绑定冲突即 fail closed。

## Text Run 与原生列表

字体、字号、字重、颜色、斜体、下划线、删除线、上下标和局部字号变化精确到 Text Run；标题、标签和强调范围不得退化为整框样式，Paragraph 与 Run 不互相替代。

同源列表只用一个 TextBox，每项一个原生 Paragraph；bullet 只用 `buChar`、`buAutoNum` 或 `buBlip`。每段保存身份、层级、样式及 EMU `margin_left/indent`，最终由 `validate_pptx.py --spec` 核对。

`follow_text`：`bullet_font`→`buFontTx`、`bullet_size_mode`→`buSzTx`、`bullet_color`→`buClrTx`；禁止固化为当前字体、字号或颜色快照。

原生列表的 `buFontTx/buSzTx/buClrTx` 规范化由 compiler 发布事务内部完成；Skill 不得在 compiler 前后另跑列表规范化或继续使用未规范化 PPTX。

## 字体与字号

来源字体明确时写实际 family；未知时每项固定 `source_font_guess=unknown`、`selected_font=Noto Sans CJK SC`、`fallback_reason=source_font_uncertain`；`fallback_reason` 是枚举值，不写自然语言。PDF resolved name 为 `NotoSansCJKsc-Regular`（可有六位大写子集前缀）。

`runs[].font_size` 固定使用 point（pt），文本坐标使用 EMU；自定义字号字段以 `_font_size_pt` 结尾。初值按页面实际比例估算，不使用固定 96 DPI：

```text
scale_pt_per_source_px =
  min(slide_width_emu / 12700 / page_frame_width_px,
      slide_height_emu / 12700 / page_frame_height_px)
```

比例只映射物理长度，不把 glyph 高度当作字体 em。先确认页面映射、`selected_font`、显式 margin 和关闭 AutoFit，再生成首次整页预览。预览无明显字号、换行或溢出差异时继续；有系统性差异时，从标题、正文、数字/KPI、列表/表格等实际存在组别各选一个代表性高风险 TextBox，以 `new_font_pt = current_font_pt × target_glyph_px / current_glyph_px` 生成一个 candidate，目标框及相邻边界改善后应用于同组。不逐框试排，不做自动字号搜索，不新增字体优化状态机。

不做字体比较或独立试排。未知字体先用 `render_preview.py` 的 `pdffonts` 确认；同一运行环境下项目级只验证一次。每个最终 PDF 都检查 `pdffonts`；仅特殊字符、生僻字、公式、多语言、缺字、意外 fallback、换行或溢出触发局部调查。

调整顺序：字体 → 字号 → box → margin → 字距 → 行/段距；不用硬换行、拆框、过度缩字、改写或图片化掩盖问题。candidate 只检查目标框和相邻边界；回退即拒绝。`validate_pptx.py --spec` 核对 OOXML Text Run 字号与规格 point 值。

render 后、structure 前必须运行 `create_rendered_text_geometry.py`，用当前 spec/PPTX/build/render/runtime 从 PDF bbox 重算原生文字几何。溢出容差在所有 profile 固定为 **1.5 pt**：`<=1.5 pt` 才可通过，`>1.5 pt` 必须失败；不得按页面、字体或 profile 改写容差。`rendered-text-geometry.json` 必须与当前 `content_spec_sha256`、build report 及输入文件身份一致；不得手写 `valid=true`。

## 特殊文本与最低可编辑性

旋转、竖排、上下标、公式、化学式和 WordArt 写入 `modules.special_text`；优先原生 TextBox/Run。仅无法可靠识别且原生表示明显失真时图片化最小字形，周围文字仍可编辑；保持阅读顺序、rotation、方向、bbox、基线和公式结构。

文字、数字、表格数据和基础结构须可独立选择；照片与复杂装饰只覆盖最小范围。最终检查选择粒度、Text Run、Paragraph、bullet 和图片化风险。
