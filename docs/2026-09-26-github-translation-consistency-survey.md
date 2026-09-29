# GitHub 翻译项目「术语 + 文风长期一致性」设计调研

调研日期：2026-09-26
调研对象：GitHub 上关注**长程翻译中术语与语言风貌稳定**的项目
调研方法：GitHub REST API（检索 / tree）+ `raw.githubusercontent.com`（正文）
产出目的：判断哪些外部设计可以落到 `LCTA_auto_update`

---

## 0. 方法与范围

### 0.1 方法

- 仓库检索：GitHub REST API `/search/repositories`。
- 目录枚举：`/repos/{owner}/{repo}/git/trees/{branch}?recursive=1`。
- 正文读取：`raw.githubusercontent.com`（带 `User-Agent`）。
- 环境限制：本机无 `GITHUB_TOKEN`、无 `gh` CLI。未认证限额为 core 60/h、search 10/min，因此检索使用窄查询并加间隔，所有深读走 raw，不消耗 core 配额。

### 0.2 收录判据

项目必须**显式**处理"跨文件 / 跨卷 / 跨更新"的一致性，且有一手可读的实现或规格文档。只做单次翻译、或只在 README 里宣传一致性的项目不收录。

### 0.3 阅读深度标注

- 【深读】读完了与主题相关的核心文件。
- 【略读】读了 README 与目录结构，关键结论基于文档声明。
- 【未读】已列出但未取正文，见附录 B。

---

## 1. 本项目基线事实

调研结论必须落在这份基线之上，因此先把与本主题相关的机制钉死（均为本次核对过的位置）。

### 1.1 术语匹配与注入

| 环节 | 现状 | 位置 |
|---|---|---|
| 匹配方式 | 4 个 AC 自动机（专有名词 / 角色 / 状态效果 ID / 状态效果名），精确子串，无模糊匹配、无向量 | `src/translateFunc/matcher/engine.py`、`matcher/ac_automaton.py` |
| 术语表来源 | 单次 GET `https://web.lcta.top/proper_terms_paratranz.json`，10s 超时，无分页 | `src/translateFunc/get_proper.py`（全文 6 行） |
| 术语字段 | `term` / `translation` / `note` → `ProperTerm{kr, cn, note, positive_contexts, negative_contexts}` | `matcher/proper.py:65-105` |
| 按块过滤 | **已有**：`chunk_proper_refs` 由 `block["proper_refs"]` 得到，只取该块实际引用的术语 | `builder/request.py:184-193`、`266-273` |
| 重试时再裁剪 | **已有**：重试请求只保留被引用术语 | `processor.py:2398-2400` |
| prompt 顺序 | glossary → affects → role_styles → skill_reference → text_blocks | `builder/request.py:425-442` |
| 渲染格式 | `<glossary><term><kr/><cn/><note/></term></glossary>`，`note` 为空则省略 | `builder/prompt.py:687-704` |

> 注：`note` 字段在语义上已经等价于 LinguaGacha 的 `info`。这是后面最省力的接入点。

### 1.2 文风 / 语气

| 环节 | 现状 | 位置 |
|---|---|---|
| 数据 | `translate_doc.ROLE_STYLE`，15 个角色，中文键 `角色/称呼习惯/语言风格/示例/特殊说明/昵称示例` | `src/translateFunc/translate_doc.py:96-176` |
| 映射 | `RLOE_COMPARE` 把 18 个 KR 名（含 `단테2`、`베르길리우스2/3`）映射到 EN 键 | `translate_doc.py:177+` |
| 注入条件 | 仅 `is_story`（`rel_path.parent.name == "StoryData"`） | `processor.py:2516`、`request.py:396-406`、`433-435` |
| 触发时机 | 只在处理 `model` 文件时更新角色表 | `pipeline.py:210` |
| 技能文件 | 改用 `SKILL_DOC`，与角色风格互斥 | `request.py:408-412` |
| 配置 | `src/config.yaml` 只有 `features.enable_role: true`，**没有任何 style / voice 结构键** | `src/config.yaml` |

### 1.3 后处理校验（本项目最薄的一环）

`src/translateFunc/validator.py` 是唯一存在的确定性后处理校验器，docstring 自述"用于技能文件翻译"。规则只有 4 条：

| 规则名 | 位置 |
|---|---|
| `buff_spacing` | `validator.py:231` |
| `effect_ref` | `validator.py:306` |
| `richtext_escape` | `validator.py:347` |
| `bracketed_cn_buff` | `validator.py:404`、`426` |

**没有一条检查"术语表里的固定译名是否真的落到了译文里"。** 这是本次调研发现的最大缺口。

现有的近似兜底是"韩文原样回填检测"（`contains_hangul` / `is_untranslated_hangul`，`tests/test_field_coverage.py` 覆盖），但它判的是**漏翻**，不是**术语不一致**。

### 1.4 架构约束（决定方案形态）

- `src/translateFunc` 是从 LCTA 工具箱**原样同步**的翻译核心，`README.md`（约 109 行）与 `docs/architecture.md` 都明确它不承载本项目逻辑。
- 因此新能力应当优先放在：`src/auto_update/`、`tools/`，或作为**独立模块**由 `src/config.yaml` 开关驱动。
- 直接改 `translateFunc` 内部会与上游同步冲突，除非改动本身也能回流上游。

### 1.5 基准现状

`benchmark/runs/subval-iter-01/compare.md`：

- 共同字段 152 个，其中 **105 个因 stage-0 消歧退化而不可归因**，只有 47 个可用于归因。
- 不可归因子集上 `mean_chrf_nontrivial` 从 64.854 → 63.119（Δ −1.735）；分桶 `other_placeholder` +8.56 / `other_plain` −0.10 / `other_tag` −12.39 / `other_term` −1.98。

**结论：任何 prompt 侧改动的 A/B 目前都会被噪声吞掉。** 这条约束直接影响方案优先级（见 §6）。

---

## 2. 外部项目清单

