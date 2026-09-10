# 03 · 接入新项目 / 新 JIRA 环境与故障排查

## 1. 工作原理（为什么换项目不用改代码）

工具所有「项目相关」的信息都**在运行时从 JIRA 读取**：

| 需要知道的东西 | 来源 | 机制 |
|---|---|---|
| 我是谁、连的哪台 | `GET /rest/api/2/myself` | `whoami` 一行输出（冒烟验证） |
| 可见项目列表 | `GET /rest/api/2/project` | `projects [--query]` |
| 字段 id / 类型 | `GET /rest/api/2/field` | `fields [--query]` |
| 可用状态流转 | `GET /rest/api/2/issue/{KEY}/transitions` | 按名称匹配「接受」「解决」（中文名优先，兼容英文/目标状态名）；歧义时要求 `--transition-id`；**空列表**会提示"先 assign --to me 接管" |
| 解决屏幕字段（含必填自定义字段） | 同上 + `?expand=transitions.fields` | **只有这里能拿到流转屏幕字段**（`editmeta` 拿的是编辑页，不含）；`resolve --dry-run` 会打印完整字段清单 |
| 中文标签 → 字段 id | 字段名与「影响范围/发生原因/解决方法/预防措施」归一化后互相包含匹配 | 归一化忽略空格/括号/标点差异；非中文实例改用 `--field "字段名=值"` |
| select 下拉可选值 | 同一展开里的 `allowedValues` | 注意选项键是 `value`（不是 `name`）；传值自动校验并映射为 option id |
| 字段值格式 | 字段 `schema.type` | option→`{"id":…}`；version 数组→`[{"name":…}]`；user→`{"name":…}`；文本框→原值 |
| 经办人候选 | `GET /rest/api/2/user/assignable/search` | `assign` / `resolve --assign` 用；报错会提示不在 assignable 名单的情况 |

标准 resolution 字段如出现，自动从 allowedValues 挑「Fixed/已修复」类值；很多中文工作流用的是**自定义 select 字段「解决方案」**（已修复/不修复/延迟修复…），工具按名称匹配即可，传 `--field "解决方案=已修复"`。

## 2. 新 JIRA 环境接入检查清单

1. **初始化 + 认证体检**：`init --check` 看配置状态（未初始化就 `init` 向导走一遍），`whoami` 能输出身份即认证通。JIRA Server/DC 8.x 走 Basic；有 PAT 的实例可改用 `token:`（Bearer）；Cloud 用「邮箱+API Token」当 Basic（见 §4）
2. **凭据**：按文档 01 配置并冒烟。多套环境并存用 **profile**（`--profile 名字`），别共用一份凭据来回改
3. **摸清项目**：`projects --query 关键词` 找到项目 key → `search --jql 'project = KEY …'` 验证能拉到单
4. **一次看清状态机+字段+词表**（读操作，零风险）：
   ```bash
   python3 jira.py transitions KEY          # 状态机叫法（接受/解决是否同名）
   python3 jira.py resolve KEY --dry-run    # 流转屏幕字段清单（id/名称/必填/类型）+ 词表
   ```
   `--dry-run` 输出的「流转屏幕字段清单」就是本项目要填什么的完整答案（替代旧版"故意填错值看词表"的技巧，不再需要）
5. **确认解决屏幕必填字段**：dry-run 后若报「必填字段未填」→ 用 `--field "标签=值"` 补齐（多个就写多个 `--field`）。公司常见的额外必填：修复的版本、解决方案、BUG解决人
6. **「接受/解决」类流转不唯一或按名匹配不到**：`transitions KEY` 看候选 → `--transition-id <id>` 兜底
7. **转交策略**：很多项目「解决」流转自带 post-function 会把经办人改成报告人（= 转测试），无需 `--assign`；如果你们没有，用 `--assign "测试员显示名"` 显式转交
8. **别人的单怎么处理**：工作流常限制"只有经办人能流转"（表现为 transitions 空列表/无「接受」类流转）→ 先 `assign KEY --to me` 接管再流转；详见 docs/07
9. 每个团队约定词表后，可以把词表抄进自己项目的备注文档，但**工具不依赖它**——随时以运行时输出为准

## 3. 故障排查速查

