# 图片与图标

素材只来自当前页 `clean_visual_reference` 或用户原始素材；不得联网、调用 imagegen 生成替代素材、借用其他页资产，或把外部标题/标签并入图片。照片、Logo、插画、纹理和复杂装饰可用独立局部 picture；主要文字、数字和数据仍保持可编辑。满页照片/纹理可作背景，但主要内容不能整页图片化。

## 图标

图标必须从当前页视觉参考裁切为各自独立 picture，放回原 bbox；简单或复杂图标都不得用 Shape、字符、重绘 SVG 或图标库替代。编号圆点、标签、文字、bullet 和分隔线不是图标，不得混入 `icon_only`。`extract_icon_asset.py` 是唯一图标资产生成入口，每次只处理一个已测量图标；不得编写页面专用裁切脚本，也不增加对象检测、批处理状态机或第二套资产规格。

规格坐标统一为 `source_bbox=[x,y,w,h]`。bbox 本身必须包含完整轮廓、阴影和少量周边背景，大图标和小图标遵守同一规则；新任务固定 `padding=0`，不得在生成器里再次扩框。资产生成前必须查看带 bbox 的局部上下文，确认只包含目标图标且没有邻近文字、边框或分隔线，将 `roi_context_400` 写为 passed。任何可见前景触边都视为坐标不足，先扩大 bbox 再重跑；前景触边时不得回退到 `background_preserved`，也不得在裁后补边或删除小连通部件。

每个图标只选一种终态模式，但共用一条主流程：默认先执行 `alpha_isolation`。它只把与裁切四边 4-connected 连通的同色背景写为透明：开放线框内部与外部连通的同色背景默认透明，封闭区域内未与外部连通的底色保持不透明；图标本体、封闭区、阴影、抗锯齿和所有小部件不做删除、重绘、改色或羽化。输出必须为同时含透明背景和可见前景的 RGBA PNG，前景不得触边，并保存 alpha channel hash。只有 bbox 已确认正确且透明化造成虚化、色晕或边缘损失，或阴影、浅色轮廓、纹理、低分辨率细节与底色无法稳定分离时，才显式回退 `background_preserved`，不得继续放宽阈值强制透明，也不得将整页图标统一硬编码为保底模式。

`background_preserved` 按同一 bbox 原样保留图标及原始局部底色，保持原始像素尺寸和宽高比，不预缩放、不锐化、不得羽化、不得阈值抠图、改色或重建底色。资产只允许 RGB PNG 或 alpha 全为 255 的 RGBA PNG；渐变、纹理和阴影必须原样保留。该模式必须逐图标写非空且具体的 `fallback_reason`，以及 `background_handling=preserved_source_patch`、`semantic_scope=intentional_composite`、`alpha_mask_sha256=null`；图片仍可单独选择，但移动时会连同局部底色移动，作为 P2 可编辑性限制披露。

无论采用哪种模式，资产尺寸都等于 bbox 尺寸，资产 RGB 必须逐像素一致于来源裁块；`alpha_isolation` 只允许改变 alpha，包括最终透明像素下的 RGB 也不得改变。不得通用阈值抠图、美化、颜色分类过滤或按面积删除连通部件。

`modules.icons` 绑定当前视觉图路径/哈希，逐 icon 保存唯一 `icon_id/element_id/category`、instance/repeat、semantic_scope、pixel/EMU bbox、layer、source path/hash、crop mode、`fallback_reason`、padding/background handling、当前页 `assets/icons` 内非 symlink PNG 路径/hash、条件式 alpha hash、尺寸、sharpness、inspection、validation、`native_redraw=false`、`object_type=picture`。validator 直接校验已声明的 `assets/icons` 路径，不得从 clean visual 父目录推导；实际像素尺寸必须等于声明尺寸，PPTX 嵌入媒体 SHA-256 必须等于当前资产哈希。生成前不写 relationship/object ID；最终每图标独立可选择，`roi_context_400/source_400/asset_400/placement_400` 均 passed。

生成全部图标资产并写入路径/hash 后、prebuild 前运行 `create_icon_crop_review.py`。每个图标按 `icon_id` 展示“带 bbox 的局部上下文 / source/asset 400%”三栏证据，资产侧固定使用 `#00FF00` 绿幕。工具以真实源图、图标顺序与 `icon_id`、`source_bbox`、padding、crop mode、真实资产 SHA-256、背景处理/fallback、alpha mask 和固定渲染参数生成 `icon_manifest_sha256`；命中 PNG metadata 时返回 `reused=true`，不得重建 contact sheet。`spec_sha256` 只用于追踪，非图标字段变化不得使旧绿幕复核图失效。真实源图或任一图标依赖变化、metadata 缺失/损坏时必须重建；缓存命中也不得跳过真实输入校验。

通过 commentary 以 `[第 N/总页数] 图标裁切绿幕复核` 展示本会话尚未展示的当前 `icon-crop-review.png`；无图标时不生成也不展示。将工具返回的 output path/hash、`icon_manifest_sha256` 和 `inspection=passed` 写入 `modules.icons.crop_review_evidence`；prebuild 对三种 profile 统一重算 manifest，来源、bbox、padding、crop mode、fallback、alpha 或 asset 任一改变时旧证据立即失效。缓存不替代首次展示和审查。放大只供审查，不写回资产或 PPTX；先确认 ROI 语义正确，再确认轮廓完整、背景处理正确、开放线框/封闭区符合连通规则且没有白边、色晕或漏裁，才运行 prebuild。bbox 不足先扩大重跑；仅透明结果本身损坏时才改用 `background_preserved`。回填后仍以 `placement_400` 检查位置、比例、清晰度和局部底色接缝。

## 非图标图片

写入 `modules.picture_framing` 并绑定 element：素材路径/hash、source/slide bbox、原始/显示比例、`contain|cover|none`、四边 crop、焦点/偏移、mask、圆角、rotation、transparency、border/shadow/reflection/glow 和 picture fill 的 stretch/tile/alignment。

优先保持来源像素和宽高比；只有来源确为图片填充时才用 picture fill。`cover` 不得无证据居中，不得裁掉主体；crop 不得带入邻近文字、图标、边框或线；不扩图、不补被裁区域。圆形保持正圆；mask/alpha 抗锯齿连续，无白边、黑边、绿幕、色晕、断裂或不透明 halo。无平铺证据不得 tile，背景不得有接缝/漏底/重复。

border/shadow/reflection/glow 只在来源可见时使用，分别记录方向、距离、blur、透明度、颜色、扩散和层级；不得套默认效果。以整页查构图、位置、焦点和层级，再以 200%–300% 查 crop/mask/alpha/圆角/边框/效果。误裁、拉伸、边缘断裂、拼接缝或效果方向错误不得通过。

纯色必须写明确 RGB/alpha；无填充边框在最终 OOXML 中必须是真正 `noFill`。不得用不透明色块掩盖透明边缘问题，也不得为空刷新 hash 改动无关图片。
