# 设计依据、旧方案修正与外部项目取舍

> 日期：2026-09-29。只读调查与未来计划；没有重新运行翻译、benchmark、Jev 端点或远程仓库调研。外部项目机制来自仓库内既有调研，不表示已重新验证上游 HEAD。返回[总路线图](2026-09-29-future-capability-roadmap.md)。

## 1. 证据口径

本次阅读了 `docs` 的架构、下个迭代、权威收敛、两篇 JEV 会话归档、GitHub 一致性调研，以及 16 篇项目剖析的机制总览与相关对照部分；没有逐行重新审计其中所有外部代码片段。并只读核对了当前工作树的配置、编排、字段覆盖、请求构建、术语收集、匹配器、Jev 客户端、工作流及相关测试样例。

代码基线 HEAD 为 `d9981f9d9f8d21dec1f6ee7ff367fe93e1d0fb8d`。调查开始时，GitHub 调研及 `translation-consistency-designs/` 已是未跟踪文件；它们是用户现有材料，保持原样。旧文档引用的测试通过数、504 次数、234,897 个字段、2,000 个词条都是历史样本事实，不能当作本次测量或未来覆盖率承诺。

证据可信顺序：本次明确需求 → 当前代码的实际分支 → 有来源的历史调查 → 外部可借鉴模式。历史文档中的“必须保持上游整目录同步”等建议不是禁止本次设计重构的授权边界。本次仍然只写新计划，未来是否实际执行另属一次实施任务。

## 2. 当前代码对设计的直接影响

| 当前观察 | 位置 | 后续工作含义 |
|---|---|---|
| 词表由一次 GET 获取；未发现 10 页/8,000 条硬上限 | [get_proper.py](../src/translateFunc/get_proper.py)，`fetch` | 不再把“扩展分页”列为已确认缺陷；改成版本化本地资产，远程旧表仅作迁移种子 |
| 启动会 `merge_terms`，候选以同 KR 最高票输出；默认收集、热更新开启 | [new_terms.py](../src/translateFunc/proper/new_terms.py)、[config.py](../src/translateFunc/config.py)、[pipeline.py](../src/translateFunc/pipeline.py) | 票数、线程完成顺序均不能决定定译；拆除读回与热更新，保留隔离采集 |
| 自动更新把流水线输出放临时目录，未显式传持久 `new_terms_path` | [runner.py](../src/auto_update/runner.py)，`_run_translation` | 当前跨 Actions 的候选持久化不足，但“使候选持久化”不意味着使其可注入 |
| 优先翻译 `BattleKeywords*`/角色文件，之后读取输出构造中文角色和效果表 | [pipeline.py](../src/translateFunc/pipeline.py)，`_update_roles`、`_gather_affects` | 除普通词表外还存在机器定名回流；所有中文知识入口均须经过同一来源规则 |
| 角色列表用 `zip_longest` 配对，效果列表已按 ID 配对，重复效果当前先到优先 | 同上 | 角色改为 ID 对齐；同 ID 跨域同义与否需区分；效果不再以文件顺序解决冲突 |
| RequestBuilder 保存 KR/JP/EN，但调用 `match_all(kr_text_val)` 未传日英；匹配器有可用 `jp_text/en_text` 参数 | [request.py](../src/translateFunc/builder/request.py)、[engine.py](../src/translateFunc/matcher/engine.py) | “已有三语”不等于端到端用好；多语证据应贯通召回、消歧、生成、检查 |
| StoryData 按数组序号建索引；普通嵌套数组展平仍包含下标 | [processor.py](../src/translateFunc/processor.py)，`_make_data_index`、`_flatten_texts` | 插行/重排会破坏跨语对应；统一逻辑身份与物理路径，不能仅替换文件前缀 |
| `_field_covered` 排除空值与韩文原样回填，但不证明中文对应哪个 KR 版本 | 同上 | 保留字段覆盖能力，增加时间和源指纹认证，不把旧中文非空视为可复用证明 |
| Jev 按 `(block_index, term)` 去重；二选一；低置信度保留；成功仅删引用 | 同上，`_try_jev_disambiguation`、`_apply_jev_disambiguation` | 下一版要区分块内重复 span；保留决策对象与否定范围；未知不代表适用 |
| 自动 raw token 去重早于 `latest_stable_release` 查询 | [runner.py](../src/auto_update/runner.py)，`run`、`_select_release_version` | 中文晚到必须有独立触发，并用完整输入快照做幂等键 |
| 当前 cooked 下载实际使用 Release 上传资产；README 仍描述源码快照 | [runner.py](../src/auto_update/runner.py)，`_download_cooked_source`；[config.yaml](../src/config.yaml) | 新来源契约围绕实际资产 ID/内容哈希；tag/仓库名不够 |
| 当前已有重试升级、规则修复、诊断和字段覆盖测试 | [processor.py](../src/translateFunc/processor.py)、[tests](../tests) | 不把旧文档的“完全无重试/完全无校验”当当前事实；先区分外部服务重试与内容修复的缺口 |
| `.github` 默认只监听 raw dispatch/手工运行，未注入 Jev secret | [check.yml](../.github/workflows/check.yml) | 设计独立 authority 事件和状态轮询；Jev 仍按影子验证后上线 |