| 项目 | 与本主题相关的核心能力 | 深度 |
|---|---|---|
| `NEKOparapa/AiNiee` | 术语表 / 禁翻表 / 角色 / 世界观 / 文风 / 翻译示例多块并列注入；独立的 `TerminologyChecker` 离线术语检查 | 【深读】 |
| `hydropix/TranslateBooksWithLLMs` | CAST 块（性别）注入**每个** chunk；glossary-filter 按块裁剪；style-lint 禁止具体词范例 | 【深读】 |
| `UrgenProchnoff/prozetta` | 全书 passport（翻译前建立、可手工编辑）；glossary hygiene 报告（只报不改） | 【深读】 |
| `Alexu0317-FATHER/translation-workbench` | 角色档案 / 译者风格两个**最小 schema 模板**；只读上下文交接校验器 | 【深读】 |
| `neavo/LinguaGacha` | 术语表 `src/dst/info/case_sensitive`；四态落位判定；关系分析；种子账本自愈循环；角色扮演防退化 | 【深读】 |
| `neavo/KeywordGacha` | 大规模术语候选生成，导入 LinguaGacha | 【略读】 |
| `KazKozDev/book-translator`（Tolmach） | 术语 `exact/inflectable/preferred` 三态 + 源形泄漏重写；分块-跨全文实体一致性检查（词干匹配） | 【深读】 |
| `shahriar-ahmed-seam/ln-translator` | 跨卷术语表账本；性别归一与迁移；姓名合并 | 【深读】 |
| `P-Tipp/translation-consistency-checker` | 纯标准库离线 QA：五类检查 + CI 退出码 | 【深读】 |
| `10div10/translation-agent-rag` | 三库检索（TM / Glossary / Style）+ Router + Critic | 【略读】 |
| `yexi-by/att` | 游戏翻译：术语 `term/translation/triggers` + 重叠命中选择规则；译文候选四类验收（强不变量 vs Review）；TaskBlock 稳定装箱 | 【深读】 |
| `juliandreas/dire-cli` | `.dire.toml` 的 `[styleGuide]` / `[glossary]` 分层配置 | 【略读】 |
| `chaosen315/AIwork4translator` | AC 自动机术语保护 + 复数归一化 + 冠词处理；新术语实时采集 | 【略读】 |
| `yeager/l10n-lint` | PO/TS 本地化文件 linter | 【略读】 |

> `chaosen315/AIwork4translator` 值得单独一句：它也走 **AC 自动机 + 精确匹配**路线，说明本项目的技术选型在同类中文工具里是主流共识，不是需要推翻的落后选择。真正缺的是**校验层**，不是匹配层。

---

## 3. 设计模式提炼

### 3.1 注入范围：按块过滤 vs 常驻块

**共识：术语按块过滤，性别/声音常驻。**

- 按块过滤：LinguaGacha `relevant_terms()`（casefold 子串包含）、Tolmach `relevant_terms()` + `prompt_context()`（只渲染相关术语）、`TranslateBooksWithLLMs/glossary-filter.py`（拉丁词用 `\b`、CJK 用子串、`|` 备选求和、按长度优先、按频次封顶后重排）、ATT 的命中选择规则。
- 常驻：`TranslateBooksWithLLMs/injector.py::build_cast_block` 把 CAST 注入**每个** chunk，理由是性别在**名字缺席**时才是关键信息；`DEFAULT_MAX_CAST_ENTRIES = 80`（注释自述约 900 tokens，"injected into every chunk, so an unbounded list would be a permanent token tax"）。原文要求"MANDATORY… even when the source passage omits the subject or does not mark gender… never default to masculine for an entity listed as female"。

**ATT 的命中选择规则比本项目的 AC 最长匹配更细**（`docs/translation/terminology.md`「命中与使用」）：

1. 选起点最早的命中；
2. 起点相同时选跨度最长的；
3. 选中后丢弃与之**相交**的命中，从跨度末端继续；
4. 相邻或其后**独立**的命中仍然有效。

给出的例子：`魔法剣` 抑制内部的 `剣`，但 `魔法剣と剣` 仍分别触发两者；`プフクス`（姓名）与 `プフクスッ`（笑声）共存时，`プフクスッ` 只选后者，后方独立出现的 `プフクス` 仍触发姓名。另外，**实际触发的条目始终按术语文件顺序提供，而不是按出现顺序**——顺序稳定本身就是一种一致性。

**ATT 的 `triggers` 字段**：一个"规范原文身份"可以有多个原文写法，但只有一个目标写法。

```toml
[[term]]
term = '星読み'
translation = '观星者'
triggers = ['星読み', '星を読む者']
```

这正好对应本项目的痛点：同一角色的敬称 / 昵称 / 变体写法今天需要多条独立术语，容易译成不同中文。

### 3.2 术语表结构

| 项目 | 字段模型 | 关键约束 |
|---|---|---|
| LinguaGacha | `src` / `dst` / `info` / `case_sensitive` | `info` 的准入标准："影响译法·身份·指代·消歧的必要事实，**且条件须能从局部翻译输入判断**"；角色 `info` 必须声明 男性角色 / 女性角色 / 未知性别角色；`case_sensitive` 只在存在真实碰撞时 `true` |
| Tolmach | `source` / `target` / `mode` | `mode ∈ {exact, inflectable, preferred}`，默认 `inflectable`；上限 500 条、单项 200 字符；构造时按 `source.casefold()` 去重 |
| ATT | `term` / `translation` / `triggers` | 严格 TOML，只允许一个 `term` 数组；字段**按原值处理，不做 trim 或 Unicode 归一化**；拒绝未知字段、控制字符、重复 key、显式空 `triggers` |
| ln-translator | `{en, gender, es_variants[], notes}` | 预置 `CANONICAL_NAMES`，按 `characters/locations/magic_terms/organizations/creatures/other_terms` 分组 |
| 本项目 | `{kr, cn, note}` | 无 mode、无 case_sensitive、无性别、无变体链接 |

LinguaGacha 的 `info` 准入标准值得直接照抄：它把"需要跨文件才能成立的信息"**排除**在术语表之外，从根上避免术语表膨胀。

### 3.3 落位校验（本项目最缺的一环）

这是本次调研里外部项目投入最多、而本项目完全空白的地方。四种做法：

**（a）四态判定**（LinguaGacha `quality-glossary.ts`）

`resolve_glossary_application_state()` → `"none" | "applied" | "partial" | "missing"`；`evaluate_glossary_applications()` 对每个块判定；`format_glossary_entry()` 渲染为 `src -> dst #info`。四态而非二态，才能区分"完全没落位"和"落位了但只落了一部分"。

