# 视觉审计与交付

## 固定验证模式合同

`verification_profile` 是项目级固定模式，默认 `rapid`；用户明确提出“独立复核”才用 `reviewed`，明确提出“严格审核”才用 `strict`。运行中不得自动升级、降级或从 `reviewed` 进入 `strict`，多页合并必须拒绝混合模式。

| 模式 | 必需视觉证据 | reviewer | 成功状态 |
|---|---|---|---|
| `rapid` | 当前 preview、对照图、overlay、diff、`visual-diff.json`；不生成 regions 200% 证据 | 不启动独立 reviewer，visual status 为 `not_independently_reviewed` | `rapid_validated` |
| `reviewed` | rapid 全部证据 + finding、高风险对象和审查所需的必要区域 200% 证据 | 全新上下文只读 reviewer，最多 2 轮 | `reviewed_passed` |
| `strict` | 全页证据 + 完整 regions 200% 证据 + accepted/candidate 证据链 | 全新上下文只读 reviewer，最多 2 轮 | `strict_gate_passed` |

三个模式共享复刻、prebuild、结构、tripwire、对象身份和失败诚实性。profile 只控制终态证明成本，不得降低构建前输入质量。

### `rapid` 快速校验

主代理运行 `create_visual_diff.py --profile rapid`，确认整页 preview、对照图、overlay、diff、全页指标和当前哈希一致；不启动独立 reviewer，不生成 regions 200% 证据。通过结构门禁、visual-diff schema、tripwire 和 final 后写 `rapid_validated`；失败写 `rapid_validation_failed`。该状态只表示快速校验完成，不得表述为“独立复核通过”或“严格审核通过”。

### `reviewed` 独立复核

每轮 reviewer 前结构必须通过；只为实际 finding、当前高风险对象及 reviewer 判断所需位置生成必要区域 200% 证据。最多 2 轮，第 1 轮通过即停止；第 2 轮仍有 P0/P1 或 `not_reviewable` 时写 `reviewed_failed`，不得进入 `strict`。成功写 `reviewed_passed`。修复仍需保持 accepted 质量下限，但不强制 strict 的完整 regions 与逐 candidate 全证据链。

### `strict` 严格审核

以下 accepted/candidate、证据复用、第二轮准入、独立 reviewer 和终态身份规则均为 strict 完整合同。它要求完整 regions 200% 证据、唯一 `candidate.pptx`、accepted 是质量下限，并对 source、PPTX、spec、preview、visual diff、regions、validator、reviewer 与运行依赖做完整哈希绑定；成功写 `strict_gate_passed`，失败写 `strict_gate_failed`。

## 三个检查点

1. **自动 preflight/prebuild：** 规格只描述当前页；`validate_reconstruction_spec.py --stage prebuild` 失败不得生成。
2. **自动结构门禁：** 每轮视觉审查前用 `validate_pptx.py --expected-slides 1 --spec ...` 检查当前 PPTX；结构写入修正后重新校验，直至通过。
3. **独立视觉门禁：** 主代理生成证据，全新上下文视觉子代理只读判断；指标不能自动批准。终态 reviewer 通过后不得再写入 PPTX，最后运行 schema v2 final 校验。

条件 module 是复刻能力，不是额外审批。用户反馈、圈选、当前高风险对象或门禁差异写入唯一 `modules.high_risk.items`；从未触发时不建空清单，不创建第二套差异文件、状态机或修复历史。

## 修复候选与当前视觉证据

第一轮 reviewer 后，最近一次通过当前阶段门禁的 PPTX 记为 accepted；accepted 是质量下限。每批只从 accepted 生成唯一 `candidate.pptx`。中间修复只重建受影响区域证据，并检查 finding、受影响区域及相邻边界和同一容器。candidate 仅在目标达到 expected、无新 P0/P1、结构通过时晋级；未改善目标问题、回退或结构失败时不得覆盖 accepted。箭头/连接线同时核对端点、方向、弧度、线宽、填充与层级；复杂装饰连续两次原生重绘仍退化时，改用最小范围透明 picture。