这些观察只是定位未来工作的依据；本次没有修复任何上述行为。

## 3. 必须修订的旧建议

| 历史建议 | 本计划如何取舍 | 原因 |
|---|---|---|
| [next-iteration-plan](next-iteration-plan.md) / [converge-to-authority](converge-to-authority.md)：首次机译作为全局译名账本 | 取消首次机译定名；运行前冻结有权威证据的约束 | 本次明确禁止机器专名直接用于后续翻译；锁并不能修正来源问题 |
| “模型学习”以弱建议进入术语表 | 不进入翻译提示，包括弱建议；只进审阅队列 | 弱提示、例句、摘要同样会把未确认译名带入后续生成 |
| Jev 验证 × 票数作为入库权重 | 只作为候选排序依据 | 判断模型不拥有中文译名权威，重复生成也不是独立证据 |
| 同路径可零歧义使用中文 | 要有版本对应证明、源指纹及字段语义 | 中文可能落后、路径可能复用、数组可能插行 |
| 归一化去空白/标点后按最高频中文复用 | 原值与结构语义分别保存；冲突不按频率择一 | 数字、否定、换行、标签差异可能改变游戏含义；同句不同角色也不同 |
| “两侧 CJK 都无空格，裸子串即可” | CJK 子串只负责候选召回；短 KR 词须词界/助词/范围消歧 | 韩语有空格且存在助词黏连；`사` 命中 `사색` 正是失败模式 |
| `exact` 意味着可以全局替换 | 仅在来源、适用位置和目标位置均确定时作局部修补 | exact 是目标形式要求，不证明该源词在此处属于该实体 |
| 所有已接受文本都不再回溯 | 历史 Release 不变；新版本必须修订失效的机器译名 | 项目有“权威译文迟到”的特殊生命周期，照搬小说一次性交付会永久锁死临时译名 |
| 所有新能力放 auto_update，绝不能改核心 | 推荐独立领域内核并逐步替换旧翻译控制流 | 用户允许破坏性重构；span、来源和阶段契约不能靠外层字符串拼接可靠实现 |
| 路径对齐率约 99.99%，所以未来几乎所有增量可复用 | 只作历史存量覆盖背景 | 已有中文全量的对齐率不等于当日缺译字段的可复用率；还需排除时间泄漏 |
| 参考剖析声称项目无付费 API/无 key | 不作为计划前提 | 当前运行要求生成模型 key；预算须据真实调用计量 |

## 4. 16 个参考项目的采用矩阵

“借鉴”指设计模式，不能直接把外部许可证、语言边界和未读实现当成已解决问题。实施时固定来源 SHA、核对许可证；若复制代码再保留对应声明。本次只引用内部文档并提出自有设计。