**（b）分块评估 + 跨全文统计**（Tolmach `kaz-quality_tests.py::eval_entity_consistency`，约 384 行）

这是设计理由写得最清楚的一个，直接引用：

> a chunk can be internally perfect and still call the family something different from what the previous chunk called it

实现要点：

- **在每个 chunk 上运行，但统计覆盖整个文档**；
- 屈折形式算作一次使用（词干匹配），理由是"a name that cannot take target-language endings would make the sentence around it ungrammatical"；
- 两个姓名词干互为前缀时（`Дурсль` / `Дурсли` → 都是 `Дурс`）**无法区分**，必须标记给人工，而不是静默通过；
- 术语表为空时测试 `flagged: True`，note 写着"with an empty glossary this test can only ever pass, which for a text whose main risk is proper nouns is the most expensive check to skip"；
- 逐术语报告 `chunks_with_source` / `chunks_with_rendering` / `forms_used`，按最差缺口排序；总结形如"N of M term(s) are not rendered everywhere"。

**（c）离线多类检查**（P-Tipp/translation-consistency-checker）

- 五类：approved term missing / forbidden term used / untranslated (target == source) / cross-file inconsistency (同一源文两个译法) / structural gaps（多出的目标串只算 warning 不算 flag）；
- 多语言行只在源术语出现时才 flag；单语言行只要术语出现就 flag；
- **默认大小写不敏感**；拉丁术语按词边界匹配（`health` 不匹配 `healthcare`）；无空格文字（泰文）用普通子串；
- 输出 `[FLAG]` / `[PASS]` + `Summary: 4 checks, 1 pass, 0 warn, 3 flag`；退出码 0/1/2 供 CI 使用；
- 支持简单 `term,translation` CSV 与完整 Matecat termbase CSV（`Forbidden` 列，`TRUE` 表示禁用）。

**（d）复用已译语料做检查**（AiNiee `TerminologyChecker.py`）

`run_check()` → `_perform_pre_checks("terminology")`（无项目文件报 `ERROR_CACHE`，无已译内容报 `ERROR_NO_TRANSLATION`）→ `_check_terminology()`。`_get_match_options()` 读取 `prompt_dictionary_match_case_sensitive` / `prompt_dictionary_match_whole_word`（**都默认 False**）。`_collect_merged_term_rows()` 把 characters / terms / glossary 三种来源合并成 `{src, dst, row_type}` 并按源去重。

**（e）模式感知的源形泄漏重写**（Tolmach `kaz-terminology.py`）

- `exact_violations(source_text, translated_text)` → `[{source, required_target}]`；
- `enforce_exact_source_forms(translated_text)` 只重写**源形泄漏**（模型把源语词原样留在译文里），用 `rf"(?<!\w){re.escape(term.source)}(?!\w)"` + `re.IGNORECASE`（`\w` 在 Python 里是 Unicode 感知的）；
- 跳过 `source.casefold() == target.casefold()`；
- **绝不触碰 `inflectable` / `preferred`**；
- `eval_terminology_delta()` 统计 draft 与 final 的违规差值，标签 `'Glossary violations (draft vs final)'`。

> ATT 的对应规格把边界划得更死（`docs/translation/candidate-validation.md` §4）：术语、专名、文风、语义、称呼是否一致，**只能产生 `ReviewFinding`**，不能证明候选必错；只有同时写明"权威依据 + 成立条件 + 确定性反例 + 最小拒绝范围 + 状态影响"五项，才能成为强不变量。这条纪律对我们是现成的护栏——术语未落位是**风险**不是**确定错误**（`exact` 模式除外）。

### 3.4 文风 / 语气

**（a）全书 passport**（prozetta）

在翻译**之前**建立（文档明说必须早于翻译，否则"corrects nothing"），整书一次调用，可手工编辑，重建前备份旧护照。`buildStyleBlock(passport, chunkIndex, promptLang, chunkText)` 按块取 POV。

`STYLE_WORDS` 的设计细节值得学：

| 键 | 作用 |
|---|---|
| `person` | 人称 |
| `secondPerson.fiction` vs `.nonfiction` | 同样是"ты"，对**角色**说和对**读者**说是两件事 |
| `readerGender` | m / f / neutral |
| `register` | 语域 |
| `precision` | "Это НЕхудожественный текст… Не украшай" |
| `author(name, genderTxt)` / `authorDossier` | 作者信息 |
| `authorVoice` | **被围栏限制在前后记**，理由写得极清楚："Said without the fence, this line would order the author's gender onto a first-person narrator of the opposite one" |
| `tense` | 以原文为基准："ЭТАЛОН ВРЕМЕНИ — ОРИГИНАЛ"（逐短语对齐原文时态） |
| `address(form)` | **只作用于作者叙述**，明确不作用于对白内的称呼 |
| `gender` | M/F 全套一致形式 |
| `focal` / `dossier` / `other` | 视点人物、档案、其他 |
| `dialogueAddress` | 由档案关系推出 ты/вы：下级→上级、证人→警察、陌生人/商务 = вы；亲近/家人/朋友 = ты |

工程约束同样重要：

- `DOSSIER_TOKENS`（默认 600）限制档案长度；
- `dialogueAddress` **只在 `dossiersShown > 0` 时输出**，注释："Only point at the dossiers when at least one is actually in the prompt"；
- `isEmptyPassport()` 在 narration / person / tense / addressForm / characters / povMap / addressRegistry / voices / dialogue 全空时返回 true，防止"空护照假装生效"。

**（b）文风抽取必须过 lint**（`TranslateBooksWithLLMs/style-lint.py`）

禁项 lint 码：`quoted_example`（直接引原句当范例）、`example_marker`、`word_list`、`proper_noun`、`too_specific`（`TOO_SPECIFIC_MIN_CHARS = 25`）。倾向抽象**倾向**，绝不给具体词。lint 保守，偏向召回。

**（c）角色档案的最小 schema**（translation-workbench）

`tw-character-profiles.md`（全文 21 行）的 `## Recording rules`：

> 只记录能改变**理解 / 语气 / 称呼 / 措辞**的信息；源语事实与翻译决定分开保存；持久指导**须经用户批准**才加入。

每个角色三段：

