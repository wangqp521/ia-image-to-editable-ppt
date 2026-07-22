# 测量与布局

## 事实源与 preflight

无法确认污染时使用 `direct_to_reconstruction`；只有可见拍摄透视/弯曲、反光/摩尔纹/环境背景、浏览器或聊天外壳、浮层/通知/遮挡、拼接或非内容边界时才用 `clean_with_imagegen`。分类理由写可观察事实，不按扩展名或上传渠道判断。清洗只允许一次，提示词固定为“请根据附件生成图片，要求高度还原，16:9，复刻源图片风格。”不得追加美化、改字或风格要求。

`content_reference` 唯一裁决文字、数字、单位、数量、分组和语义；`clean_visual_reference` 唯一裁决坐标、比例、颜色、字体观感、图标、纹理和层级。直通页两者均指向原图；清洗页内容仍服从原图，清洗图改变的内容不得进入 PPTX。每页不得借用其他页事实。

preflight 记录当前输入绝对路径/SHA-256、像素尺寸、alpha、页边界、分类、依赖和页目录。先运行 `create_coordinate_overlay.py`；需要取色或确认边界时对明确点/bbox 运行 `inspect_image_region.py`。写规格前通过 commentary 以 `[第 N/总页数] 坐标定位图` 展示当前 `coordinate-overlay.png`；同一来源 SHA-256 下每页只展示一次。来源图改变时，旧坐标定位图立即失效，必须重新生成并展示。工具输出是测量证据，不是新的事实源。

## 唯一 schema v2 规格

生成前只维护 `work/page-reconstruction.json`：`schema_version/page_id/session_reuse/content_reference/clean_visual_reference/canvas/activated_modules/modules/regions/elements/reading_order/visual_gate/editability_gate`。`source_bbox` 使用视觉参考图 pixel；`slide_bbox` 与 typography 坐标只用 EMU。module 只引用正式 `element_id`，已激活 module 必须非空；`reading_order` 必须覆盖全部 elements，每个 element 至少归属一个 region。不得预填最终 OOXML ID、另建平行对象清单或让构建脚本维护第二套内容/坐标。

主代理提供元素、内容、`source_bbox`、layer、关系和顺序；`scaffold_reconstruction.py` 只算 EMU、bbox 与 hash。冲突返回 `SPEC_DERIVED_FIELD_CONFLICT`，缺字段返回 `MISSING_REQUIRED_FIELD`；报告不是事实源。

每个实际对象记录数量、pixel/EMU bbox、结构关系、样式、层级、可编辑性和 `high|medium|low` confidence。先判断视觉事实和语义对象，再选绘制方式；不能根据代码方便反推原图。运行 prebuild 校验通过后才能生成。

## 画布、区域与关系

可信 16:9 页面按内容边界映射；其他比例使用等比 contain 和明确 offset，禁止拉伸。`canvas` 记录原图/视觉图尺寸、`page_frame_bbox`、slide EMU、mapping、offset、背景和内容范围。

`regions` 只记录实际存在的标题、主要内容、表格/图表、说明、图例、页脚等区域及 source/slide bbox、padding、层级、阅读顺序和 element_ids。不得套模板、补区域、合并视觉上分离区域或自动平均栏宽/行高/间距。

`anchors/relationships/layout_invariants/density_targets` 保存原图的边界、基线、中心线、包含/附着/重叠、阅读顺序、层级、留白、区域比例和对象/文字/线条/色彩密度。原图轻微不齐或非均匀间距应保留，不得为了整齐改变视觉重心。分组背景块逐个记录 bbox、颜色和层级，不能只生成表头色。

## 生成与修正

生成顺序：页边界与映射 → 主要区域 → 锚点/层级/阅读顺序 → elements → 局部文字/图形/图片。全局缩放或区域比例错误时先修全局；局部差异只修目标及相邻受影响对象。不得用缩小字号、硬换行或移动个别对象掩盖区域错误，也不得用整页图片兜底。

图片保持宽高比；`cover` 必须有焦点/偏移证据，不能裁掉主体。圆形保持正圆。原图无线、无渐变、无效果时不得补造。`editable_object_count` 只作结构证据，不能证明质量。
