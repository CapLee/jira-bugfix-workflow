---
name: jira-bugfix-workflow
description: Use when 用纯数据流（REST API）处理 JIRA bug——拉单/读单/改码/接受/解决/建单/附件，含改别人的单、多实例切换。不要浏览器点击时。
version: 2.5.0
---

# JIRA Bug 纯数据流处理（jira.py）

工具 = `scripts/jira.py` 单文件（Python 3 标准库，零依赖），全程 REST API，零浏览器。
安装：`git clone <本仓库>`；凭据配置与完整命令手册见 `docs/01`、`docs/02`，场景剧本见 `docs/07`。

## 何时用

- 用户要「拉 JIRA bug / 改 bug / 更新状态」，且环境能直连 JIRA REST（Server/DC 8.x Basic，或 Bearer/Cloud）
- 用户要求「不要浏览器 / 不要模拟点击 / 纯数据流」
- 用户要帮改**别人名下的单**、切换**多个 JIRA 实例/账号**（v2 能力）
- 用户要**建单 / 上传附件 / 查变更历史**（v2.4：`create` / `attachments --upload` / `changelog`）

## 日常使用

- 「拉一下我的 Bug / 继续处理」：沿用当前目录 `.jira-project.yaml` 和已选活动项目，默认只拉当前项目分配给我的未解决单；已有配置直接使用，不反复跑初始化、不重问账号和项目。
- 「看看 / 分析一下」：读齐描述、最后评论、变更历史和必要截图，给根因与建议，不自动改代码或流转。
- 「修一下 / 这些都改 / 按你说的改」：范围清楚就直接定位、修复、验证；不为了固定每轮数量要求再次圈选。确有方案分歧或影响范围不清才问。只留工作区，不顺手接受、解决、评论或提交代码。
- 「我已经修好了，流转给测试」：先查当前单状态、报告人与实际修复提交，再按授权流转；已说明范围且有既定字段口径，不重复问整套流程。默认转本单报告人，不固定转某个人。
- 「加条备注：……」：用户原文作为评论发送，不自行改写，不额外加另一条说明；没有明确要评论就不顺手评论。
- 重开单先读最后一次变更和最新评论，不照旧调查结论重做；用户已自行修复或流转的单只补缺少的动作。

## 首次使用：先初始化

- 默认地址 `https://ticket.quectel.com`。首次才运行 `init --check`；未就绪用 `init --dialog` 补齐，日常不重复让用户初始化。
- 凭据只走本工具自己的账号密码：`init --dialog` 在本机打开一次用户名/密码弹窗，工具先请求 JIRA 核对身份，验证成功才写入配置；取消、认证失败或账号不匹配都不改动原配置。密码不回显，不进聊天、命令参数或日志。
- 已有配置直接使用，不重复弹窗；改密码重跑 `init --dialog`。指定的 profile 或 `--config` 不存在就停止，不偷偷换账号。不再依赖 quectel-cli 插件授权；公司 SSO 登录不能代替 JIRA 账号。
- 凭据文件（flat yaml：base_url / username / password，可选 token / insecure / timeout / projects / active_project / default_jql），默认 `%LOCALAPPDATA%\hermes\jira-api-creds.yaml`（macOS/Linux `~/hermes/…`）；多实例：`hermes/jira-profiles/<名字>.yaml` + `--profile <名字>`
- **活动项目**：`use` 查看/切换；不带条件的 search、纯数字单号都聚焦当前活动项目
- **项目文件夹配置（v2.3）**：在项目根目录放 `.jira-project.yaml`（`pconfig --here --set-project <KEY> --default-jql '…'` 写入）→ 在该目录（含子目录）裸跑 search / 纯数字单号都以它为准；优先级 **项目文件夹 > hermes 项目配置 <KEY>.yaml > 全局配置 > 内置默认**（显式 --jql/--url/--project 永远最高）
- 冒烟：`python3 scripts/jira.py whoami`（身份+实例+活动项目一行看清）

## 命令速查（全部 `--help` 可用）