- `### Reading premises` — "Facts that change how the character or speaker should be understood across a whole work, even when a local passage does not signal them explicitly"
- `### Translation triggers` — "Specific textual signals that should affect wording, register, address, rhythm, or another translation decision"
- `### Provenance`

`tw-translator-style.md`（全文 31 行）的五段占位：

- `## Narrative contract` — 叙述者-读者关系与整体效果
- `## Voice and register` — 声音、语域、称呼与对白指导
- `## Target-language transformation principles` — 持久的结构 / 节奏 / 习语 / 本地化选择
- `## Protected choices and annotations` — **改动需用户批准**的措辞或注释模式
- `## Collaboration and decision rules` — 哪些选择模型可自主、哪些需用户决定

使用说明一句是关键："Read the sections relevant to the current translation unit before drafting or reviewing. **Do not invent missing preferences.**"

**（d）把文风漂移当默认失败模式**（LinguaGacha roleplay skill）

> **长期扮演容易让人物逐渐退化成性格标签和重复口癖**

自检项："只有姓名和口癖不同、其余反应可以套给任意人物时，回查缺失的依据并调整"。冲突保护顺序把"人物的外显身份、声音、自主性、知识边界与性格连续性"排在第 3 位（前两位是玩家对其角色的所有权、已成立事实与世界规则）。

`voice` 锚点的定义可以直接借：知识与用词来源、句法、礼貌程度、称呼、提问、停顿、沉默、回避与改口，以及对**不同人**的表达差异。

**（e）AiNiee 的多块并列注入**

`build_glossary_prompt`（`###术语表`，`原文|译文|备注`）、`build_ntl_prompt`（禁翻表）、`build_project_characters_prompt` / `terms_prompt` / `non_translate_prompt`、`build_characterization`（经 `CharacterHelper.normalize_rows` / `match_original_name`）、`build_world_building`、`build_writing_style`、`build_translation_example`，最后 `generate_prompt()` 组装。

其中 `CharacterHelper.py` 有几个可直接复用的细节：`SEPARATOR_TOKEN = "[Separator]"`；`LITERAL_BRACKET_NAME_PATTERN = r"\[[A-Za-z]+\]"`；`DOT_SEPARATORS` / `SPACE_SEPARATORS`（含全角 `\u3000`）；`ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff"`；用 `regex._regex_core._parse_pattern` 区分"字面术语"与"真正则"；`match_original_name` 逐关键词大小写不敏感搜索并**替换实际匹配到的文本**（保留源文大小写），无匹配则返回 `None`。

### 3.5 全书级 / 跨运行级决策

**（a）种子账本自愈循环**（LinguaGacha `glossary-extract.md`）

`work/TASKNAME-seeds.json` 记录种子与 `consumed` 标志，`completedRounds >= 3`；同轮多种子计一轮。五个探查方向：**发现候选 / 扩展同类 / 核验差异 / 追查关联 / 检查边界**，每个都带 worked example。交付顺序：先出关系图（Mermaid ER，≤3 图 × ≤10 实体，已确认但无关的实体画成孤立节点），再合并变更 `apply`，最后交账本 + 覆盖情况 + 决策 + 计数 + 未决项。

**（b）跨卷账本**（ln-translator）

`mark_volume_processed()` 维护 `meta.volumes_processed`；`consolidate_characters()` 最长名优先，把单 token 的教名或 token 集有重叠的条目折叠进全名，**已知性别胜过 unknown**，并合并 `es_variants`；`migrate_genders()` 每次加载都从遗留 `[MALE]` / `[FEMALE]` 注记回填性别，幂等；`merge_extracted_terms()` **从不覆盖**已有条目；`PRONOUNS = {male: "he/him/his", female: "she/her/hers", unknown: "they/them/their"}`，上方注释直接称性别/代词处理是"the #1 source of he/she translation errors"。

另外它的初始术语表里带两份策略文本：

- `honorifics_policy`："Preserve Japanese honorifics (-san, -kun, -chan, -sama, -sensei, -senpai) exactly as they appear in the source text"
- `style_notes`："Light novel tone: energetic narration, punchy dialogue, internal monologue in italics. Preserve exclamations and dramatic punctuation."

**（c）术语表制作流程**（ATT `skills/extract-game-terminology/SKILL.md`）

从**完整 Manual**（`manual export --selection all`）出发，而非分片；每条候选必须在完整 Manual 中核对：逐字存在 → 表达一个完整游戏专有名词 → 多次出现或跨上下文使用需要统一 → 结合角色/世界观定译 → **合并同一原文身份，保留确实需要共同触发同一译名的原文写法**。术语变化后重跑术语并审校受影响译文。

### 3.6 术语卫生与关系（只报不改）

**（a）prozetta `glossary_hygiene.js`**

- `normalizeGender` 归一性别写法；
- `betterSurfaceForm` 去全大写（de-shouts all-caps）；
- `caseDuplicates` 按小写找大小写重复；
- `nested` / `inconsistentNested` 用词干容忍：`i.length > 4 ? i.slice(0, -2) : i`；
- 性别冲突**只对名字**统计（避免噪声）；
- `glossaryFindings()` 产出报告，`applySafeFixes()` **只做安全修复**；
- 名称变体**链接而非合并**；删除覆盖更宽的术语时警告"the merge would lose coverage"；
- 零出现条目提示"0 occurrences — junk?"；
- 术语表条目带 Original / Translation / Type / Gender / Notes / # occurrences 列，1–10 评分 + 理由；
- 明确声明：**编辑术语表不会改变已译文本**，需要另行搜索替换。

**（b）LinguaGacha `relations.mjs`**

`analyzeRelations()` 用 `Intl.Segmenter` 做字素切分，`MIN_ROOT_LENGTH = 2`、`MIN_ROOT_COVERAGE = 0.5`，理由分 `equivalent` / `contains` / `shared_root`，按 `case_sensitive` 分桶。验收要求"对象覆盖 + 命中安全"双查。

术语表规则文档里给了判定示例：`セラ` / `セラ王女` / `セラ殿` 词根覆盖，`セラ教団` 无关，`黒騎士` / `白騎士` 前缀区分，`May` vs `may`。

**（c）KeywordGacha**