| 项目与材料 | 采用部分 | 必须改造/不采用 | 对应工作包 |
|---|---|---|---|
| [AiNiee](translation-consistency-designs/01-ainiee.md) | 术语、角色、风格分块；注入和检查共用匹配规则；离线检查 | 不采用裸字符串去重消除多义，不复制 regex 私有 API | W3/W4/W6 |
| [TranslateBooksWithLLMs](translation-consistency-designs/02-translate-books-with-llms.md) | 按块裁剪；少量必要角色事实；风格规则检查 | 全书 cast 常驻改为场景相关角色；不把英语屈折规则移植到中文 | W6 |
| [prozetta](translation-consistency-designs/03-prozetta.md) | 翻译前冻结上下文；卫生报告；变体链接而非盲合并 | 相似词根只作线索；passport 不能积累机器定译 | W3/W4/W6 |
| [translation-workbench](translation-consistency-designs/04-translation-workbench.md) | 阶段交接契约；事实和决定分离；审核证据 | 不引入运行时逐个等待用户批准的同步流程；待审资产与发布任务解耦 | W3/W7 |
| [LinguaGacha](translation-consistency-designs/05-linguagacha.md) | 词条局部适用事实；双向落位；关系报告；发现队列 | 自愈只能补证据，不能从机器译文自证权威；四态细节以实际实现复核为准 | W3/W4 |
| [KeywordGacha](translation-consistency-designs/06-keywordgacha.md) | 候选清洗、噪音隔离、增量账本 | 多数票不能决策中文；不无条件简繁转换权威原字形 | W3/W7 |
| [Tolmach](translation-consistency-designs/07-tolmach-book-translator.md) | 形式约束、快照指纹、局部编辑、编辑后校验 | 中文采用 explicit allowed_forms，不照搬目标词干；禁止无位置的全局改写 | W3/W4/W7 |
| [ln-translator](translation-consistency-designs/08-ln-translator.md) | 跨运行实体记录、别名和未知性别 | 不采用首次结果永不覆盖或姓名首词自动合并；权威后来修订必须生效 | W3/W7 |
| [P-Tipp](translation-consistency-designs/09-ptipp-consistency-checker.md) | 离线 finding、同源异译、禁用词与报告状态 | 同源文本按实体/语境分组；全字段包含目标词不代表对应位置已落位 | W4 |
| [translation-agent-rag](translation-consistency-designs/10-translation-agent-rag.md) | TM、术语、风格分开管理 | 不引入 FAISS/Chroma；相似结果只作参考；未读 Router/Critic 不视作已验证能力 | W2/W6 |
| [ATT](translation-consistency-designs/11-att.md) | triggers、重叠 span、稳定装箱、强不变量和 Review 区分、CAS | 权威迟到时需新增机器结果失效机制；稳定语境不等于让机器上下文变成术语 | W1/W4/W6/W7 |
| [dire-cli](translation-consistency-designs/12-dire-cli.md) | 生成前两级精确预填、内容指纹 | 不仅靠 KR 字符串决定 target；必须满足 authority、版本与语境条件 | W2 |
| [AIwork4translator](translation-consistency-designs/13-aiwork4translator.md) | 采集与翻译结果分开返回、错误分类、有界重试 | 禁止机器新词自动并回；一致性重写只针对适用的权威约束 | W0/W3/W4 |
| [l10n-lint](translation-consistency-designs/14-l10n-lint.md) | 规则目录、稳定 finding 指纹、基线豁免 | 不搬 PO/TS 解析；不能用历史基线屏蔽结构损坏和来源越界 | W4 |
| [bookllm](translation-consistency-designs/15-bookllm.md) | 抽取、生成、复核分阶段；最小编辑集 | 提示词不当执行保证；编辑必须验证字段 ID 和 before_hash；和上一小说项目是不同仓库 | W4/W6 |
| [llm-subtrans](translation-consistency-designs/16-llm-subtrans.md) | 上下文槽位、有界历史、具体错误回灌 | 不采纳从机译生成并持续回写术语；字幕分段不直接等同游戏字段 | W4/W6 |

## 5. 近期必须补齐的证据

1. 以结构化名称、剧情、技能、播报、UI 各取样，列出真实稳定 ID 和嵌套键，不假定所有 `levelList` 都有 `level`。
2. 梳理 raw token、三语资源哈希、LLC 资产/提交的可验证对应关系。缺证时保留 `source_version_unknown`，不能用发布日期补推。
3. 确定 LLC 正式资产内哪些内容受既定权威来源策略覆盖；排除能识别的自动回填/本项目机译回灌。仓库名称、`llc_ref` 和 commit 标题均不是逐字段证明。
4. 重建当前可靠性基线；历史保存错误与现有 retry escalation 必须分开核对，不预报本次一定还能重现 22 个 504。
5. 固定外部材料来源及许可证信息。旧快照放在临时目录，永久性和上游最新状态本次均未验证。
6. Jev 的版本、请求限制、价格来自 2026-09-26 会话资料，实施前重新核实；本计划只依赖“结构化判断、可回退”这个接口，不依赖历史价格宣传。

