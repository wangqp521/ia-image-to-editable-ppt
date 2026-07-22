# 图形与图示

所有对象写入 schema v2 对应 `modules.graphics/diagram/chart` 并引用统一 `element_id`；不得预填最终 OOXML ID。先记录语义数量、bbox、结构、样式、层级和可编辑性，再决定由多少 Shape/Line 实现。

## 表格、矩阵与框线

规则行列、单元格与合并关系明确时用原生表格；不规则分区、局部边界、父子组或跨行对象用独立 Shape/Line/TextBox 自绘矩阵。不得把规则表格拆成漂浮文本框，也不得因外观像网格就强制原生表格。

记录真实行/列数、非均匀行高/列宽、merge row/column span、cell fill/margin/align、四边存在性及线的起止/颜色/宽度/透明度/虚实。禁止默认完整网格、给无线区补线、让局部线贯穿整行列。父项/子项/明细/状态的合并区、组内/组间线和填充归属逐范围保存；合并区内部横线不得穿越，视觉换行不得自动产生横线。

闭合虚框按一个语义对象计数，保存完整 bbox、线宽、颜色、虚线节奏和层级；不能按短线数量计数或漏掉边界。

## 状态条、圆角、线和填充

只有可见同高底轨才用 `track_plus_fill`；只有细线从填充末端延续时用 `fill_plus_continuation_line` 且 `track_bbox=null`。每实例独立记录 fill/track/continuation bbox、中心线、端点、比例和层级。续接线必须从该实例 fill 右端开始；不得补伪底轨、统一长度、用同宽深色容器掩盖差异、跨行或合并多行。长/中/短实例都要局部核对。

普通矩形用 `rect`；圆角/胶囊用 `roundRect` 并显式设置 `shape.adjustments[0]`，`corner_adjustment` 在 `(0,0.5]`，根据当前 preview 校准。不得依赖默认空 adjustment，也不得顺带改变 bbox/填充/文字。

每条线保存实际起止、宽度、颜色、透明度、虚线、端点和层级；存在但被遮挡也算错误。纯色记录 RGB/alpha，真正无填充用 `noFill`，不得用无颜色 `solidFill`。只有连续且方向明确的颜色变化才用渐变，并保存类型、角度和 stops；压缩噪声不算渐变证据。

## 图示、Connector 与重复组件

记录 nodes/ports/edges/groups/component_templates。edge 固定表达 `source_node+port → route/bend_points → target_port+node`：端点附着正确边界，路径类型、拐点、箭头、线型和 Z-order 与来源一致；多段相邻端点严格重合，不悬空、不深入错误节点、不误穿节点/标签、不被背景截断。一个逻辑 edge 不拆成无关系线段，多条关系不合并。

重复卡片/KPI/步骤共享来源定义的尺寸、padding、圆角、基线和间距，只保留真实例外；不得累计漂移或自动等距。可 Group 逻辑组件，但不得用巨型 Group 包住无关对象，关键文字和 connector 保持独立可编辑。

## 图表

先记录证据等级：`high` 可确认类型/分类/系列/数据时用 native chart；`medium` 只确认可见几何时用 Shape/TextBox/连续 path/marker；`low` 才把复杂图表最小范围图片化，标题、图例和可识别标签仍可编辑。任何等级都不得编造数据、分类、系列、轴范围或趋势。

每图保存 type、source/slide/plot bbox、表示法、分类/系列顺序、可确认点、轴/零轴/刻度/gridline、legend/label、颜色/线型/fill/marker 和裁剪。折线系列用有序点；相邻路径无断口/突刺/跨系列，marker 位于点中心，缺失值只在来源位置断开，直线不擅自平滑，曲线不越界或改变极值。柱/条查基线、gap/overlap/堆积；饼/环查扇区顺序/角度/内径；散点用数值轴；组合图查主次轴和图例映射。

对象数量、merge/边界、状态条表示、connector 连续性、图表映射或裁剪任一错误均为具体差异，不得用“整体相似”放行。