README 直接下判断："**高质量的术语表是保障翻译质量的最重要手段，没有之一**"，宣称 90%+ 命中率，三个作用：名词统一 / 人称矫正 / 角色关系引导。`NERAnalyzer.py` 有 `check(src, dst, info)`、`find_best(src, choices)`、`search_for_context()`、`rule_filter()`、`language_filter()`。

### 3.7 修订机制：编辑而非重写

**（a）Tolmach 的定位编辑 + 验证器**

精修阶段模型返回**定位编辑**（located edits），由 Python 只应用这些替换，避免重写已经正确的措辞。独立的 Verifier 对备选做**双向**检查；出现位置偏见时去掉有序 A/B 重试。Review desk 有 Source / Draft / Final 三栏，`Needs review` 优先，且"never replaces your final choice automatically"。

质量诊断会标记术语错误、漏译、数字变更、错语种段落、重复、语义漂移，但"they do not rewrite the book or prevent export"。

**（b）ATT 的验收分层**

- `ValidatedTranslation` / `ProvenInvariantViolation` / `ReviewFinding` / `OperationFailure` 四类结果；
- `ReviewFinding` **不能**被转换成拒绝、非零退出或写回阻断；
- 强不变量必须五项齐全（见 §3.3 引注）；
- `Rejected` 有独立生命周期：默认跳过以免重复消耗额度，只有显式 `--retry-rejected` 才重试；合法译文提交时在同一事务里写入当前译文并清除该 Unit 的 Rejected；
- 契约变化后重读数据库：仍合法则保留，违反则**保留正文并转入 Rejected**，不得删除正文或继续标为 Current。

**（c）LinguaGacha item-review 的警告码速查**

`SIMILARITY` / `FOREIGN_CHAR_RESIDUE` / `TEXT_PRESERVE` / `GLOSSARY` / `PUNCTUATION_MISMATCH` / `RETRY_THRESHOLD` / `LINE_COUNT_MISMATCH`。

**（d）共同纪律**

所有项目一致：**术语表是草稿，不是成品答案**；术语/风格变更**不回溯**已接受文本。prozetta 说得最直白——改术语表需要另行搜索替换；ATT 说"术语只改变后续模型请求的翻译要求。已接受的人工与自动译文保持 Current"。

### 3.8 状态与幂等（ATT）

虽然不直接涉及术语一致性，但 ATT 的装箱纪律对本项目的"跨更新幂等"有参考价值：

- 固定规划顺序：完整 Scope/Group/Unit → 按完整原文稳定表示装箱 → 判断每个 Unit 是代表还是语境 → 分配临时 ID → 过滤无 ID 的块 → 渲染发送；
- **Current、复用、全局去重、非源语、完全保护、Placeholder token、术语命中、临时 ID 和任务记录都不参与装箱**——状态只影响"哪些 Unit 带 ID、无 ID 语境显示原文还是译文、哪些块不发送"；
- Partial 重试：A/C/D 通过后，下一轮 A/C/D 以安全译文作语境省略 ID，B 单独带 `"0"`；
- 保存的 user message 必须包含完整 TaskBlock 的全部语境，即使实际只发了部分。

本项目的对应教训已经吃过一次：`processor.py:2491-2499` 记录了对 `llc_index` 做 `_align` 会让 `set(kr).issubset(set(llc))` 恒真、整文件被误判为已翻译。

---

## 4. 应用到本项目：候选方案

按"风险 × 收益"排序。P0 全部不碰 `translateFunc`。

### P0-1 术语落位校验器（离线、纯 Python、无 LLM）

**缺口**：`validator.py` 的 4 条规则完全不管术语是否落位。

**做法**：新增独立模块（建议 `tools/term_audit.py` 或 `src/auto_update/audit/`），输入 = 产物目录（`LLc-CN-LCTA`）+ 术语表，输出五类发现：

1. approved term missing（该文件出现了源术语，译文里没有对应固定译名）
2. partial（出现了但只落了一部分，对应 LinguaGacha 的 `partial` 态）
3. forbidden term used（若术语表将来支持禁用列）
4. untranslated（target == source）
5. cross-file inconsistency（同一源文两个译法）

**实现要点**（照抄外部已验证的判定）：

- 源语是韩文、目标语是中文，**两边都没有空格分词**，所以走普通子串匹配，不要用 `\b`（P-Tipp 对泰文的处理同理）；
- 大小写不敏感为默认；
- 逐术语输出 `files_with_source` / `files_with_rendering` / `forms_used`，按缺口最差排序（Tolmach 的报告形态）；
- 术语表为空时**必须报 flagged**，不能静默通过（Tolmach 的 note 说得对：对以专有名词为主要风险的文本，这是最不该跳过的检查）；
- 退出码分级（0 / 1 / 2）供 CI 使用，但按 ATT 的纪律：**未落位是 Review 而非 Rejected**，除非术语标了 `exact`。

**成本**：零模型调用，可先用现有 `benchmark/runs/*/output/` 做回归。

**参考**：P-Tipp `ptipp-README.md`；LinguaGacha `quality-glossary.ts`；Tolmach `kaz-quality_tests.py::eval_entity_consistency`。

### P0-2 术语卫生报告（只报不改）

**做法**：读远程术语表（或缓存快照），输出：

- 大小写重复；
- 包含 / 嵌套关系（词干容忍，`MIN_ROOT_LENGTH=2`、`MIN_ROOT_COVERAGE=0.5`）；
- 零出现条目（"junk?"）；
- 一名多译 / 一源多译；
- 前缀歧义对（`Дурсль` / `Дурсли` 型）——必须标记人工，不能静默通过。

**纪律**：`applySafeFixes` 只做安全修复（如去全大写表面形），其余全部作为 finding 交人工 accept/dismiss；**链接而非合并**；删除宽覆盖项要提示覆盖损失。

**参考**：prozetta `glossary_hygiene.js`；LinguaGacha `relations.mjs`。

### P0-3 术语表复用 `note` 作为 `info`，并加 `mode` 列

**现状**：`render_glossary` 已经在渲染 `<note>`（`prompt.py:687-704`），语义上就是 LinguaGacha 的 `info`。**不需要新增字段**，只需要：

