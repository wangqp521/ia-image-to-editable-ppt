# 视觉审计与交付

## 固定模式与审核职责

`verification_profile` 必须显式写入每页规格并在批次内固定：用户未指定时写 `rapid`，明确要求独立复核时写 `reviewed`。不得依赖脚本默认值、在运行中自动升降级；多页合并拒绝缺失或混合模式。

- `rapid`：主代理完成一次正式语义判断，最多一次集中修复；修复后只做确定性闭环，不启动第二次语义判断。
- `reviewed`：先完整执行 rapid 基础流程，包括主代理检查和最多一次 rapid 集中修复，但暂不写 rapid 终态；再把当前 PPTX 和当前证据交给独立 reviewer。
- reviewer round 1 直接通过即停止；发现可修复 P0/P1 时允许一次 reviewer 驱动的集中修复，重建新哈希证据后进入 round 2。
- round 2 是终局；只有 `passed` 才通过，其他结果均写入 `reviewed_failed`，不得再次修复或开启第三轮。

两个模式共享同一复刻、prebuild、compiler、render、结构、背景、tripwire 和 final 身份合同。reviewed 只在 rapid 基础流程后增加独立复核，不增加新的证据强度子模式。

## 当前哈希的确定性证据

统一使用稳定版 LibreOffice。`preflight_runtime.py` 锁定 `soffice`、`pdftoppm`、`pdffonts`、`pdftotext` 和 fontconfig 的绝对路径、版本与 SHA-256，拒绝开发版或不完整环境。`render_preview.py` 在隔离 profile 中执行唯一 `PPTX → PDF → PNG`，核对单页 960×540 point PDF、resolved fonts 和非空 1920×1080 preview。正常路径不双重渲染；仅明确的沙箱 `SIGABRT` 可重试一次，仍失败即停止。

每个当前 PPTX 哈希必须有且只有一组闭包证据：

1. `build-spec-snapshot.json` 与 `prebuild-validation.json`；
2. `build-report.json` 和当前 PPTX；
3. `runtime-preflight.json`、`render-report.json`、PDF、font report、preview；
4. `structure-validation.json` 与 `background-contract.json`；
5. `visual-diff.json`、overlay、diff 与当前 profile 所需 region evidence；
6. `reviewed` 的当前 raw reviewer response；
7. `final-validation.json`。

任一 spec、PPTX、runtime 或 producer 身份变化，所有下游证据失效。SHA-256 只证明身份，不是视觉分数。tripwire 只单向阻断：有基线且触发则失败；无基线固定为 `available=false, triggered=null, reason=no_approved_baseline`，不能据此自动通过。

## 一次完整检查与批量修复

正式语义审核按以下顺序检查，并一次返回全部差异：画布/mapping → regions/层级 → 对象数量与几何 → 文字、Text Run、Paragraph、bullet、数字与单位 → 表格/矩阵 → 图形、connector、diagram、chart → picture crop、icon、mask、alpha → background 与局部细节。

structure report 中的 `TEXT_RUN_STYLE_MISMATCH` 是非阻断诊断，final/reviewer 通过当前 structure artifact 的 SHA-256 原样绑定，不复制或改写。审核者结合 preview 判断其视觉影响并按既有 P0/P1/P2 合同归类；不得仅因 warning 存在就自动阻断，也不得因 `valid=true` 忽略可见的高保真差异。Run 结构错误仍由 structure gate 直接阻断。

P0 包括 PPTX 不可用、页数或比例错误、核心内容缺失、主要内容不可编辑和数据编造；P1 包括数量、比例、结构、fill、字号/换行、行距/段距、框内垂直位置、Text Run、bullet、crop、connector、图表或关键装饰错误；P2 仅限不改变内容、结构、关系和可编辑性的字体 fallback、轻微色差、线宽或不超过确定性容差的 renderer 近似。

