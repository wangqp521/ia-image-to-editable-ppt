# 图片与图标

素材只来自当前页 `clean_visual_reference` 或用户原始素材；不得联网、调用 imagegen 生成替代素材、借用其他页资产，或把外部标题/标签并入图片。照片、Logo、插画、纹理和复杂装饰可用独立局部 picture；主要文字、数字和数据仍保持可编辑。满页照片/纹理可作背景，但主要内容不能整页图片化。

## 图标

图标必须从当前页视觉参考裁切为各自独立 picture，放回原 bbox；简单或复杂图标都不得用 Shape、字符、重绘 SVG 或图标库替代。编号圆点、标签、文字、bullet 和分隔线不是图标，不得混入 `icon_only`。`extract_icon_asset.py` 是唯一图标资产生成入口，每次只处理一个已测量图标；不得编写页面专用裁切脚本，也不增加对象检测、批处理状态机或第二套资产规格。

规格坐标统一为 `source_bbox=[x,y,w,h]`。bbox 必须包含完整轮廓、阴影和少量周边背景，大图标和小图标遵守同一规则；新任务固定 `padding=0`，不得在生成器里再次扩框。资产生成前查看带 bbox 的局部上下文，确认只包含目标图标且没有邻近文字、边框或分隔线。任何可见前景触边都视为坐标不足；前景触边时扩大 bbox 后重跑，不得在裁后补边或删除小连通部件。

图标裁切只允许 `alpha_isolation`，固定执行 `alpha_isolation`，不存在第二种裁切模式。它只把与裁切四边 4-connected 连通的同色背景写为透明：开放线框内部与外部连通的同色背景默认透明，封闭区域内未与外部连通的底色保持不透明；图标本体、封闭区、阴影、抗锯齿和所有小部件不做删除、重绘、改色或羽化。输出必须为同时含透明背景和可见前景的 RGBA PNG，前景不得触边，并保存 alpha channel hash。

资产尺寸必须等于 bbox 尺寸，RGB 必须逐像素一致于来源裁块；只允许改变 alpha，包括最终透明像素下的 RGB 也不得改变。不得通用阈值抠图、美化、颜色分类过滤或按面积删除连通部件。提取脚本自动检查 RGBA、透明和可见像素、前景边界、RGB 一致性及 alpha hash；任一检查失败即修正 bbox 或输入后重跑，不切换处理模式。

`modules.icons` 绑定当前视觉图路径/哈希，逐 icon 保存唯一 `icon_id/element_id/category`、instance/repeat、semantic_scope、pixel/EMU bbox、layer、source path/hash、固定 `crop_mode=alpha_isolation`、padding/background handling、当前页 `assets/icons` 内非 symlink PNG 路径/hash、alpha hash、尺寸、sharpness、validation、`native_redraw=false`、`selectable_picture_verified` 和 `object_type=picture`。validator 直接校验已声明的 `assets/icons` 路径，不得从 clean visual 父目录推导；实际像素尺寸必须等于声明尺寸，PPTX 嵌入媒体 SHA-256 必须等于当前资产哈希。生成前不写 relationship/object ID；最终每图标必须可独立选择。

当前页全部最终图标生成并通过自动校验后，运行 `create_icon_green_preview.py` 生成 `comparisons/icon-alpha-preview.png`。绿幕背景固定为 `#00FF00`，按 `icon_id` 展示最终 RGBA 资产。通过 commentary 标注 `[第 N/总页数] 图标透明效果展示（仅展示，不设审核门禁）`；每页最终图标资产集合只展示一次，无图标时不生成、不展示。展示不产生 passed/failed 结论，不等待用户或主代理确认，不作为 prebuild 或 final 证据；绿幕展示不写入 schema。展示后直接进入 prebuild。

图标资产发生变化时，以新的最终资产集合重新展示一次；未变化时不得重复展示。绿幕预览只提供过程可观察性，不得据此引入另一种裁切模式、审核循环、manifest、缓存状态或额外门禁。图标放入 PPTX 后的位置、比例和整页视觉一致性统一由后续结构校验与整页视觉审计处理。

## 非图标图片

写入 `modules.picture_framing` 并绑定 element：素材路径/hash、source/slide bbox、原始/显示比例、`contain|cover|none`、四边 crop、焦点/偏移、mask、圆角、rotation、transparency、border/shadow/reflection/glow 和 picture fill 的 stretch/tile/alignment。

优先保持来源像素和宽高比；只有来源确为图片填充时才用 picture fill。`cover` 不得无证据居中，不得裁掉主体；crop 不得带入邻近文字、图标、边框或线；不扩图、不补被裁区域。圆形保持正圆；mask/alpha 抗锯齿连续，无白边、黑边、绿幕、色晕、断裂或不透明 halo。无平铺证据不得 tile，背景不得有接缝/漏底/重复。

border/shadow/reflection/glow 只在来源可见时使用，分别记录方向、距离、blur、透明度、颜色、扩散和层级；不得套默认效果。以整页查构图、位置、焦点和层级，再以 200%–300% 查 crop/mask/alpha/圆角/边框/效果。误裁、拉伸、边缘断裂、拼接缝或效果方向错误不得通过。

纯色必须写明确 RGB/alpha；无填充边框在最终 OOXML 中必须是真正 `noFill`。不得用不透明色块掩盖透明边缘问题，也不得为空刷新 hash 改动无关图片。