1. 在术语表制作流程里按 LinguaGacha 的准入标准收紧 `note` 的内容："影响译法·身份·指代·消歧的必要事实，**且条件须能从局部翻译输入判断**"；
2. 角色类条目在 `note` 里显式声明性别（男性角色 / 女性角色 / 未知性别角色）；
3. 可选加 `mode ∈ {exact, inflectable, preferred}`，默认 `inflectable` 保持现有行为；只有 `exact` 才启用源形泄漏重写。

**参考**：LinguaGacha `lingua-glossary-rule.md`；Tolmach `kaz-terminology.py`。

### P0-4 术语表 `triggers` / 变体链接

把"同一身份的多个原文写法"收进一条术语，避免敬称/昵称变体被译成不同中文。

**参考**：ATT `docs/translation/terminology.md`；ln-translator `consolidate_characters()`。

### P1-1 外部角色档案（cast / voice），并把注入范围从 story 扩到全文件类型

**现状问题**：

- `ROLE_STYLE` 是硬编码在 `translate_doc.py` 的字典 → 扩展它等于改 `translateFunc`，与上游同步冲突；
- 只对 `is_story`（`StoryData` 目录）注入 → `PersonalityVoiceDlg` / `BattleAnnouncerDlg` / 技能与被动文本全部没有语气约束，而这些恰恰是有说话人的文本。

**做法**：新增 `cast.yaml`（或 `.json`）外部档案，字段用 workbench 的最小 schema：

```yaml
characters:
  - source_name: "단테"
    target_name: "但丁"
    gender: neutral          # male | female | neutral | unknown
    reading_premises:        # 跨整部作品都成立、改变理解的事实
      - "……"
    translation_triggers:    # 影响措辞/语域/称呼/节奏的具体文本信号
      - "……"
    address:                 # 对谁用什么称呼
      - toward: "베르길리우스"
        form: "敬语"
    provenance: "……"        # 该结论的来源
```

工程约束（照抄 prozetta + bookllm）：

- **按块过滤**：只注入该块实际出现的角色；
- **上限**：条目数上限（bookllm 80）+ 档案字符上限（`DOSSIER_TOKENS` 600）；
- **`dialogueAddress` 只在档案真的进 prompt 时才输出**；
- **`isEmptyPassport` 等价检查**：空档案不得静默生效；
- 性别必须显式声明，禁止默认男性（bookllm 的 MANDATORY 措辞）。

**落位方式**（避免改 `translateFunc` 内部）：

- 短期：在 `src/auto_update` 侧的 prompt 前置拼接；
- 长期：作为独立模块由 config 开关驱动（如 `features.enable_cast_file` + `cast_path`），走 `request.py` 已有的 reference 机制。

**参考**：`TranslateBooksWithLLMs/injector.py`；translation-workbench 两个模板；prozetta `passport.js`。

### P1-2 跨更新稳定决策档（passport 的本项目等价物）

Limbus 不是"一次翻一本书"，而是**持续长跑**。等价物不是全书护照，而是仓库里的**稳定决策档**：

- 叙述人称与视点约定；
- 称呼体系（谁对谁用敬语）；
- 术语定译来源（远程 paratranz 表优先，本地建议让位——`pipeline.py:276` 已有这条规则）；
- 禁用词 / 受保护措辞；
- 目标语标点与引号约定。

形式：手工可编辑的 YAML/Markdown，进 git；**必须在翻译前存在**；改动需要人工批准（workbench 的 `Protected choices` 段）。

### P2-1 权威语料检索优先（最高收益，但需设计）

`docs/converge-to-authority.md` 已经论证过：raw-kr 2073 文件 / 20.6 MB，llc-latest 2048 文件 / 19.4 MB，文件名对齐 2037/2048 = 99.5%，字段路径+值对齐 234,897 字段 ≈ 99.99%；`llc-base → llc-latest` 有 232,249 条完全相同（98.98%）。

**命中即不调 LLM** 的方案（L0 权威索引 + P1 路径级查找）能**同时**改善三件事：术语一致性（复用官方已定译）、成本、504 失败率。

**建议优先级高于任何 prompt 侧调整**——prompt 调整在 §1.5 的噪声下根本测不出来，而这条路线的收益是确定性的。

### P2-2 术语落位发现的种子账本自愈循环

把 P0-1 的发现做成 `work/*-seeds.json`，`completedRounds >= 3`，按五方向探查（发现候选 / 扩展同类 / 核验差异 / 追查关联 / 检查边界）。

**参考**：LinguaGacha `lingua-glossary-extract.md`。

---

## 5. 不建议引入

| 方案 | 理由 |
|---|---|
| 向量库 / RAG 三库检索 | 本项目 ~8000 术语上限、实际缓存约 2000、纯子串精确匹配；AC 自动机更快更确定。`docs/next-iteration-plan.md` 已判 premature。`translation-agent-rag` 的架构适合开放域，不适合定长术语表。 |
| gettext / PO 生态 linter | 与本项目 JSON `dataList` 结构不匹配。 |
| 自动回溯改写已发布译文 | 所有外部项目一致反对；本项目 Release 已被玩家安装，回溯改写风险更高。 |
| 直接改 `translateFunc` 内部实现 | 违反 `README.md` 的上游整目录同步约定。除非改动本身能回流上游。 |
| 无上限的常驻注入 | bookllm 明确警告这是"permanent token tax"；必须带上限 + 按块过滤。 |

---

## 6. 验证协议（做任何改动前必须先满足）

`benchmark/runs/subval-iter-01/compare.md` 的教训是硬约束：

- 152 个共同字段里 **105 个因 stage-0 消歧退化而不可归因**；
- 只有 47 个无混杂字段可用，且这 47 个上 Δ = −1.735 已接近噪声量级；
- 分桶差异极大（`other_placeholder` +8.56 vs `other_tag` −12.39），只看总 `mean_chrf_nontrivial` 会得出错误结论。

因此任何 prompt 侧 A/B 必须：

1. **固定 stage-0 行为**：`disambiguation_mode`、词典快照哈希、`min_confidence` 写入 `run_meta.json` 并在对比时校验一致；
2. **先建立无混杂基线**：两次运行使用同一 stage-0 结果；
3. **按桶看，不看总均值**：至少分 `other_term` / `other_tag` / `other_plain` / `other_placeholder`；
4. **术语类改动额外看术语桶**：`other_term` 是唯一能反映术语一致性的桶。

