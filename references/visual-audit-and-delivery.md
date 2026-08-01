# 视觉审计与交付

## 固定验证模式合同

`verification_profile` 是项目级固定模式，默认 `rapid`；用户明确提出“独立复核”才用 `reviewed`，明确提出“严格审核”才用 `strict`。运行中不得自动升级、降级或从 `reviewed` 进入 `strict`，多页合并必须拒绝混合模式。

| 模式 | 必需视觉证据 | reviewer | 成功状态 |
|---|---|---|---|
| `rapid` | 当前 preview、对照图、overlay、diff、`visual-diff.json`；不生成 regions 200% 证据 | 不启动独立 reviewer，visual status 为 `not_independently_reviewed` | `rapid_validated` |
| `reviewed` | rapid 全部证据 + finding、高风险对象和审查所需的必要区域 200% 证据 | 全新上下文只读 reviewer，最多 2 轮 | `reviewed_passed` |
| `strict` | 全页 + 完整 regions 200%；发生 candidate 时保留 initial/candidate 链 | 全新上下文只读 reviewer，最多 2 轮 | `strict_gate_passed` |

三个模式共享复刻、prebuild、结构、tripwire、对象身份和失败诚实性。profile 只控制终态证明成本，不得降低构建前输入质量。

## 统一 LibreOffice 预览渲染合同

三个模式统一使用稳定版 LibreOffice。LibreOffice 是统一预览渲染器，不是逐 TextBox 字号优化器。`preflight_runtime.py` 拒绝 LibreOfficeDev、alpha、beta、rc，并锁定工具与 fontconfig 身份。`render_preview.py` 在隔离 profile 中执行唯一 `PPTX → PDF → PNG`，核对 960×540 point 单页 PDF、字体及非空 `1920×1080` PNG，原子写入 `render-report.json`。

`create_visual_diff.py` 必须用 `--render-report` 取得 preview。SHA-256 只用于身份与溯源，不是视觉评分；视觉判断来自对照、指标、区域存在性和 reviewer。LibreOffice 是统一验收事实，不承诺 PowerPoint 原生像素一致。

macOS 沙箱首次渲染即升级权限；仅 `SIGABRT` 自动重试一次。异常时先确认 PPTX 对象，再到空目录复渲染；第二次失败即停止，不切换渲染器或无限重试。正常路径不双重渲染；PPTX 或运行身份变化即废弃旧证据。

按表执行当前 profile：`rapid` 只做主代理整页证据；`reviewed` 只为 finding/高风险对象生成必要区域证据；`strict` 保留完整 regions 与 accepted/candidate 证据链。三者都须先通过结构、visual-diff schema、tripwire 和 final；失败写各自失败状态，成功状态不得冒充更高等级。

## 三个检查点

1. **自动 authoring/prebuild：** 编写期反复跑等价只读 `authoring`（非门禁）；冻结后只跑一次正式 `prebuild`，失败不生成。
2. **初始诊断：** compiler 后仅 render → text geometry → structure；一次列全 P0/P1，不生成 background/visual/review/final。
3. **最终门禁：** 只对 final current 补齐一次 structure/background/visual/profile 证据；`reviewed|strict` 再独立只读审查。指标不自动批准，审查后禁改 PPTX，最后 final。

用户反馈、圈选和门禁差异写入唯一 `modules.high_risk.items`；未触发时不建空清单或第二套状态机。

## 唯一综合候选与当前视觉证据

每页最多 `initial + 1 comprehensive candidate`（2 次 compiler/render）。初始 preview/text/structure 后按 mapping → regions/层级 → 系统文字 → TextBox → 图示 → 图片/图标 → 细节一次列全 P0/P1；同根因以代表对象验证后批量修正。

无修正则 initial 成为 current 并同哈希补证，任何 profile 都不强制制造 candidate；有修正才新建唯一 candidate，启动即耗额。仅改善、无新 P0/P1 且结构通过才晋级；同 preview、无改善、结构/视觉失败、仍有 P0/P1 或 `not_reviewable` 均停止，交付较好版本和失败状态。

candidate 中间只查 finding 与邻界；晋级后才生成全页/profile 证据。source/grid 未变复用 overlay；PPTX 写入即废弃旧绑定。

### 当前任务内证据复用

仅当前页复用；PPTX/source/spec、runtime/fontconfig/renderer、preview/crop、validator/证据脚本、regions 身份须齐全一致，否则重建。相同身份已通过的 render/text/structure report 直接补证，禁跨任务复用。

完整证据用 `create_visual_diff.py --render-report ...` 生成；检查身份、左右顺序、区域存在性和 `region_summary.skipped==0`。缺证据、错页、旧 preview、拉伸/裁切或非法区域时为 `not_reviewable`。

tripwire 只单向阻断：批准基线触发即失败，未触发不能自动通过。无基线固定 `available=false, triggered=null, reason=no_approved_baseline`。全页指标不能覆盖局部缺失、文字、换行、crop、merge 或 connector 错误。

## reviewer 最多两轮