局部 candidate 不立即重建整页证据链。晋级后、进入 reviewer 前，基于当前 accepted 重新运行结构门禁，按下述复用合同沿用或一次性生成全页 preview、对照图、overlay、diff 和全部 regions 200% 证据，通过 commentary 展示当前双/三联图。PPTX 再次写入时，上一版本的 preview、全页证据和 reviewer 结论立即失效。

### 当前任务内证据复用

复用仅限当前页目录内。PPTX SHA-256、source SHA-256、spec SHA-256、fontconfig SHA-256、渲染器身份、渲染尺寸与裁切参数、证据脚本 SHA-256、区域定义 SHA-256 必须字段齐全且完全一致；命中即沿用路径并复核哈希，任一字段缺失或不一致则重建。不得跨任务复用、读历史缓存或新增状态机。

完整证据用 `create_visual_diff.py` 生成；检查 SHA-256、左右顺序、preview 版本和 `region_summary.skipped==0`。缺证据、错页、旧 preview、拉伸/裁切或非法区域时为 `not_reviewable`。

tripwire 只单向阻断：有用户批准基线时 `available=true` 且触发即失败；未触发不能自动通过。无批准基线时固定 `available=false, triggered=null, reason=no_approved_baseline`，不得临时设阈值。全页 similarity、foreground similarity、edge F1 不能覆盖局部缺失、文字、换行、crop、merge、connector 或条形端点错误。

## 最多两轮与批量收敛

一轮是一次绑定当前 source/preview 哈希的独立 reviewer 调用；准备证据和结构校验不计轮次。每页最多 2 轮，第 1 轮通过即停止；`not_reviewable` 也计入一轮，调用前须预检证据。每轮启动全新上下文，不沿用上一轮结论。

reviewer 必须一次返回全部可见 P0/P1，不得只报告首个问题，P0/P1 不设数量上限；同根因可合并但须列全位置。第 1 轮后先批量修 P0，再修 P1；纯 P2 只在同根因且安全时顺带修。第 2 轮仍有 P0/P1 或 `not_reviewable` 时停止，不得开启第 3 轮、未审查修复或降级；输出当前产物但不得称审核通过。

### 第二轮准入

第一轮全部 P0/P1 finding 必须映射到唯一 `modules.high_risk.items`，且当前 accepted 的对应 item 均为 `result=passed`、`verification` 指向真实局部证据；存在未关闭/未映射 finding、新 P0/P1 或回退时，不消耗第二轮 reviewer。只有全部 P0/P1 关闭、结构通过、完整证据按当前哈希预检后，才能启动第 2 轮。事实重判须用证据更新同一 item，不得降级严重度规避准入。仍有安全替代策略时继续修复；若只会重复失败策略、引入回退或没有可验证改善路径，则按第一轮记录输出未通过草稿，不得伪造第二轮记录。

全局字体度量、字距或换行 P1 关闭前，必须同时核对密集正文、数字与单位、换行敏感区域；任一仍有同根因差异，item 保持未关闭。

失败分支不新增 schema、validator 或状态机。输出绑定最近 reviewer 的 source/preview 哈希，提供当前 PPTX、证据、结构报告和已发生的 reviewer 记录；继续输出当前可用产物，但不得称为完整完成或审核通过。含 P0 时标注“未通过视觉门禁，含 P0，当前 PPTX 可能不可用”；仅有 P1 时标注“未通过视觉门禁的可编辑草稿”；`not_reviewable` 时标注“当前 PPTX 未完成视觉审核，证据不可审查”。

## 独立 reviewer 固定提示词

