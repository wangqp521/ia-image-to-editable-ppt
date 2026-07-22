# Image to Editable PPT

`ia-image-to-editable-ppt` 只做一件事：把一张或多张幻灯片图片高保真复刻为主要内容可编辑的 16:9 PPTX。

它保留文字、数字、Text Run、原生项目符号、表格/矩阵结构、状态条、图形和连接线的合理可编辑粒度；照片、Logo、图标、插画、纹理和复杂装饰按最小必要范围保留为独立局部图片。它不会把整页原图作为可见底图冒充可编辑 PPT，也不会扩展成独立平台或通用视觉系统。

## 使用方式

```text
使用 $ia-image-to-editable-ppt，把 /path/slide.png 高保真转换为可编辑 PPTX。
```

多页时按输入顺序逐页完成再合并：

```text
使用 $ia-image-to-editable-ppt，把 /path/01.png、/path/02.png、/path/03.png
按顺序转换并合并为一份可编辑 PPTX。
```

## 质量流程

每页采用一个自动 preflight、一道独立视觉门禁、一道自动结构门禁：

1. 测量当前原图的路径、哈希、画布、区域、颜色和必要字体，先写 schema v2 规格并通过 prebuild。
2. 主代理作为唯一写入者生成和修正 PPTX。
3. 当前 PPTX 重新渲染 preview，生成双/三联图、overlay、diff 和区域 200% 对照；独立只读视觉审查员判断 P0/P1/P2。
4. 视觉通过后运行现有 `validate_pptx.py` 检查 16:9、可编辑对象、Text Run、原生列表、图片化风险和规格一致性。
5. 任何结构修正都重新触发视觉审查；final 规格校验通过后才交付或合并。

相似度和边缘指标只用于定位差异，不能自动批准视觉质量。没有用户批准的合格基线时，不临时设置阈值。

## 交付

至少包括可编辑 PPTX、当前 preview、双联图或三联图、overlay、diff、`visual-diff.json`、结构/final 校验结果，以及必要的 P2 近似或字体 fallback 说明。