```
init [--check]                           # 初始化向导 / 体检（首次必跑）
use [KEY]                                # 查看/切换当前活动项目
pconfig [KEY] [--default-jql '…']        # hermes 项目配置（<KEY>.yaml）
pconfig --here [--set-project KEY] [--default-jql '…']  # 项目文件夹配置（.jira-project.yaml，就近生效）
whoami                                   # 身份/实例/活动项目（冒烟首选）
search                                   # 不传条件 = 当前活动项目我的未解决
search --jql '…' [--max|--all] [--format table|json|md]   # 拉清单，自动分页
                                                          # 手写 JQL/链接未限定 project 会被拦截；跨项目要 --all-projects
search --project KEY                     # 临时只看某项目（ALL=不限）
search --url '…/issues/?jql=…'           # 贴过滤器链接
triage [KEY …] [--max 10] [--format json|md] [--save f]  # 一次拿齐资料，标记完整性
issue KEY [--save f.md]                  # 详情+评论+附件
changelog KEY [--format table|json]      # 变更历史（谁改了什么：流转/字段/指派/链接）
attachments KEY [--save-dir 目录]         # 下载附件（截图）；--list 只列表；--upload 文件… 上传（+--comment/--dry-run）
transitions KEY                          # 可用流转（空列表=不是你的单）
start KEY [--dry-run]                    # 接受（未开始→Working）
resolve KEY --repo <业务仓库> --fix-commit <修复SHA> --impact … --cause … --solution … --prevention … \
  [--field '标签=值']… [--assign 人] [--comment 备注] [--dry-run]   # 解决+填字段+转交
create --project X --type 类型 --summary '…' [--description '…'/--description-file f] [--priority P] [--assign 人] [--field '标签=值']… [--dry-run]
                                         # 建单（建单屏幕必填/词表动态校验）
assign KEY --to me|姓名 [--dry-run]       # 接管/转派经办人
comment KEY --body '…' [--dry-run]        # 评论（@人写 [~登录名]）
batch plan --file input.json             # 保存批量清单与快照，不写 JIRA
batch run|resume <任务文件> [--execute]  # 默认预演，明确授权后才加 --execute
batch status <任务文件>                 # 查看本地结果
projects / fields [--query]              # 探查项目/字段（接入新环境）
```

## 标准流程（六步）

1. `search` 拉当前项目清单并编号呈现；用户已点名单号或授权范围时直接处理，不重复要求圈选。只在目标不明确时问。
2. `triage KEY …` 一次读取范围内单的详情、最新评论、附件清单、历史、流转；检查 `complete/total/fetched`，不完整不能当无记录（需要时 `attachments` 下截图）
3. 用户已要求修复就改码 + 自验证（改动只留工作区，**不 git commit**；涉及表/数据交付 DDL/DML），不顺手改变工单状态。
4. 只有用户明确说接受或流转才执行 `start KEY`，并读回验证。
5. 用户明确授权流转后，`resolve … --repo <业务仓库> --fix-commit <修复SHA>` 核对修复已推送，填四项必填 + 版本 + 转交（默认逐单报告人）；工具不自动提交/推送。多单用 `batch plan/run/resume`，说明见 `docs/08-生产批量工作流.md`
6. 汇报：单号+根因+改动文件+验证结果+状态

## 改别人的单（工作流常只允许经办人流转）

1. 探路：`transitions KEY` 或 `resolve KEY … --dry-run`——空列表/无可用流转 = 权限不够
2. 接管：`assign KEY --to me`（先经用户/原负责人同意——这是公开变更记录）
3. 照常 start → 修 → resolve；交回方式按约定（自动转报告人 / `--assign` / 转回原负责人）
4. 只有用户要求评论时才留痕，评论内容沿用其原话；不擅自追加「代为处理」评论。

## 铁律（写操作三保险 + 范围）