```text
你是当前页面的独立视觉审查员。你只读审查，不得修改任何文件、PPTX、规格或代码，也不得读取构建脚本、规格、自报状态、上一轮结论或结构门禁结果。
只比较原图、当前 preview、side-by-side、diff/overlay 和区域 200% 对照。
逐项检查画布与主要区域、对象数量与几何、文字内容与排版、表格/矩阵、图形/连接线/图表、图片裁剪与层级及全部高风险区域。一次返回全部可见 P0/P1，不得只报告首个问题；P0/P1 不设数量上限。同根因问题可合并，但必须列全受影响位置。
仅返回 JSON：page_id、source_sha256、preview_sha256、decision、coverage、findings、p2_disclosures。
coverage 必须且只能包含 canvas_and_regions、objects_and_geometry、text_and_typography、tables_and_matrices、graphics_connectors_charts、pictures_crop_layers、high_risk_regions；值只允许 checked、not_applicable、not_reviewable。canvas_and_regions、objects_and_geometry 及当前 `elements[]`/module 命中的类别必须 checked；passed 时不得含 not_reviewable。
decision 只允许 passed、changes_required、not_reviewable。findings 每项包含 severity、category、location、source_fact、observed_difference、evidence；severity 只允许 P0、P1、P2。
存在 P0/P1 时必须 changes_required；证据缺失、错页或哈希不一致时必须 not_reviewable。
```

`visual_gate.reviewer.mode` 固定为 `independent_read_only_subagent`；`review_round` 只允许 1、2。source/preview hash 必须为当前值；finding 字段须完整，decision 与 P0/P1 一致。仅 decision=passed、coverage/证据完整且无 P0/P1 时写 visual passed。视觉子代理不得修文件。

## 严重度与修正

- **P0：** PPTX 不可用、页数/比例/页面对应错误、核心内容缺失、主要内容不可编辑或图表编造数据。未关闭时不得作为通过版交付；第 2 轮后仍输出当前文件，并明确标注可能不可用。
- **P1：** 数量、主要区域比例、结构/merge、关键 fill、明显字号/换行、局部 Text Run、bullet、关键位置/crop、状态条表示、connector 或图表映射错误。未关闭时不得作为通过版交付；第 2 轮后仅作为未通过视觉门禁的可编辑草稿输出。
- **P2：** 不改变内容、结构、关系和可编辑性的字体 fallback、轻微色差/线宽或 renderer 近似。披露后可交付。

confidence 与 severity 分开；证据不足不自动成为 P1。`changes_required` 必须修；`visual_approximation` 仅用于事实不可确认或原生对象明显降质并说明影响；`not_verifiable` 不算通过。用户明确错误、假 bullet、同源列表拆框、跨行状态条、替换图标、断裂 connector/折线和整页图片化不得降级为近似。只有门禁发现明确差异才修，不为空刷新 hash 做 correction。

## 自动结构门禁与终态身份

每轮视觉审查前，结构门禁首次运行即用 `--output` 原子保存当前 validator JSON，要求 `valid=true`、`pptx_sha256` 等于当前 PPTX、页数/16:9 正确、主要内容可编辑、无整页图片冒充、Text Run 和图片资产正确；列表规格存在时 `native_list_contracts_checked` 等于同源列表 TextBox 数。必要时局部查 OOXML `noFill` 等 validator 尚未覆盖的属性。结构通过不证明视觉通过。

第 2 轮 reviewer 返回后不得再写入 PPTX。通过页的 visual/editability gate 绑定同一 PPTX。终态只显式运行一次 `validate_reconstruction_spec.py --stage final`；final 内重新运行 `validate_pptx.py`，核对 PPTX/preview/reviewer 哈希并校验 visual-diff、overlay/diff、regions 和 `reviewer page_id`。不得在 final 前后追加等价的 `validate_pptx.py`、`unzip -t` 或另一轮 full final。仅修证据/规格绑定可重跑 final；若改写 PPTX，reviewer 结论失效并回到候选流程。失败页不伪造 `passed` 或强求 final 通过。

## 多页与交付

默认逐页串行并标注 `[第 N/总页数]`。失败页输出后继续后续页面且仍按上传顺序合并。任一页有 P0/P1 或 `not_reviewable` 时，整份标注“未通过视觉门禁版”。用户要求并行时仍按页隔离和唯一写入；合并前核对身份与结论，再用 `merge_pptx.py` 验证。

最终提供 PPTX、当前视觉证据、结构/final 校验、已发生的 reviewer 记录和 P2/未验证说明。缺证据、哈希不一致或有 P0/P1 时不得称完整完成；失败时输出当前产物并标注未通过。