**P0 方案的优势**：P0-1 / P0-2 / P0-3 全部是**离线纯 Python**，可以拿现有产物目录直接回归，不需要调模型，成本为零，且不受 stage-0 噪声影响。这是它们应当排在最前面的第二个理由。

---

## 7. 开放问题

1. **术语表规模上限**：`get_proper.py` 是单次 GET、无分页；`matcher/proper.py:54` 的 `min_len` 只做过滤。历史上提到过"8000 上限（10 页 × 800）"的说法，但本次核对**未在代码中找到 800/8000 的硬编码**（唯一命中是 `proper/analyze.py:80` 的性能注释）。若要做全表审阅，需要先确认上游 paratranz 表本身的规模。
2. **`note` vs `info`**：建议直接复用 `note`，不新增字段——`render_glossary` 已经在渲染它。需要确认的是**上游表**是否允许写长 note（当前 `note` 的实际内容分布未调查）。
3. **术语表版本化**：玩家已装旧包 + 新包之间的一致性，需要"术语表版本"概念。目前 `Info/version.json` 只记录版本与 token。
4. **非 StoryData 文件的角色归属**：`PersonalityVoiceDlg` / `BattleAnnouncerDlg` / `Skills_*` 的文件名或内部字段可能能推断说话人，但 `is_story` 只看 `StoryData` 目录。做 P1-1 前需要先跑一次文件清单调查，确认可推断性。
5. **`exact` 模式的适用面**：Limbus 术语里哪些是真正的"不许改写"（如 `LCE E.G.O::` 这类含品牌缩写的），哪些允许屈折？需要一次人工标注。注意 `compare.md` 里已有 `LCE E.G.O:: 次元撕裂者` vs `LCE E.G.O::\n次元撕裂者` 的换行差异案例——这说明 `exact` 判定要区分"内容不同"和"空白/换行不同"。
6. **是否要把 P0-1 的结果接回 `auto_fixes_applied` 日志链路**：`processor.py:1216-1249` 已有该链路，但按 ATT 纪律，术语类发现应当是 Review 而非 auto-fix。

---

## 附录 A：证据索引

### 外部

| 项目 | 文件 | 关键位置 |
|---|---|---|
| AiNiee | `PromptBuilder.py` | `build_glossary_prompt` / `build_ntl_prompt` / `build_characterization` / `build_writing_style` / `generate_prompt` |
| AiNiee | `TerminologyChecker.py` | `run_check` / `_perform_pre_checks` / `_get_match_options` / `_collect_merged_term_rows` |
| AiNiee | `CharacterHelper.py` | `SEPARATOR_TOKEN` / `LITERAL_BRACKET_NAME_PATTERN` / `ZERO_WIDTH_CHARS` / `_normalize_name` / `is_regex_pattern` / `match_original_name` |
| TranslateBooksWithLLMs | `injector.py` | `build_glossary_block` / `build_cast_block` / `TARGET_INFLECTION_INSTRUCTION` |
| TranslateBooksWithLLMs | `glossary-models.py` | `DEFAULT_MAX_CAST_ENTRIES = 80` / `normalize_gender` / `GlossaryTerm` / `GlossaryConfig` |
| TranslateBooksWithLLMs | `glossary-filter.py` | 拉丁 `\b` / CJK 子串 / 长度优先 / 频次封顶 |
| TranslateBooksWithLLMs | `style-lint.py` | `quoted_example` / `example_marker` / `word_list` / `proper_noun` / `too_specific` |
| prozetta | `passport.js` | `PASSPORT_VERSION` (23) / `DOSSIER_TOKENS` (29) / `emptyPassport` (114) / `povForChunk` (202) / `STYLE_WORDS` (252) / `buildStyleBlock` (345) / `isEmptyPassport` (443) |
| prozetta | `glossary_hygiene.js` | `normalizeGender` / `betterSurfaceForm` / `caseDuplicates` / `inconsistentNested` / `glossaryFindings` / `applySafeFixes` |
| prozetta | `PASSPORT.en.md` | 翻译前建立 / 手工可编辑 / 重建前备份 |
| translation-workbench | `character-profiles.md` | 全文 21 行 |
| translation-workbench | `translator-style.md` | 全文 31 行，五段占位 |
| translation-workbench | `check_translation_context.py` | `DISPOSITIONS` / `normalize_text` / `parse_glossary` / `extract_markdown_section` |
| LinguaGacha | `glossary-rule.md` | `src`/`dst`/`info`/`case_sensitive` 定义与准入标准 |
| LinguaGacha | `glossary-extract.md` | 种子账本 / 五方向 / 关系图 / 交付顺序 |
| LinguaGacha | `relations.mjs` | `analyzeRelations` / `MIN_ROOT_LENGTH = 2` / `MIN_ROOT_COVERAGE = 0.5` |
| LinguaGacha | `quality-glossary.ts` | `resolve_glossary_application_state` → `none\|applied\|partial\|missing` |
| LinguaGacha | `roleplay/SKILL.md` | 冲突保护顺序 / voice 锚点 / 防退化自检 |
| Tolmach | `kaz-terminology.py` | `VALID_MODES` / `relevant_terms` / `exact_violations` / `enforce_exact_source_forms` / `fingerprint` |
| Tolmach | `kaz-quality_tests.py` | `eval_terminology_delta` (194) / `eval_entity_consistency` (384) |
| ln-translator | `glossary.py` | `CANONICAL_NAMES` / `PRONOUNS` / `migrate_genders` / `consolidate_characters` / `mark_volume_processed` |
| P-Tipp | `README.md` | 五类检查 / 大小写与词边界 / 退出码 |
| ATT | `docs/translation/terminology.md` | 术语表收录标准 / `triggers` / 命中选择规则 / 不回溯 |
| ATT | `docs/translation/candidate-validation.md` | 四类结果 / 强不变量五项 / Review 不得升级 |
| ATT | `docs/translation/task-planning.md` | 稳定装箱 / 临时 ID / Partial 重试 / 状态不参与装箱 |
| ATT | `docs/translation/prompts.md` | 四类响应格式 / `strict` 与 `free` / 保守 JSON 修复 |
| ATT | `skills/extract-game-terminology/SKILL.md` | 完整 Manual 出发 / 逐条核对 / 合并同一身份 |
| ATT | `skills/translate-with-att/SKILL.md` | 分工与交付口径 |
| KeywordGacha | `README.md` / `NERAnalyzer.py` | 术语表定位 / `check` / `find_best` / `search_for_context` |
| translation-agent-rag | `README.md` | 三库 + Router + Critic |
| AIwork4translator | `README.md` | AC 自动机 + 复数归一化 + 实时采集 |

