# 更新日志

## v2.2.0（2025-09）

**防跨项目跑偏 + 项目级拉单模板**

- `search` 硬护栏：手写 `--jql`/`--url` **未限定 project 时默认被拦截**（报错给出两条路：改写当前项目 / 跨项目显式 `--all-projects`）；查询指向的不是当前活动项目时给提示（stderr，不拦截）
- 新增 `pconfig`：项目级拉单模板管理（查看/设置/删除）。模板文件 `hermes/jira-project-configs/<KEY>.yaml`；优先级：**项目配置 > 全局配置（default_jql）> 内置默认**；`search` 输出的 `[项目]` 行会标明当前模板来源
- `init --check` 增加项目级模板清单显示
- `assign`/`resolve --assign` 指派人解析加固：部分实例忽略查询词返回全量名单，改为**客户端按显示名/登录名兜底过滤**（精确 → 归一化子串唯一命中）；多候选**列出名单并中止**（不再静默取第一个）；拉取上限 20→200
- 自测扩到 57 项离线断言（含 pconfig 全流程、三级模板优先级、护栏拦截/放行/提示、指派人解析三例）

## v2.1.0（2025-09）

**初始化向导与「活动项目」**

- 新增 `init`：初始化向导（填账号 → 现场验证 → 勾选登记项目 → 设活动项目）；`init --check` 一行体检（返回码 0=已就绪）；支持非交互参数（--base-url/--username/--password/--token/--projects/--use）
- 新增 `use`：查看/切换当前活动项目（序号、KEY、前缀模糊；未登记的项目先到实例校验再自动登记）
- **活动项目聚焦**：`search` 不带条件 = 当前活动项目里我的未解决（默认 JQL 模板可用配置 `default_jql` 改）；`search --project KEY` 临时换（ALL=不限项目）；纯数字单号（`issue 25`）自动补活动项目前缀；操作其他项目的单给出提示
- 配置文件新增 `projects` / `active_project` / `default_jql` 键；写回为"原地更新 + 原子写入"，保留注释与其余内容
- `start` 安全护栏：当前状态已不是「未开始」时拒绝执行并列出可用流转（修掉旧版兜底在个别项目上会误选「解决」类流转的问题）；「接受」名称匹配不到时，按目标状态（working/处理中）单一候选兜底
- 自测扩到 41 项离线断言（覆盖 init/use/活动项目/护栏全路径）

## v2.0.0（2025-09）

**更通用：换项目 / 换实例 / 多账号**

- 新增凭据 **profile** 机制：`--profile 名字` 读取 profiles 目录下 `名字.yaml`（`JIRA_PROFILE` / `JIRA_PROFILES_DIR` 环境变量同样支持），多实例并存互不干扰
- 认证支持 **Bearer token**（Server PAT）；Cloud「邮箱+API Token」走 Basic 即可
- 凭据文件新增可选键：`token` / `insecure` / `timeout`
- 新增 `whoami`（身份+实例冒烟）、`projects`（列项目）、`fields`（查字段 id/类型）——接入新环境先跑这三个
- `search --url`：直接贴 JIRA 过滤器链接（自动提取 JQL），browse 链接会给出明确提示
- `search` 超 100 条自动分页；新增 `--all` 取全量；表格新增「经办人」列
- 429/5xx/网络抖动自动重试（最多 2 次，尊重 Retry-After）；内网自签证书自动降级为不校验重试

**更丰富：改别人的单 / 协作 / 安全预演**

- 新增 `assign`：改经办人（`--to me` 一行接管别人名下的单），执行后读回验证
- 新增 `comment`：加评论（`--body` / `--body-file`），支持 JIRA wiki 语法（`[~登录名]` @人）
- 新增 `attachments`：下载单子附件（截图等）
- 全部写操作（start/resolve/assign/comment）支持 **`--dry-run` 预演**：打印将发送的完整请求与流转屏幕字段清单，零写请求
- `resolve` 增强：执行后**逐字段读回校验**（不只状态）；支持 `--comment` 流转附评论；无可用流转时给出「先 assign --to me 接管」提示
- `issue` 输出新增附件清单；支持 `--format json`

**修复**

- 选项值校验提前到必填检查之前（错误提示更贴近真实问题）
- 沿用：写操作后读回验证、词表预校验中止、零第三方依赖

**工程**

- 新增 `scripts/selftest_offline.py`：离线 mock 自测（26 项断言，零真实请求，改完脚本跑一遍）

## v1.0.0

- 首个版本：`search` / `issue` / `transitions` / `editmeta` / `start` / `resolve`
- JIRA Server 8.x REST + Basic 认证；动态发现流转 id/字段 id/词表；写操作读回验证
