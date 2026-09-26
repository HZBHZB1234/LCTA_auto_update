# 会话归档：Jev 阶段 0 逐位置消歧 —— 可行性与首次实施

- 日期：2026-09-26（Asia/Shanghai）
- 项目：`LCTA_auto_update`
- 本文继 [2026-09-25-architecture-terminology-jev-session.md](2026-09-25-architecture-terminology-jev-session.md)，落实其中"首个小版本：Jev 逐处消歧对照"的第一步实现，并记录可行性复核结论。
- 本次工作性质：代码调查、Jev 官方资料核实（含端点真实调用）、客户端实现与单元测试、阶段 0 集成的首个可用版本。**尚未开启生产路径（`jev_enabled` 默认 false），也未做大规模效果评测（用户明确暂不动 `benchmark/`）。**
- 阅读约定：沿用上一篇，"官方翻译"按会话语境指都市零协会／LLC 正式发布译文。

## 导航

1. [复核后的事实基线](#复核后的事实基线)
2. [计划可行性结论](#计划可行性结论)
3. [Jev 融入方案：三个落地位置](#jev-融入方案三个落地位置)
4. [本次实施：阶段 0 逐位置消歧](#本次实施阶段-0-逐位置消歧)
5. [实施细节与已修复问题](#实施细节与已修复问题)
6. [测试与真实端点验证](#测试与真实端点验证)
7. [后续工作与未决事项](#后续工作与未决事项)

## 复核后的事实基线

对上一篇归档中的关键判断，逐一对照了当前代码（提交 `85c38bc` 之后的工作树）：

| 上一篇判断 | 当前代码复核结果 | 状态 |
|---|---|---|
| 词表获取无快照/失败回退 | [`get_proper.py`](../src/translateFunc/get_proper.py) 仍为 `requests.get(timeout=10)` 直接返回 JSON | ✅ 属实 |
| 去重先于 LLC 获取 → LLC 后续版本被跳过 | [`runner.py`](../src/auto_update/runner.py) 中 `_select_release_version()` 在下载 LLC 前返回 | ✅ 属实 |
| 新词默认落在临时目录，自动发布不积累 | 自动更新构造 `TranslateConfig` 未指定 `new_terms_path` | ✅ 属实 |
| 消歧结果是"整词移除"，无逐处语义 | [`processor.py`](../src/translateFunc/processor.py) `_apply_disambiguation()` 从 reference 与全部块引用移除 | ✅ 属实 |
| hybrid 仍收集全部匹配术语 | [`_collect_ambiguous_terms()`](../src/translateFunc/processor.py) 注释确认 confidence 过滤尚未集成 | ✅ 属实（比归档更乐观：阶段 0 当前是"全量术语"消歧） |

Jev 官方关键事实（2026-09-26 重新核实 [Models 页](https://docs.typesafe.ai/models)与 [API 参考](https://docs.typesafe.ai/api)）：

- 模型 `jev-1.13.0`，别名 `jev-latest`/`jev-preview` 当前均指向它；
- 价格 **$0.042 / 1M 输入 token，输出免费**；
- 上下文：state 与全部问题合计 64k，state 与单最长问题合计 32k；
- 多问题**同一请求内并行独立评估**（一大把问题 ≈ 一次请求的延迟）；
- Choice 最多 255 选项，Score 最多 10 级；Noul 无单独 confidence 字段；
- **语言：英语最佳，CJK 可处理但不等同，需实测**；
- 三个官方端点验证结果（实测 HTTP 200，`model: jev-1.13.0`）：
  - 官方 `https://api.typesafe.ai/v1/systemone` 与个人代理 `https://sub.nekopeer.com/v1/systemone` **输出结构一致**（个人端点即官方兼容代理）；
  - 响应 `answers.<question_id>` 为 `{type, noul|choice+confidence+probabilities|score}`，`usage` 返回输入/输出 token 计数。

## 计划可行性结论

### 方向合理性

上一篇归档的路线（B 主线：权威翻译记忆库；A 基础：术语资产；C 融入：全局定名＋固定快照）**方向正确，与当前代码状态一致**。本次复核没有发现归档中的判断过时或错误。

### 三个值得补强的判断

1. **Jev 的价值窗口比归档估计更大。** 归档假设 hybrid 已有部分 confidence 过滤，但代码显示阶段 0 实际是"全量术语消歧"（`_collect_ambiguous_terms` 在 hybrid 下收集全部）。也就是说，现在每次运行、每个文件、每个匹配术语都在走生成模型消歧——这正是 Jev 最容易替换、且行为最可审计的地方。
2. **"路径相同不代表译文有效"同样适用于现有覆盖判断。** `_field_covered()` 检查"有无译文"，不核对 KR 指纹。归档用它论证记忆库要存原文指纹；落地时这是记忆库与增量术语库的公共前置。
3. **首个版本的决策口径必须内置量化。** 只做"跑一次对照，人工看结果"不足以下上线决定。本次实现把 Jev 决策**记录进诊断事件**（decisions/excluded 全量），后续对照可直接从 dump 中统计误套率、漏用率、回退率。

### 风险排序（按影响 × 发生概率）

| 风险 | 评估 | 缓解 |
|---|---|---|
| CJK（KR/CN/JP 混合语境）准确率不达标 | 高 × 高（官方明确须实测） | 默认关闭、只替换阶段 0、低置信度/失败全回退；dump 全量记录对比 |
| 外部 API 稳定性（网络、限速、断服） | 中 × 中 | 独立客户端、请求级失败返回 None → 回退 LLM 消歧路径；`JEV_TEST_LIVE` 冒烟脚本可手动验证 |
| 阈值未校准导致误判 | 中 × 中 | `jev_min_confidence` 可调；低于阈值不采纳（保守保留） |
| 逐块语义与现有整词语义混用 | 低 × 低 | Jev 走独立 `_apply_jev_disambiguation` 路径，不影响现有 `_apply_disambiguation` |

## Jev 融入方案：三个落地位置

核心认知：**Jev 是 System One 决策模型，不产出译文。** 它的价值不是"翻译更快"，而是把"需要判断才能决定是否翻译/如何翻译"的环节从生成模型试探改为便宜判断，从而**减少调用次数与无效试探**。速度与成本因此来自调用结构的改变，而非单个请求的速度。

按收益排序的三个位置（本次只落地第一个）：

| 位置 | 说明 | 收益 | 状态 |
|---|---|---|---|
| **① 阶段 0 逐位置消歧** | 每个 (text_block, term) 单独一个 Choice 问题，判定该处是否按词表译名使用 | 直接替换当前"全量生成模型消歧"；输出绑定位置，修复"整词移除"缺陷 | ✅ 已实现 |
| ② 新词候选"进入门槛"验证 | 模型回传的新词先用 Jev 做原子判断（KR 是否完整名词 / CN 是否同指），入库权重 = 票数 × Jev 验证 | 阻止一个错误译名进入 AC 自动机 = 免掉后续所有相关字段的复用错误 | ⏳ 后续 |
| ③ 正式例句 rerank | SQLite 词面召回候选后，Jev Score 按机制/角色/语境接近度打分，取 top 3 作 few-shot | 让 B 方案"动态例句检索"无需向量检索基础设施即可上线 | ⏳ 后续 |

**明确不做的位置**：阶段 1 主翻译与任何"生成译文"动作——Jev 不生成文本，强行使用会降质。界限与归档一致。

## 本次实施：阶段 0 逐位置消歧

### 新增文件

| 文件 | 内容 |
|---|---|
| [`src/translateFunc/proper/jev.py`](../src/translateFunc/proper/jev.py) | Jev 客户端与代数：问题构建（Noul/Choice/Score）、`evaluate()` 单请求多问题、答案解析、`build_block_state`/`build_occurrence_questions`/`answers_to_occurrences` 工厂 |
| [`tests/test_jev.py`](../tests/test_jev.py) | 19 个单元测试（schema 解析、逐位置映射、低置信度门限、`_apply_jev_disambiguation` 语义、`_try_jev_disambiguation` 回退）+ 2 个真实端点冒烟（`JEV_TEST_LIVE=1` 时运行） |

### 修改文件

| 文件 | 改动 |
|---|---|
| [`src/translateFunc/config.py`](../src/translateFunc/config.py) | `TranslateConfig` 增加 `jev_*` 字段（默认关闭）；`from_config_manager` 同步 |
| [`src/auto_update/config.py`](../src/auto_update/config.py) | `TranslationSettings` 增加 Jev 段；`_optional_boolean`/`_optional_number` 帮助函数 |
| [`src/config.yaml`](../src/config.yaml) | `translation.jev_*` 配置段落（默认 `jev_enabled: false`，附注释） |
| [`src/auto_update/runner.py`](../src/auto_update/runner.py) | 把 Jev 配置透传进 `TranslateConfig` |
| [`src/translateFunc/processor.py`](../src/translateFunc/processor.py) | 阶段 0 入口分支：先 `_try_jev_disambiguation`，失败回退现有 LLM 消歧；新增 `_try_jev_disambiguation` / `_apply_jev_disambiguation` |

### 决策流程（`_try_jev_disambiguation`）

1. `jev_enabled` 关 / 环境变量无 key / 无文本块 → 立即返回 False（回退）。
2. 对每个 `(block_index, term)` 出现位置构造一个 Choice 问题：
   - state 只含待判断的去重文本块（`build_block_state`），指令用 backtick 引用 `blocks[i].kr`；
   - 选项 `applicable`（附词表译名、note 截断）与 `not_applicable`；
   - **只考虑这一处出现位置**，避免一次"不适用"扩散成整词禁用。
3. `client.evaluate(state, questions)` —— 一次请求并行评估全部位置。
4. 无答案/异常 → 返回 False → 回退 LLM 消歧。
5. `answers_to_occurrences(min_confidence=jev_min_confidence)`：置信度低于阈值不采纳（None → 保守保留）。
6. **只把明确 `not_applicable` 的 (block, term) 传给 `_apply_jev_disambiguation()`**：
   - 只移除该块引用（`proper_refs`）；
   - reference 仅裁剪不再被任何块引用的术语；
   - 同术语的其他出现位置不受影响。
7. 全量决策（含保留项）写入诊断事件 `parsed_response.decisions/excluded`，供后续对照统计。

## 实施细节与已修复问题

1. **URL 拼接 bug（已在实施中发现并修复）**：早期版本把 `base_url + "/systemone"` 无条件拼接，导致传入完整 `/v1/systemone` 的个人端点变成 `/v1/systemone/systemone`（实测 404）。现改为：仅当 base_url 不以 `/systemone` 结尾才追加。
2. **决策应用范围 bug（已在单测中捕获并修复）**：最初把"所有有答案的决策"（含 `applicable`）都当作排除集传给 apply，导致适用位置也被误删；单测 `test_success_applies_decisions` 捕获，改为只过滤 `not_applicable`。
3. **`is_high_confidence` 定义为 property 却带参数**（TypeError）——改为无参 property，阈值判定交给 `answers_to_occurrences(min_confidence=...)`。
4. **阶段 0 分支结构**：Jev 成功路径与 LLM 路径都调用 `builder._split_by_length(prompt_format=user_format)`，保证后续阶段分片行为一致。
5. **测试隔离**：`test_jev.py` 中真实端点冒烟默认 `skip`；设置 `JEV_TEST_LIVE=1`（且 `.env` 存在）时才调用外网，单测默认不依赖网络。

## 测试与真实端点验证

- 全量回归：`pytest tests -q --ignore tests/test_jev.py` → **371 passed, 5 skipped, 9 failed**，其中 9 个失败全部位于 `tests/upstream/test_validator.py`，通过 `git stash` 验证为**基线既有失败**（与本次改动无关）。
- Jev 测试：`pytest tests/test_jev.py -q` → 19 passed（均为快；真实端点另测）。
- 真实端点冒烟 `JEV_TEST_LIVE=1`：官方端点 + 个人端点各一次调用，均返回 `model: jev-1.13.0` 且 Choice 结构符合预期（2 passed）。单次请求输入 token 约 468（成本 ≈ $0.00002），符合"极小开销做单测工作流合理性验证"的要求，没有跑 benchmark。

## 后续工作与未决事项

1. **Jev 对照实验**（上一篇的"首个小版本"第三件事）：同一批难例分别跑现有消歧与 Jev，从 dump 的诊断事件统计误套率/漏用率/回退率/费用/延迟。用户明确暂不动 `benchmark/`，此步留待将来。
2. 阈值校准：以真实标注样本校准 `jev_min_confidence`（0.9 是起点，不代表已达目标精确率）。
3. 上线决策：对照结果出来后，再决定生产 `jev_enabled: true` 与端点选择（官方 vs 个人代理）。
4. 后续两个落地位置（新词验证、例句 rerank）的设计与实现。
5. 生产密钥注入：GitHub Actions 新增 `TYPESAFE_API_KEY` secret；本地密钥只保存在被 `.gitignore` 忽略的 `.env`。

**本文件记录本次实施的代码状态与验证结果，不代表"Jev 消歧已上线生产"或"已通过大规模评测"。** `jev_enabled` 默认保持 `false`。