### 本项目

| 主题 | 位置 |
|---|---|
| 两层架构边界 | `docs/architecture.md`、`README.md`（上游同步约定约 109 行） |
| 术语获取 | `src/translateFunc/get_proper.py`（全文 6 行，单次 GET，无分页） |
| 术语分析 | `src/translateFunc/matcher/proper.py:54`（`fetch_terms`）、`:65`（`analyze`） |
| 按块术语过滤 | `src/translateFunc/builder/request.py:184-193`、`266-273` |
| prompt 组装顺序 | `src/translateFunc/builder/request.py:414-442`（XML）、`:490-494`（JSON） |
| 术语渲染 | `src/translateFunc/builder/prompt.py:687-704` |
| 角色风格数据 | `src/translateFunc/translate_doc.py:96-176`（`ROLE_STYLE`）、`:177+`（`RLOE_COMPARE`） |
| 角色风格注入条件 | `builder/request.py:396-406`、`433-435`；`processor.py:2516`（`is_story`） |
| 角色表刷新时机 | `src/translateFunc/pipeline.py:210` |
| 状态效果注册 | `src/translateFunc/pipeline.py:215` |
| 规则校验器 | `src/translateFunc/validator.py:231` / `306` / `347` / `404` / `426` |
| 修复告警链路 | `src/translateFunc/processor.py:1216-1249` |
| 韩文回填检测 | `src/translateFunc/processor.py:43`；`tests/test_field_coverage.py` |
| LLC 索引教训 | `src/translateFunc/processor.py:2491-2499` |
| 远程表优先规则 | `src/translateFunc/pipeline.py:276` |
| 配置 | `src/config.yaml`（`features` / `translation` 段，无 style 键） |
| 基准 | `benchmark/runs/subval-iter-01/compare.md`、`benchmark/bench_config.yaml` |

---

## 附录 B：已列出但未读的文件（后续可按需补）

若要把 P1-1 / P1-2 推进到落地，以下内容值得补读：

**ATT**

- `docs/rpg-maker/rules.md`（39,981 B）— RPG Maker 专用规则
- `docs/runtime/openai-compatible.md` — 运行时与模型交互
- `docs/translation/placeholders.md`、`write-back-layout-rules.md`、`language.md`、`task-records.md`
- `docs/guides/diagnosis-and-recovery.md`
- `tools/formic/docs/task-design.md`（11,517 B）
- `docs/translation/lua/README.md`、`docs/manual/README.md`

**prozetta**

- `docs/GUIDE.en.md`、`docs/MONITOR.ru.md`、`docs/GLOSSARY.ru.md`
- `src_v4/stages/03_passport.js`（25,626 B）、`core/glossary_review.js`（23,220 B）、`core/language.js`（21,709 B）、`stages/05_translation_review.js`

**LinguaGacha**

- `docs/AGENT_RUNTIME.md`（40,031 B）、`docs/BACKEND.md`（42,584 B）
- `src/backend/quality/quality-prompt-service.ts`、`src/backend/batch-translation/work-unit/work-unit-prompt-builder.ts`
- `builtin/skills/translation/references/page-translate.md`
- `builtin/skills/writing-guide/references/{creative-guide,translation-guide}.md`
- `lingua-roleplay-SKILL.md` 121–259 行（已读 1–120）

**其他**

- `P-Tipp/translation-consistency-checker` 的 `consistency_checker.py`（14,925 B，实现细节）
- `AiNiee` 的 `GlossaryHelper.py`
- `neavo/KeywordGacha` 的 `module/TableManager.py`
- `machinewrapped/llm-subtrans`（默认分支非 `main`，需先取 tree）
- `CFPAOrg/Glossary`（Minecraft EN→ZH 术语库，可作为"大规模术语库治理"参照）

---

## 附录 C：一眼可用的结论

1. **匹配层不需要改。** 本项目的 AC 自动机 + 精确子串与 `AIwork4translator` 同路线，在本领域是主流选择；向量库已由 `docs/next-iteration-plan.md` 判为 premature。
2. **真正缺的是校验层。** `validator.py` 只有 4 条技能文本规则，**零条术语落位检查**。P0-1 是投入产出比最高的一步，且零模型成本、可离线回归。
3. **`note` 字段已经够用。** `render_glossary` 已在渲染 `<note>`，等价于 LinguaGacha 的 `info`；不需要新字段，只需要按准入标准收紧内容并加性别声明。
4. **文风约束的注入范围偏窄。** 角色风格只对 `StoryData` 生效（`processor.py:2516`），而有说话人的 `PersonalityVoiceDlg` / `BattleAnnouncerDlg` / 技能被动文本全部没有约束；同时 `ROLE_STYLE` 硬编码在 `translateFunc` 内，扩展它会与上游同步冲突，因此应走外部档案 + config 开关。
5. **常驻块必须带上限。** bookllm 给 CAST 设 80 条上限并注明 900 tokens 的"permanent token tax"；prozetta 给档案设 600 tokens；本项目的按块过滤已有，但常驻部分（若引入）必须同样设限。
6. **术语与文风发现只能是 Review，不能是 Rejected。** ATT 的纪律：除非能给出"权威依据 + 成立条件 + 确定性反例 + 最小拒绝范围 + 状态影响"，否则不得升级为错误、拒绝或阻断导出。
7. **不回溯已发布译文。** 所有外部项目一致；本项目已发 Release 被玩家安装，回溯风险更高。
8. **P2-1（权威语料命中即免调模型）优先级高于任何 prompt 侧调整。** `docs/converge-to-authority.md` 的 98.98% 完全相同率意味着这条路线的收益是确定性的，而 prompt 调整在当前 stage-0 噪声（152 个共同字段中 105 个不可归因）下无法验证。
9. **任何 prompt 侧 A/B 必须先固定 stage-0 行为并按桶看。** `other_term` 是唯一能反映术语一致性的分桶。
