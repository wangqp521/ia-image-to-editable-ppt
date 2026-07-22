# Image to Editable PPT

## 一句话介绍

`ia-image-to-editable-ppt` 把一张或多张幻灯片图片高保真还原为主要文字、数据和基础结构可编辑的 16:9 PowerPoint。

快速调用：

```text
使用 $ia-image-to-editable-ppt，把上传的图片还原成可编辑 PPTX；看不清的内容不要猜。
```

## Skill 是什么

这是一个把图片型幻灯片逆向重建为可编辑 PPTX 的 Skill，适用于幻灯片截图、从 PPT/PDF 导出的页面图片、扫描页和拍摄的演示文稿页面，支持单页转换和多页顺序合并。

它遵循以下优先级：内容事实正确 → 视觉高保真 → 主要内容可编辑 → 多页顺序正确。它不会用整页原图加少量文本冒充可编辑页面，也不会自行美化或补造无法确认的内容。

## 能做什么

- 将标题、正文、标签和数字重建为可编辑文本框；
- 保留局部字号、颜色、粗体、斜体和上下标等文字样式；
- 将列表重建为 PowerPoint 原生项目符号；
- 将表格、矩阵、合并单元格和局部边线重建为可编辑结构；
- 将矩形、圆角框、状态条、连接线和基础图示重建为原生形状；
- 保留必要的图片裁剪、蒙版、透明度、边框和阴影效果；
- 将照片、Logo、图标、插画、纹理和复杂装饰保留为最小范围的独立图片；
- 对转换结果执行结构校验和视觉差异检查；
- 将无法可靠确认的内容列为未验证项，而不是自行猜测。

## 验证模式

同一批图片只能使用一种验证模式，开始处理后不会自动升级或降级。

| 模式 | 触发方式 | 适用场景 |
|---|---|---|
| `rapid` | 默认 | 日常转换，包含结构校验和整页视觉差异检查 |
| `reviewed` | 明确要求“独立复核” | 重要汇报材料，增加独立视觉复核和必要区域证据 |
| `strict` | 明确要求“严格审核” | 高要求交付，使用最完整的区域证据和审核流程 |

验证未通过时仍会保留当前产物并明确标注；严重失败时 PPTX 可能不可用。Skill 不会偷偷降低验证标准或伪造通过状态。

## 单页使用

上传一张图片，然后输入：

```text
使用 $ia-image-to-editable-ppt，把这张图片还原成单页可编辑 PPTX。
```

推荐写法：

```text
使用 $ia-image-to-editable-ppt，把上传的这张图片高保真还原成单页可编辑 PPTX。

要求：
1. 保持原始内容、配色和版式；
2. 主要文字、数字、表格和基础图形可编辑；
3. 照片、Logo 和复杂装饰可以保留为局部图片；
4. 看不清的内容不要猜，列为未验证项；
5. 使用 rapid 模式；
6. 输出文件名为“单页还原_可编辑版.pptx”。
```

也可以直接提供本地图片路径，例如 `/path/to/slide.png`。需要更高验证等级时，在提示词中明确写“独立复核”或“严格审核”。

## 多页使用

上传多张图片并说明页序，然后输入：

```text
使用 $ia-image-to-editable-ppt，把这些图片按上传顺序逐页还原，
并合并成一份可编辑的 16:9 PPTX。
```

推荐写法：

```text
使用 $ia-image-to-editable-ppt，把上传的 01—08 页图片按顺序还原成一份可编辑 PPTX。

要求：
1. 全部页面使用 reviewed 独立复核模式；
2. 保持原图内容、配色、字号层级和相对位置；
3. 主要文字、数字、表格、流程图和基础图形可编辑；
4. Logo、照片和复杂装饰可以使用局部图片；
5. 看不清的内容不要补造，列入未验证项；
6. 最终文件名为“项目汇报_可编辑版.pptx”。
```

多页任务按输入顺序逐页处理。视觉复核未通过但结构有效的页面可以保留失败标记参与合并；结构无效、必要证据缺失或文件对应关系不一致时，可能无法完成最终合并。

## 需要提供的信息

- 一张或多张源图片；
- 多页任务的正确页序；
- 验证模式：默认 `rapid`，也可以选择 `reviewed` 或 `strict`；
- 希望使用的最终文件名；
- 必须优先保持可编辑的内容，例如表格、流程图或图表；
- 字体、品牌色或版式方面的特殊要求；
- 对模糊内容的处理要求；没有明确授权时，默认不补造；
- 如果有原始 Logo、照片、图标或字体文件，建议一并提供。

建议使用原始分辨率 PNG 或高质量图片，避免聊天软件二次压缩。拍摄图片应尽量正对页面，减少透视、反光、模糊和遮挡。

## 最终交付物

- 单页或多页可编辑 `.pptx`；
- 当前页面预览和源图对照；
- 结构与视觉校验结果；
- 当前验证模式要求的复核证据；
- 字体替代、视觉近似和未验证内容说明；
- 多页任务还会交付按输入顺序合并的最终演示文稿。

验证通过和未通过的版本会使用清晰标签区分，例如“快速校验版”“独立复核通过版”“完整视觉门禁通过版”或对应的“未通过版”。

## 标准自动化流程

主代理先确认内容和元素语义，再依次运行：

```bash
python3 scripts/scaffold_reconstruction.py --spec work/page-reconstruction.json --preflight-report work/preflight-runtime.json --in-place --report work/scaffold-report.json
python3 scripts/extract_assets.py --spec work/page-reconstruction.json --assets-dir assets --in-place --report work/assets-report.json
python3 scripts/create_asset_crop_review.py --spec work/page-reconstruction.json --output comparisons/asset-crop-review.png --report work/asset-review-report.json
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage prebuild --output work/prebuild-report.json
python3 scripts/build_pptx_from_spec.py --spec work/page-reconstruction.json --prebuild-report work/prebuild-report.json --output candidate.pptx --build-report work/build-report.json
python3 scripts/validate_pptx.py candidate.pptx --expected-slides 1 --spec work/page-reconstruction.json --build-report work/build-report.json --output work/structure-report.json
```

三个执行报告都不是第二套规格；修改和返工以 `page-reconstruction.json` 为准。运行 `--help` 查看各工具参数。工具遇到不支持的表示、旧 hash 或不安全透明分离时返回结构化错误并停止，不会静默截图化。

## 系统运行时依赖

| 依赖 | 用途 |
|---|---|
| Python 3 | 执行图片处理、PPTX 生成和校验工具 |
| LibreOffice | 将 PPTX 渲染为可检查的页面预览 |
| Poppler | 将 PDF 转换为图片并检查字体信息 |
| Pillow | 图片读取、测量、裁切和视觉差异分析 |
| python-pptx | 创建和编辑 PowerPoint 文件 |
| 可用字体环境 | 提高文字宽度、换行和页面渲染的还原精度 |

该 Skill 不依赖外部 OCR 服务、云端 OCR API、API Token、Node.js、npm 或数据库。

## 使用限制与注意事项

- 输出固定为 16:9 PPTX；
- 内容事实正确优先于视觉相似度；
- 不会自行美化、平均布局或补造隐藏内容；
- 不允许用整页原图加少量文本冒充可编辑页面；
- 照片、Logo、插画、纹理和复杂装饰可以保留为局部图片；
- 模糊、遮挡、透视严重或分辨率过低会降低文字与结构还原精度；
- 缺少原字体可能导致字宽、换行和渲染差异，并会在交付说明中披露；
- 同一批页面不能混用验证模式；
- 验证失败时会交付当前产物并明确标注；严重失败时 PPTX 可能不可用，且不会自动降低标准。