reviewer 调用每页最多 2 轮，第 1 轮通过即停；`not_reviewable` 也计轮，每轮全新上下文。两轮 reviewer 不等于两个 candidate，不能增加构建额度。

reviewer 一次返回全部 P0/P1。额度未用时把第 1 轮 findings 合入唯一 candidate；已用则失败，禁另开。第 2 轮仍失败即停，禁第 3 轮或降级。

### 第二轮准入

第一轮全部 P0/P1 必须映射到 `modules.high_risk.items`，accepted 对应项须为 `result=passed` 并有真实证据；有未关闭项时不消耗第二轮 reviewer。全部关闭后才启动第 2 轮；不得降级严重度，不得伪造第二轮记录。

全局字体度量、字距或换行 P1 关闭前，必须同时核对密集正文、数字与单位、换行敏感区域；任一仍有同根因差异，item 保持未关闭。

失败分支不新增 schema、validator 或状态机；继续输出当前可用产物，但不得称为完整完成或审核通过。含 P0 标注“未通过视觉门禁，含 P0，当前 PPTX 可能不可用”；仅 P1 标注“未通过视觉门禁的可编辑草稿”；`not_reviewable` 标注“当前 PPTX 未完成视觉审核，证据不可审查”。

## 准入派生的独立 reviewer 提示词

`reviewed|strict` 在 background/text/structure/visual 通过并回写其身份、tripwire、editability、P0/P1 关闭事实后，立即用 `freeze_reconstruction_spec.py --purpose pre-review` no-overwrite 创建 `work/pre-review-spec-snapshot.json`；issue 只读它，控制器仍读 text 锁定的 build snapshot。禁回读工作 spec 或手工复制。issue 生成 admission/prompt；prompt 禁手写、编辑或复用，page/round/hash 禁人工输入。

reviewer 前必须用 `review_admission.py invoke` 消费当前 admission，再把未改写的 `reviewer-prompt.txt` 交给全新只读 reviewer。reviewer 仅返回 admission 规定的九个 JSON 字段：`admission_id`、`page_id`、`review_round`、`source_sha256`、`preview_sha256`、`decision`、`coverage`、`findings`、`p2_disclosures`。保存原始响应且不改写，随后用 `review_admission.py validate-response` 绑定 admission、invocation 和原始 response；验证失败不得写 visual passed。

`visual_gate.reviewer.mode` 固定为 `independent_read_only_subagent`，其余 reviewer 字段必须与 response-validation 绑定的原始 response 完全一致；`admission_id` 和 `review_round` 同时必须等于 admission。仅 production response-validation `valid=true`、decision=passed、coverage/证据完整且无 P0/P1 时写 visual passed。reviewer 不得修文件。

## 严重度与修正

- **P0：** PPTX 不可用、页数/比例错误、核心内容缺失、主要内容不可编辑或数据编造；未关闭不得通过。
- **P1：** 数量、比例、结构、fill、字号/换行、Text Run、bullet、crop、connector 或图表错误；未关闭不得通过。
- **P2：** 不改变内容、结构、关系和可编辑性的字体 fallback、轻微色差/线宽或 renderer 近似。披露后可交付。

confidence 与 severity 分开；证据不足不自动成为 P1。`changes_required` 必须修；`visual_approximation` 须说明影响，`not_verifiable` 不算通过。假 bullet、拆框、断裂 connector 和整页图片化不得降级为近似。

## 自动结构门禁与终态身份

每轮视觉审查前用 `--output` 保存 validator JSON，要求 `valid=true`，且 schema SHA-256、PPTX SHA-256、compiler identity/capability manifest、representation summary、对象清单、asset fallback 与 build report 三方一致；同时核对页数/16:9、可编辑性、整页图片风险和 `native_list_contracts_checked`。缺 report、旧 report 或任一绑定不一致都不得进入视觉审查；结构通过不证明视觉通过。

reviewer 返回后禁改 PPTX。终态只运行一次 final：当前工作 spec 提供终态；PPTX/background/text 重算读 build snapshot；admission 重建读 pre-review snapshot；当前 review-state hash 必须精确相等。并核对 PPTX/runtime/render/PDF/font/preview/visual 身份。`reviewed|strict` 重跑 response-validation；`rapid` 要 background/text 且拒绝 reviewer artifact。改写 PPTX 即全部失效。

## 多页与交付

逐页串行并标注 `[第 N/总页数]`。prebuild/compiler 失败只留证据、继续后页且不合并；已有 PPTX 的 visual/final 失败页才按原序合并。整份须披露缺页；任一合并页有 P0/P1 或 `not_reviewable` 时标注“未通过视觉门禁版”。并行时仍按页隔离和唯一写入；合并前核对身份与结论，再用 `merge_pptx.py` 验证。

最终提供 PPTX、当前视觉证据、结构/final 校验、已发生的 reviewer 记录和 P2/未验证说明。缺证据、哈希不一致或有 P0/P1 时不得称完整完成；失败时输出当前产物并标注未通过。