第一个正式结果为 `passed` 时立即停止。若为 `changes_required`，把全部 P0/P1 映射到 `modules.high_risk.items`，按共同根因修改同一页面的 `prepare_spec.py` 一次并重新生成规格。PPTX 哈希变化后，以新哈希从 prebuild/snapshot 起重跑 build/report、render、structure、background 与 visual diff；runtime 身份未变时复用批次 preflight，发生变化时先重建 preflight。不得直接修改生成的 Layout 内容、只重跑有利指标、沿用旧报告、保留另一份 PPTX 作为回退，或逐项边看边改。

`rapid` 修复后只根据新确定性闭包关闭可客观验证的问题；构图平衡、复杂装饰、主观色彩观感、非规则几何等无法被确定性证据关闭的 P0/P1 必须使 `rapid_validation_failed`。reviewed 的 rapid 基础阶段最多修复一次，reviewer round 1 之后另有且只有一次 reviewer 驱动的集中修复；未使用的 rapid 修复额度不转移。只有全部映射项 `result=passed` 且证据绑定新哈希，才可生成 round 2 prompt。

## reviewer context、prompt 与 raw response

`create_reviewer_prompt.py` 只读当前工作规格并重新收集当前 artifacts。它生成 canonical context 与 `review_context_sha256`，绑定 `page_id`、round、profile、content spec hash、source、build snapshot/report、PPTX、preview/render/runtime、structure、background、visual diff 和排序后的 region evidence。不得手写、改写或复用 prompt，也不得让 reviewer 读取 context 之外的文件。

reviewer 使用全新只读上下文，不运行 producer、不修改文件，只返回一个 JSON object，精确包含九个字段：`response_schema_version`、`review_context_sha256`、`page_id`、`review_round`、`verification_profile`、`decision`、`coverage`、`findings`、`p2_disclosures`。coverage 精确覆盖七类；decision 只允许 `passed|changes_required|not_reviewable`；finding evidence 必须来自当前 context 允许的绝对路径。

raw response 是唯一持久化的 reviewer 产物。主代理按收到的 UTF-8 JSON 原样保存，不修补字段、不生成摘要副本。缺失或无效均按 not_reviewable，并消耗本轮；context hash、page、round、profile、coverage、evidence 或语义不匹配同样如此。`passed` 不得包含 P0/P1 或 `not_reviewable` coverage。

## final 与不可变终态

final 只读。它只重算文件哈希、解析当前 JSON、验证跨报告绑定、重建 review context 并验证 raw response；不得导入或运行 compiler、renderer、结构/背景/视觉 producer，不得创建、修补或覆盖证据。`rapid` 禁止携带 reviewer context/response；`reviewed` 必须绑定当前 raw response。

final 通过后禁止写入 PPTX。任何 PPTX 或终态字段改动都必须使旧 final 失效，并从新哈希重新建立完整闭包。`reviewed_failed` 只表示审核门禁未通过，不得阻止交付当前 PPTX。失败时必须明确标注未通过原因：P0 为“当前 PPTX 可能不可用”，P1 为“未通过视觉门禁的可编辑草稿”，`not_reviewable` 为“证据不可审查”。

## 多页合并与交付

逐页串行并标注 `[第 n/总页数]`。单页 prebuild/build 失败时不占位、不合并；已有 PPTX 但 final 失败的页面只能作为明确标注的草稿单独交付。合并输入必须逐页配对当前 PPTX、spec 与 `valid=true, errors=[]` 的 final report，核对 profile、delivery status、content spec、runtime、capability、structure 和 PPTX SHA-256 后按原序导入。merger 不重跑单页 producer 或 validator；合并完成后仅对临时 merged deck 运行一次结构验证，通过才原子替换输出。

最终提供可编辑 PPTX、当前 preview/diff、structure/final、模式要求的 regions 和 raw response，并披露 P2、字体 fallback、缺页与未验证项。round 2 未通过时不再修复，但必须交付当前 PPTX、预览、raw response、final 报告和未解决问题；该页必须单独交付，不得进入成功合并成品。缺证据、旧哈希、tripwire 触发、开放 P0/P1 或结构/final 失败时不得称完成。
