# 测量与布局

## 事实源与 preflight

污染无法确认用 `direct_to_reconstruction`；仅见拍摄透视/弯曲、反光/摩尔纹/环境背景、浏览器/聊天外壳、浮层/通知/遮挡、拼接或非内容边界时用 `clean_with_imagegen`。理由只写可见事实，不按扩展名/渠道。清洗仅一次，提示词固定“请根据附件生成图片，要求高度还原，16:9，复刻源图片风格。”，不得美化、改字或改风格。

`content_reference` 唯一裁决文字/数字/单位/数量/分组/语义；`clean_visual_reference` 唯一裁决坐标/比例/颜色/字体观感/图标/纹理/层级。直通页均指原图；清洗页内容仍服从原图，清洗改动禁入 PPTX；页间不借事实。

preflight 绑定输入路径/hash、尺寸、边界。运行 `create_coordinate_overlay.py`，按需运行 `inspect_image_region.py`。写规格前通过 commentary 以 `[第 N/总页数] 坐标定位图` 展示 PNG；同一来源 SHA-256 每页一次。将 overlay path/hash、source hash、grid、manifest、`inspection=passed` 写入 `modules.page_layout.coordinate_overlay_evidence`。来源或 grid 改变即重建展示；各 profile 不得跳过。工具证据不是新事实源。

## 唯一 schema v2 规格

生成前仅维护 `work/page-reconstruction.json`：`schema_version/page_id/session_reuse/content_reference/clean_visual_reference/canvas/activated_modules/modules/regions/elements/reading_order/visual_gate/editability_gate`。`page_id` 须精确为 `page-NNN`；从 `page-001` 按交付页序递增，禁用目录/attempt/标题。`source_bbox` 用视觉图 pixel；`slide_bbox` 与 typography 坐标只用 EMU。module 只引用正式 `element_id` 且激活项非空；`reading_order` 覆盖全部 elements，每个 element 至少属一个 region。禁填 OOXML ID、平行对象清单或第二套内容/坐标。

`modules.representation_plan.items[]` 是每个来源语义事实进 compiler 前的测量结论：存事实/bbox/必需性、`native|composite|asset`、所需可编辑性、fallback policy、绑定 element、理由、coverage、非空证据。先定表示法再写 element；编写期反复跑等价 `authoring`（非门禁）；冻结后正式 prebuild 覆盖全部 element。计划非第二 IR，禁构建后补写。

每个实际对象记录数量、pixel/EMU bbox、结构关系、样式、层级、可编辑性和 `high|medium|low` confidence。先判断视觉事实和语义对象，再选绘制方式；不能根据代码方便反推原图。冻结后只运行一次正式 prebuild，通过才生成。

## 画布、区域与关系

可信 16:9 页面按内容边界映射；其他比例使用等比 contain 和明确 offset，禁止拉伸。`canvas` 记录原图/视觉图尺寸、`page_frame_bbox`、slide EMU、mapping、offset、背景和内容范围。

`regions` 只记录实际存在的标题、主要内容、表格/图表、说明、图例、页脚等区域及 source/slide bbox、padding、层级、阅读顺序和 element_ids。不得套模板、补区域、合并视觉上分离区域或自动平均栏宽/行高/间距。

`anchors/relationships/layout_invariants/density_targets` 保存原图的边界、基线、中心线、包含/附着/重叠、阅读顺序、层级、留白、区域比例和对象/文字/线条/色彩密度。原图轻微不齐或非均匀间距应保留，不得为了整齐改变视觉重心。分组背景块逐个记录 bbox、颜色和层级，不能只生成表头色。

## 生成与修正

顺序：页边界与映射 → 主要区域 → 锚点/层级/阅读顺序 → elements → 局部文字/图形/图片。全局缩放/区域比例错先修全局；局部仅修目标及相邻受影响对象。禁用缩小字号/硬换行/移动单项掩盖区域错，禁整页图片兜底。

图片保宽高比；`cover` 须有焦点/偏移证据，禁裁主体。圆形须正圆。原图无线/渐变/效果时禁补造。`editable_object_count` 仅作结构证据，不证明质量。