| 现象 | 处理 |
|---|---|
| `认证失败 HTTP 401/403` | 凭据错/含 `<>`/账号被 SSO 锁/该实例禁 Basic。逐项对照文档 01 |
| `凭据文件不存在` / `缺字段` | 路径或三键不全；`export JIRA_CREDS_PATH=…` 或 `--profile` 指定 |
| `找不到 profile「xx」` | 按报错里列出的可用名字创建 `profiles/xx.yaml`（文件名必须=名字） |
| `当前账号对 X 无任何可用流转` | 单子经办人不是你（工作流限制）→ `assign X --to me` 接管；或请原经办人转给你 |
| `匹配到多个可指派人`（中止） | 部分实例忽略查询词、返回全量名单→脚本已客户端过滤；用完整显示名或完整登录名重试 |
| `未初始化` / 缺字段 | 跑 `python3 jira.py init`（向导）；自查 `init --check` |
| `未设置活动项目` | `python3 jira.py use <项目>` 设置；或 --jql/--url/--project 显式指定 |
| `该查询未限定项目…被拦截` | 护栏：手写 JQL/链接没带 project；补 `project = X`，或用户确认跨项目后加 `--all-projects` |
| `查询未指向当前活动项目`（提示） | 检查 JQL 是否写错项目；临时看别的项目先 `use` 切换（确要跨项目加 `--all-projects`） |
| 在项目目录里裸跑 search 自动换了项目 | 这是**项目文件夹配置**（`.jira-project.yaml`）在就近生效——正常；`pconfig --here` 查看/修改，离开目录恢复活动项目 |
| `当前状态「X」已不是「未开始」` | start 安全护栏：单已处理中/终态；确要流转用 `--transition-id` |
| 项目名记不住 / 想换项目 | `use` 看已登记清单（序号/前缀切换）；`projects --query` 全量搜 |
| `HTTP 400 … “resolution”域中没有…` | 你手写 JQL 里用了本项目没有的 resolution 词（如 Fixed）→ 用 `resolution is not EMPTY` 或 `statusCategory = done` |
| JQL 状态条件**静默返 0**（明明有数据） | 本实例状态名本地化：中文「未开始」在 JQL 中不命中，须用内部名并加引号 `status = "Initial"`；其余状态名以实测为准（Working / ST Check 等英文名可直接用） |
| 选项值不在词表 | resolve 自动列出可选项 → 换词重跑（**已在 POST 前中止，单子未动**） |
| 「接受/解决」类流转不唯一或找不到 | 输出会列出候选 → `--transition-id <id>` 强制指定（先 `transitions KEY` 查 id） |
| 必填字段未填，已中止 | 预检会列出缺失字段 id+名称 → `--field "标签=值"` 补齐；`--dry-run` 可先看全部字段清单 |
| 流转成功但读回不符 | `transitions KEY` 看当前状态可用流转再决定下一步；工具已把每个字段读回结果打出来 |
| 贴了 browse 链接报「没有 jql 参数」 | browse 是单页链接；用过滤器页地址（`/issues/?jql=…`），或手动把 JQL 传给 `--jql` |
| `[重试] HTTP 503 …` / `429` | 服务端抖动自动重试（≤2 次，尊重 Retry-After）；持续失败查网络/VPN |
| `[警告] 证书校验失败，已退回不校验重试` | 内网自签证书，正常；在意可给系统装公司根证书后删除降级逻辑 |
| 想先看"会发什么"再决定 | 所有写操作加 `--dry-run`：打印完整 payload + 字段清单，零写请求 |
| 多个环境怕操作串 | 用 `--profile` 分开凭据；写操作输出都带 `@ 实例` 提醒；`whoami` 随时核对 |
| 中文乱码 | Windows 用 git-bash/PowerShell/VS Code 终端；脚本已内置 UTF-8 重连 |
| 请求超时/网络错误 | 检查内网/VPN；urllib 自动读取 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 |
| 密码含 `#` 被截断 | `#` 前带空格才会被当注释；避免 `abc #def` 写法 |

## 4. 扩展：Jira Cloud 与其他认证方式

v2 起认证已内置三种，**Server/DC 开箱即用**：

| 目标 | 凭据写法 | 状态 |
|---|---|---|
| Server / DC（Basic） | `username` + `password` | ✅ 开箱即用 |
| Server PAT（8.14+/DC） | `token: xxxxxx`（Bearer） | ✅ 开箱即用 |
| Jira Cloud | `username: 邮箱` + `password: API Token` | ✅ 主要流程可用；注意 §4.1 差异 |

Cloud 的 REST 基本同构：`transitions?expand=transitions.fields`、中文/英文标签匹配、词表校验机制在 Cloud 同样成立；需要适配的差异集中在：

1. **user 字段**：Server 用 `{"name": …}`，Cloud 用 `{"accountId": …}`（用户搜索返回 accountId）——`assign` / `resolve --assign` / user 类型自定义字段在 Cloud 上可能要小改 `_wrap_value()` 与 `_resolve_user()`（约 10 行）
2. JQL 用户名体系：Cloud 的 `assignee` 用 accountId 或邮箱，写法与 Server 不同
3. Cloud 无「PAT」概念，一律邮箱+API Token
