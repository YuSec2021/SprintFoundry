# Quality Gate — 代码质量门禁

质量门禁是位于 **Orchestrator 提交 Generator 产物之后、Evaluator 黑盒验证之前** 的独立检查阶段。
它由 Orchestrator 通过 Bash 运行，不依赖任何 agent 的主观判断。

目标：把"代码内部质量"与"黑盒功能验证"分离，各自有独立的失败通道和修复循环。

**硬性约束（对所有 sprint、每一项更新生效）**：除静态分析（lint/type/coverage/audit）
和 Evaluator 的代码 review 之外，任何改动了应用源码的 sprint 都**必须**附带对应的
自动化测试脚本。质量门禁内置的 **test-presence** 检查会对 sprint 的 diff 做静态判定：
源码有改动但没有新增/修改任何测试文件 → 直接 FAIL。纯文档/配置/标记（md、json、
yaml、html、css 等）改动豁免——它们没有可测的行为，且各自有独立的 lint 门禁。

---

## 目录

1. [在 Sprint 门控中的位置](#1-在-sprint-门控中的位置)
2. [质量门禁脚本（Orchestrator 运行）](#2-质量门禁脚本)
3. [各语言工具配置](#3-各语言工具配置)
4. [覆盖率阈值](#4-覆盖率阈值)
5. [安全审计](#5-安全审计)
6. [.sprintfoundry/results/quality/quality-gate-N.md 格式](#6-sprintfoundryresultsqualityquality-gate-nmd-格式)
7. [失败处理](#7-失败处理)
8. [Evaluator 如何使用质量门禁结果](#8-evaluator-如何使用质量门禁结果)

---

## 1. 在 Sprint 门控中的位置

```
③ IMPLEMENT (Codex writes commit request; Orchestrator commits + writes .sprintfoundry/signals/eval-trigger.txt)
        │
        ▼
   Rule 2.1: QUALITY GATE  ◀── lint / type / coverage / audit
        │                       + test-presence（源码改动必须带测试）
        │                       + feature-gate（功能型 sprint 必须带 §2b 回归测试 + §3 案例）
        │                       + duplication（只报告与本 sprint 修改行相交的 clone）
   PASS ├──────────────────▶ ④ EVALUATE (Evaluator 黑盒验证)
        │
   FAIL └──────────────────▶ Codex 修复质量问题（含补测试脚本）
                              quality_retry_count++
                              写新的 commit request
                              (不消耗 Evaluator retry_count)
```

质量门禁失败走独立的修复循环，**不计入** Evaluator 的 `retry_count`。
超过 `quality_retry_count > 2` → pause，`needs_human=true`。

---

## 2. 质量门禁脚本

Orchestrator 在检测到 `.sprintfoundry/signals/eval-trigger.txt` 后、调用 Evaluator 前，运行此脚本：

```bash
python3 <scripts>/quality_gate.py --project-dir "$ROOT" --sprint N
```

**你不需要读这个脚本，也不需要手动运行它。** `orchestrate.py` 在检测到缺少门禁
报告时会自己调用 `scripts/quality_gate.py`、认证结果、然后继续路由——整个过程在
同一次调用内完成，不消耗额外的模型往返。本文档只描述门禁**检查什么**以及
**失败了怎么修**；实现在 `scripts/quality_gate.py`。

脚本退出码 0 = PASS，1 = FAIL；报告写入
`.sprintfoundry/results/quality/quality-gate-{N}.md`。

---

## 3. 各语言工具配置

### JavaScript / TypeScript

| 工具 | 用途 | 失败条件 |
|------|------|---------|
| ESLint | 语法/风格检查 | 任何 warning（`--max-warnings=0`）|
| tsc | 类型检查 | 任何类型错误（`--noEmit`）|
| jest + coverage | 单测 + 覆盖率 | 行覆盖率 < 70% |
| npm audit | 依赖安全 | high 或 critical 漏洞 |

推荐 `.eslintrc` 最低配置（若项目无配置，Generator 须在 Sprint 1 建立）：
```json
{
  "extends": ["eslint:recommended"],
  "rules": { "no-unused-vars": "error", "no-console": "warn" }
}
```

### Python

| 工具 | 用途 | 失败条件 |
|------|------|---------|
| flake8 | PEP8 + 语法 | 任何 error/warning |
| mypy | 类型检查 | 任何类型错误 |
| pytest --cov | 单测 + 覆盖率 | 行覆盖率 < 70% |
| pip-audit | 依赖安全 | 任何已知漏洞 |

Python 工具必须通过本地 `uv` 运行。质量门禁先读取项目声明的 Python 版本：
`SPRINTFOUNDRY_PYTHON_VERSION`、`.python-version`、`runtime.txt`、
`pyproject.toml [project].requires-python`，最后才兜底到当前 `python3`
的 major.minor。随后用同版本执行：

```bash
uv run --python <version> --with pytest --with pytest-cov pytest --cov=. --cov-fail-under=THRESHOLD -q
```

`flake8`、`mypy`、`pip-audit` 同样用 `uv run --python <version> --with <tool>`，
不要安装到系统 Python，也不要使用 `--break-system-packages`。

### 前端静态资源（HTML / CSS / 原生 JavaScript）

这一组检查**按文件存在性触发**（而非 `tech_stack` 关键词），因此即便是不带
框架的纯静态站点也能覆盖。工具通过 `npx --yes` 按需拉取。

| 工具 | 用途 | 触发条件 | 失败条件 |
|------|------|---------|---------|
| htmlhint | HTML 结构/属性检查 | 存在 `*.html` / `*.htm` | 任何 htmlhint 默认规则报错 |
| stylelint | CSS 语法/规则检查 | 存在 `*.css` | 任何 error 级规则报错 |
| eslint (vanilla) | 原生 JS 检查（`.js/.mjs/.cjs`）| 存在 JS 文件且上文框架分支**未**跑过 ESLint | 任何 warning（`--max-warnings=0`）|

要点：

- **htmlhint** 自带默认规则集，无需项目配置即可运行。
- **stylelint** 需要配置：优先使用项目自带的 `.stylelintrc*` / `stylelint.config.*`；
  若不存在，门禁会在 `.sprintfoundry/results/quality/.stylelintrc.json` 写入一份
  自包含规则集（不含 `extends`，无需额外安装配置包），不污染项目根目录。
- **原生 JS 的 ESLint** 复用项目的 ESLint 配置（Generator 应在 Sprint 1 建立）。
  与框架分支的 ESLint 互斥：若框架分支已跑过 `eslint`，这里不再重复执行。
- 所有前端检查都会跳过 `node_modules`、`dist`、`build`、`.git` 等目录。

### 其他栈

若某栈既不属于上述 Python / JS-TS / 前端三类（例如 Go、Rust、Java），
Orchestrator 记录 `.sprintfoundry/results/quality/quality-gate-N.md` 为"栈未识别，跳过静态分析"，
**不因此失败**，但 Evaluator 须在 Craft 评分中注记"缺少静态分析覆盖"并适当扣分。
注意：即使栈未识别，下面的 **test-presence** 门禁仍然生效。

### duplication（重复代码证据，对所有代码栈生效）

质量门禁使用 `jscpd` 扫描仓库，但不会用整个仓库的重复率惩罚当前 sprint。
脚本先依据 sprint fence/base commit 解析 Git 修改行，再只保留与这些修改行相交的
clone pair。结果是交给 Evaluator 做复用审查的**候选证据**，不是缺陷的自动证明。

优先使用 PATH 上已有的 `jscpd`；否则使用 `npx --yes jscpd@5`。默认忽略依赖、构建产物、
生成代码、fixtures、snapshots、migrations、测试和 examples，减少结构性重复误报。

在 `SPRINTFOUNDRY.md` 中可配置：

```text
duplication_gate: warn
duplication_min_lines: 10
duplication_min_tokens: 70
duplication_max_new_clones: 0
```

| 模式 | 行为 |
|------|------|
| `warn`（默认） | 有候选时写 `⚠️ duplication`，Quality Gate 仍可 PASS；Evaluator 必须逐项裁决 |
| `fail` | 候选数超过 `duplication_max_new_clones` 时 Quality Gate FAIL；工具缺失或报告不可解析时 fail-closed |
| `off` | 写入跳过记录，不运行重复检测 |

对应环境变量为 `SPRINTFOUNDRY_DUPLICATION_GATE`、
`SPRINTFOUNDRY_DUPLICATION_MIN_LINES`、`SPRINTFOUNDRY_DUPLICATION_MIN_TOKENS` 和
`SPRINTFOUNDRY_DUPLICATION_MAX_NEW_CLONES`，环境变量优先于项目文件。

Evaluator 对候选 pair 检查行为是否真正相同，并按以下顺序做复用审查：仓库已有实现 →
标准库 → 平台/数据库原生能力 → 已安装依赖 → 最小新增代码。只有有明确证据且缺少合理约束时，
才判定为复用违规。

### test-presence（测试脚本存在性门禁，对所有 sprint 强制）

| 项 | 说明 |
|------|------|
| 触发条件 | **每个 sprint 都跑**，与栈无关 |
| 判定方式 | 静态比对 sprint 的 diff（fence 的 `base_commit` → 与 base 分支的 merge-base → 首提交回退 `git diff-tree HEAD`） |
| 失败条件 | diff 改动了应用源码（`.py/.js/.ts/.jsx/.tsx/.go/.rs/.java/.vue/.svelte/...`）但**没有**新增或修改任何测试文件 |
| 豁免 | 纯文档/配置/标记改动（md、json、yaml、toml、html、css、图片等）——无可测行为，另有各自 lint 门禁 |

测试文件识别规则：路径含 `tests/`、`test/`、`__tests__/`、`spec/`、`e2e/`；或文件名匹配
`test_*`、`*_test.py`、`*_test.go`、`*.test.*`、`*.spec.*`。

失败时 Codex 走标准 `quality_retry` 循环补写测试脚本（不消耗 Evaluator 的 `retry_count`）。
因为比对的是**整段 sprint diff**（base..HEAD），后续只修 lint 的 quality-retry 不会因此反复失败——
只要该 sprint 累计已包含测试即通过。

> test-presence 只保证"改动带测试文件"。**每一条 success criterion 是否都有对应测试**由
> 契约 schema（Generator 写 `Automated test:`）和 Evaluator 的 CHECK 联合把关，见 AGENTS.md
> 与 `references/evaluator-agent.md`。

### feature-gate（SPRINTFOUNDRY §2b 回归测试 + §3 案例的确定性闸门）

test-presence 之上再加一道，作为 Evaluator 语义判定 §2b/§3 的**确定性兜底**。

| 项 | 说明 |
|------|------|
| 触发条件 | `run-state.json` 的 `sprint_origin ∈ {feature, minor_feature, major_feature, replan}`，且该 sprint 改动了声明目录之外的**应用源码** |
| 读取来源 | 从 `SPRINTFOUNDRY.md` 读取 `feature_tests_dir`、`examples_dir`、`sprint_tests_dir`（取 `<placeholder>` 之前的稳定前缀），兼容非默认布局 |
| 失败条件 | 改了应用源码但 diff **没有**触及 `feature_tests_dir`（§2b 缺回归/CRUD 测试）或 `examples_dir`（§3 缺案例）|
| 跳过条件 | `SPRINTFOUNDRY.md` 不存在或未声明目录；`sprint_origin` 为 `bugfix`；或 `SPRINTFOUNDRY.md` 内写了 `feature_gate: off`（脚手架阶段可临时关闭）|

"应用源码"= 声明目录（`feature_tests_dir` / `examples_dir` / `sprint_tests_dir`）**之外**的
代码文件——所以纯粹只改案例或功能测试本身、或纯脚手架（`feature_gate: off`）都不会误报。
失败同样走 `quality_retry` 循环补齐。§1 架构漂移、§2b/§3 的"测试是否真的覆盖了该功能"这类
需要判断的部分，仍由 Evaluator 兜底。

---

## 4. 覆盖率阈值

| 阶段 | 最低行覆盖率 | 说明 |
|------|------------|------|
| Sprint 1–3 | 50% | 早期搭建阶段宽松 |
| Sprint 4+ | 70% | 核心功能稳定后收紧 |
| bugfix sprint | 80% | 修复代码必须有对应测试 |

Sprint 编号从 `.sprintfoundry/state/run-state.json.current_sprint` 读取。
阈值判断逻辑内嵌于质量门禁脚本：

```python
sprint_num = int(run_state.get("current_sprint", 99))
origin     = run_state.get("sprint_origin", "feature")
threshold  = 80 if origin == "bugfix" else (50 if sprint_num <= 3 else 70)
```

---

## 5. 安全审计

### npm audit（Node.js 项目）
```bash
npm audit --audit-level=high
# 只在 high / critical 漏洞时失败；moderate 及以下只记录，不阻塞
```

### pip-audit（Python 项目）
```bash
uv run --python <project-python-version> --with pip-audit pip-audit --desc
# 任何已知 CVE 均失败
```

### 失败处理
安全漏洞失败 **不计入 quality_retry_count**——因为漏洞修复通常需要升级依赖，
可能超出当前 sprint 范围。Orchestrator 应：
1. 记录漏洞详情到 `.sprintfoundry/results/quality/quality-gate-N.md`
2. 自动生成 `change-request.md Type: minor_feature` 描述需要升级的包
3. 将当前 sprint 标记为通过（漏洞单独处理）
4. 在 `.sprintfoundry/claude-progress.txt` 注记：`[SECURITY] Sprint N 遗留漏洞，已创建 change-request`

---

## 6. .sprintfoundry/results/quality/quality-gate-N.md 格式

```markdown
# Quality Gate — Sprint {N}

**Verdict: PASS / FAIL**

## ✅/⚠️/➖/❌ eslint
```
{工具输出，最多 800 字符}
```

## ✅/❌ tsc
```
{工具输出}
```

## ✅/❌ jest-coverage
```
{覆盖率报告摘要}
```

## ✅/❌ npm-audit
```
{漏洞报告}
```
```

Orchestrator 将此文件路径传递给 Evaluator，Evaluator 读取后纳入 Craft 评分。

---

## 7. 失败处理

### quality_retry_count 与 retry_count 的区别

| 计数器 | 归属 | 计什么 | 上限 |
|--------|------|--------|------|
| `quality_retry_count` | Orchestrator | Quality Gate 失败次数（同一 sprint） | 2 |
| `retry_count` | Orchestrator | Evaluator SPRINT FAIL 次数 | 2 |

两者独立。Quality Gate 失败不增加 `retry_count`，反之亦然。

### Quality Gate FAIL → Codex 修复提示词

```
Sprint {N} 的代码质量检查失败。
请阅读 .sprintfoundry/results/quality/quality-gate-{N}.md，修复所有标记为 ❌ 的问题：
- lint 错误：修复所有报告的代码风格和语法问题
- 类型错误：补全缺失的类型标注，修复类型不匹配
- 覆盖率不足：为未覆盖的分支补写单测
不要修改已通过的功能逻辑，只修复质量问题。
修复完成后写 .sprintfoundry/signals/commit-requests/sprint-{N}.json，
attempt 使用 "quality_retry"。不要运行 git commit，不要改 `.sprintfoundry/signals/eval-trigger.txt`。
STOP 后不要执行其他操作。
```

### Quality Gate 超过重试上限

```
quality_retry_count > 2
→ set `.sprintfoundry/state/run-state.json`: mode="paused", needs_human=true
  last_failure_reason="quality gate failed after 2 retries — sprint {N}"
  append to `.sprintfoundry/claude-progress.txt`: "PAUSED: 质量门禁连续失败，需人工介入"
```

---

## 8. Evaluator 如何使用质量门禁结果

Evaluator 在 CHECK 阶段开始前读取 `.sprintfoundry/results/quality/quality-gate-{N}.md`（若存在）。
质量门禁脚本会把旧版根目录 `quality-gate-*.md` 迁移到该目录；旧根目录读取仅作为迁移兼容兜底，新文件不得再写到项目根目录。

```bash
cat .sprintfoundry/results/quality/quality-gate-{N}.md 2>/dev/null \
  || cat quality-gate-{N}.md 2>/dev/null \
  || echo "[no quality gate result]"
```

**Craft 评分影响规则：**

| 质量门禁状态 | 对 Craft 评分的影响 |
|------------|-------------------|
| PASS（所有工具通过） | 无额外扣分 |
| PASS（duplication 为 WARN） | Evaluator 必须裁决 clone 候选；确认的轻微问题扣 Craft，明确且实质性的复用违规判 FAIL |
| PASS（部分工具跳过，栈未识别） | 记录"缺少静态分析"，Craft 上限降为 8/10 |
| FAIL（不应发生，但若 Orchestrator 跳过了质量门禁） | Craft 直接 ≤ 5/10，注明"未经质量门禁" |

Evaluator **不重新运行**静态分析工具——它信任 quality gate 的扫描结果，但必须独立
检查 duplication 候选和复用阶梯。`repo-reuse`、`stdlib`、`native`、`installed-dep`、
`yagni`、`shrink` finding 必须带具体路径、symbol、API 或 manifest 证据。黑盒功能验证保持不变。
