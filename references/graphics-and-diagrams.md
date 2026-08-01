# 图形与图示

`modules.graphics/diagram/chart` 引用 v2 `element_id`，禁填 OOXML ID；存数/bbox/结构/样式/层级/可编辑性。

v1 native：文字、rectangle/roundRect/ellipse/chevron/rightArrow、线、表格、matrix/status、picture/icon；multipart 用 `composite` parts/repeat，不建 IR。自由曲线/其他 preset/native chart unsupported；`required_editability=full|labels_and_geometry` 禁 asset fallback，prebuild 失败即停。`parts/repeat_sequence` 默认禁重叠；仅源图确有重叠且各 part bbox/层级忠实时，父 element `content.allow_overlap=true`；禁为绕错改 bbox、并 parts、滥用开关。

## 表格、矩阵与框线

行列/合并明确用原生表格；不规则分区/边界/组/跨行用 Shape/Line/TextBox。表格不拆文本框，网格图示不强制表格化。

存行列数、非均匀尺寸、merge span、cell fill/margin/align、四边及线起止/颜色/宽度/透明度/虚实；禁补网格/无线区、延长局部线。合并区、组内外线/填充逐范围存；线不穿合并区，换行不生线。

闭合虚框为对象，存 bbox、线宽、颜色、虚线、层级，不按短线计数或漏边。

## 状态条、圆角、线和填充

底轨同高可见才用 `track_plus_fill`；仅细线续接用 `fill_plus_continuation_line` 且 `track_bbox=null`。每例存三类 bbox、中心线、端点、比例、层级；续线始于 fill 右端。禁伪底轨、统一长度、掩差、跨/并行；长中短均核对。

v1 shape/line 合同（字段齐全、不扩展）：

- `shape.style.fill` 仅：`"noFill"`、`{"type":"solid","color":"#RRGGBB","opacity":0..1}`，或 `{"type":"linear_gradient","angle":0..<360,"stops":[{"position":0..1,"color":"#RRGGBB","opacity":0..1},...]}`；gradient 至少 2 stops，position 严增，仅用于连续定向变色。
- shape/line `style.line`：`{"color":"#RRGGBB","width":12700,"dash":"solid","opacity":1}`；width 为 1..20116800 整数 EMU，dash 仅 `solid|dash|dot|dashDot`；另存起止/端点/层级，遮挡仍错误。
- `shape.style.effects`：`"none"` 或 `{"outer_shadow":{"color":"#RRGGBB","opacity":0..1,"blur_radius":0,"distance":0,"angle":0..<360}}`；半径/距离为非负整数 EMU。`none` 对目标 Shape/Line：保留 `p:style`、令 `a:effectRef idx=0`，清除 `spPr` 下 `effectLst/effectDag` 后写空 `effectLst`；排除表格/图片/`graphicFrame`。
- 矩形用 `rectangle`；圆角/胶囊用 `roundRect`，`style.adjustments` 为 `(0,0.5]` 单值数组，按 preview 校准；禁依赖默认值或顺带改 bbox/填充/文字。

## 图示、Connector 与重复组件

存 nodes/ports/edges/groups/component_templates。edge：`source_node+port → route/bend_points → target_port+node`；端点附边界，路径/拐点/箭头/线型/Z-order 保真，多段端点重合，不悬空、入错节点、穿节点/标签或截断；不乱拆/合并关系。

重复卡片/KPI/步骤共享尺寸/padding/圆角/基线/间距，只留例外；不漂移/自动等距。Group 不包无关项；文字/connector 独立可编辑。

## 图表

证据等级不扩 manifest。v1 无 native chart；`high|medium` 用登记的 Shape/TextBox/Line composite，`low` 且 plan 允许才最小 asset 化并留原生标签，否则 fail closed。禁造数据/分类/系列/轴/趋势。

图存 type/三类 bbox/表示法/分类与系列序/确认点/轴/刻度/gridline/legend/label/颜色/线型/fill/marker/裁剪。折线有序，无断口/突刺/串线，marker 居中；缺失值按源断开，不平滑/越界/改极值。柱条查基线/gap/overlap/堆积；饼环查序/角度/内径；散点用数值轴；组合图查主次轴/图例。

对象数、merge/边界、状态条、connector 连续性、图表映射/裁剪错误不以“整体相似”放行。