- **范围铁律**：默认只查当前活动项目。用户没明说「跨项目/所有项目」时：不得用 `--all-projects`、不得写不带 project 的 JQL、不得 `--project` 别的项目；查询指向别的项目先停下确认或先 `use` 切换。工具对手写 JQL/链接已有硬护栏，不要尝试绕过。
- start / resolve / assign / comment / create / 附件上传 **永远等用户明确指令**；拿不准先 `--dry-run` 预演给用户过目（零写请求，打印完整 payload + 字段清单）
- 执行后**必须读回验证**（脚本自动做：状态 + 每个已填字段逐项核对）
- transition id / 字段 id / 下拉词表**永远运行时发现**，不硬编码；词表填错会在 POST 前中止并列出可选项
- 多实例用 `--profile`；执行前 `whoami` 核实例/账号；批量任务绑定实例/账号/项目，切错即停止
- 幂等保护：同作者完整评论、同名同大小且内容哈希一致附件不重发；完成项续跑必须重新读回。JIRA 无服务端幂等键，不承诺跨机器 exactly-once。
- `resolve --dry-run` 可无提交证据只探查字段，但结果会标明未验证交付；正式执行始终需要门禁。Windows 自测与 Git/Python 子进程显式无窗口启动，避免逐用例弹 CMD。

## 排错速查

| 现象 | 处理 |
|---|---|
| 401/403 | 凭据/SSO；视环境降级浏览器方案 |
| 未初始化 / 未设活动项目 | `init` 向导 / `use <项目>`；`init --check` 自查 |
| start 返回 skipped | 已 Working/QA/终态，安全跳过；不要用指定 id 强转 |
| 代码交付检查未通过 | 提供本单实际修复 SHA，核工作区、上游与最新远端；不凭干净工作区判已修 |
| 结果待核对 / 任务记录损坏 | 保留记录；先查远端结果，不删日志重试写入 |
| `当前账号对 X 无任何可用流转` | 不是你的单 → `assign X --to me` |
| JQL 里 `status = 未开始` 静默返 0 | 本实例用内部名：`status = "Initial"`（=未开始；保留字须加引号） |
| 单「更新」翻转但内容疑似无变化 | `changelog KEY` 看变更历史（常见为改字段/补链接/附件，未必是评论）；元数据调整通常无需处理 |
| 指派时提示「匹配到多个可指派人」 | 部分实例忽略查询词返回全量名单→脚本已客户端过滤；用完整显示名或完整登录名重试（不会乱指派） |
| 无「接受/解决」类流转或歧义 | `transitions KEY` 看候选 → `--transition-id` |
| 必填未填 | `--dry-run` 看屏幕字段清单 → `--field '标签=值'` |
| 建单必填未填 / 类型不存在 | `create --dry-run` 会列出可建类型与建单屏幕必填；用 `--field '标签=值'` 补齐 |
| 附件上传失败 | 工具已自带 `X-Atlassian-Token`；仍 403 查该账号是否可传附件 |
| 贴 browse 链接报无 jql | 改用 `/issues/?jql=` 过滤器链接 |
| 429/5xx | 读取自动重试（≤2 次）；写入结果不确定先读回，不盲重发 |
| 中文检索失败（英文/数字正常） | 先查正文零宽字符（U+200B 等，AI 生成文档常见「每字夹 ZWSP」）→ 旧码全 0 命中属历史缺陷；先核构建时间线再动码 |

## 巡检先核「构建时间线」（2026-10）

QA 复测跑在**已部署构建**上，构建可能落后代码多天——新单先核再决定动不动码（实测：dev 前端最新构建停在数日前，当天修复全未上环境；QA 新报「中文无法检索」实为**已提交未构建**的零宽字符修复场景，码侧无需再改）。

流程：① 当前代码复现（决定改不改码）② 核构建最后 sha/时间（Jenkins API `<job>/api/json?tree=builds[number,result,timestamp]` + 构建 `lastBuiltRevision.SHA1`）③ `git merge-base --is-ancestor <构建sha> <修复commit>` 成立 → 「码侧已修，待下次构建复测」，如实告知、勿重复改码。
