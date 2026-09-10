# jira-bugfix-workflow

JIRA Bug 处理「纯数据流」命令行工具：**拉清单 → 读详情 → 改代码 → 更新状态 → 协作流转**，全程走 REST API，零浏览器、零第三方依赖（Python 3 标准库单文件）。

解决的是「让 AI（或人）不靠模拟浏览器点击，直接通过数据流转处理 JIRA bug」的问题。工具 = **一个 Python 文件** + 一份凭据配置；docs/ 是完整说明书，任何 AI 助手（Hermes / Claude Code / WorkBuddy / Cursor…）读了文档后都能驱动它执行完整 bug 处理流程；仓库根目录的 [`SKILL.md`](SKILL.md) 可直接装进支持 skill 的 AI 工具当技能用。

## 特性

- **纯 API 数据流**：JQL 拉清单（可直接贴过滤器链接）、读详情+评论+附件、查流转、「接受」、填字段流转「解决」+转交、改经办人、加评论——全部 REST 直连
- **动态发现，零硬编码**：transition id、自定义字段 id、下拉词表全部运行时从 JIRA 读取；**换项目、换实例不用改代码**；选项填错在请求发出前中止并列出可选项（不会盲发 400）
- **通用**：JIRA Server / Data Center 8.x 开箱即用；可选 Bearer token（Server PAT）与 Cloud「邮箱+API Token」认证
- **多实例/多账号**：`--profile 名字` 一键切换 JIRA 实例（凭据按 profile 分开存，互不干扰）
- **支持改别人的单**：`assign` 接管（`--to me`）/转派、`comment` 协作评论；权限不足时给出明确的下一步提示
- **写操作三保险**：`--dry-run` 预演（不提交，可看将发送的完整 payload 与字段清单）+ 选项词表预校验 + 执行后自动读回验证（状态/字段逐项核对）
- **零依赖**：Python 3 标准库（urllib），Windows / macOS / Linux 通用
- **安全**：凭据存本地文件，脚本永不打印密码；429/5xx 自动重试；内网自签证书自动降级

## 快速开始（3 步）

```
# 1. 拿工具（整个工具就是一个文件，可单独拷走）
git clone https://github.com/CapLee/jira-bugfix-workflow.git
# 或只拷贝 scripts/jira.py 到任何目录

# 2. 配凭据（一次性，详见 docs/01-配置指南.md）
#    Windows 默认读 %LOCALAPPDATA%\hermes\jira-api-creds.yaml
#    macOS/Linux 默认读 ~/hermes/jira-api-creds.yaml
#    也可 export JIRA_CREDS_PATH=/任意/路径/creds.yaml
#    多实例：凭据放 hermes/jira-profiles/<名字>.yaml，用 --profile <名字> 切换
mkdir -p ~/hermes
cat > ~/hermes/jira-api-creds.yaml <<'EOF'
base_url: https://ticket.你的公司.com
username: 你的JIRA登录名
password: 你的密码
EOF

# 3. 冒烟验证：先看身份，再拉你未解决的 bug
python3 scripts/jira.py whoami
python3 scripts/jira.py search --jql 'resolution = Unresolved AND assignee in (currentUser()) order by updated DESC'
```

## 常用命令速览

```
python3 jira.py whoami                                        # 当前登录身份/实例（冒烟验证首选）
python3 jira.py search --jql 'JQL' [--max 100] [--all] [--format table|json|md]
python3 jira.py search --url 'https://主机/issues/?jql=…'      # 直接贴过滤器链接（自动提取 JQL）
python3 jira.py issue KEY [--save x.md]                       # 详情+评论+附件
python3 jira.py attachments KEY [--save-dir 目录]              # 下载附件（截图等）
python3 jira.py transitions KEY                               # 看可用状态流转
python3 jira.py start KEY [--dry-run]                         # 「接受」（未开始→Working）
python3 jira.py resolve KEY --impact 影响范围 --cause 发生原因 \
    --solution 解决方法 --prevention 预防措施 \
    [--assign 测试员] [--field '标签=值']... [--comment '备注'] [--dry-run]
                                                              # 流转「解决」+填必填+转交
python3 jira.py assign KEY --to me|姓名 [--dry-run]            # 接管/转派经办人（改别人的单用）
python3 jira.py comment KEY --body '内容' [--dry-run]          # 加评论（协作/交接说明）
python3 jira.py projects [--query 关键词]                      # 探查实例有哪些项目（接入新环境用）
python3 jira.py fields [--query 关键词]                        # 探查字段 id/类型（配 --field 用）
```

所有写操作（start / resolve / assign / comment）都支持 `--dry-run` 预演，且执行后自动读回验证。

## 文档目录

| 文档 | 内容 |
|---|---|
| [docs/01-配置指南.md](docs/01-配置指南.md) | 环境要求、凭据配置（profile 多实例 / token 认证 / 常见坑）、冒烟验证、切换实例、安全说明 |
| [docs/02-使用方法.md](docs/02-使用方法.md) | 全部 12 个命令参考、6 步数据处理工作流、`--dry-run` 预演、自然语言驱动对照表、完整实测示例 |
| [docs/03-接入新项目与FAQ.md](docs/03-接入新项目与FAQ.md) | 工作原理（动态发现机制）、新 JIRA 环境接入检查清单、故障排查表、Cloud 适配说明 |
| [docs/04-端到端实战示例.md](docs/04-端到端实战示例.md) | **新同事上手首选**：安装→初始化→查 bug→改 bug→更新状态→批量→收尾，全流程照抄即可 |
| [docs/05-大白话速通版.md](docs/05-大白话速通版.md) | **给不看命令的人**：单据机器人比喻、三句话用法、对话演示、常见疑问大白话 |
| [docs/06-新同事实录.md](docs/06-新同事实录.md) | **角色扮演版**：假装自己是新同事，从装 Python 到交单全流程实录，每步有屏幕输出 |
| [docs/07-多实例与协作场景.md](docs/07-多实例与协作场景.md) | **换实例/换账号、改别人的 bug、团队协作**：profile 切换、接管别人名下的单、评论协作、转派交接全流程 |
| [SKILL.md](SKILL.md) | 给 AI 助手用的技能文件（可直接装进 Hermes / Claude Code 等当 skill） |

## 给 AI 的使用方式（重要）

1. 把 [`SKILL.md`](SKILL.md)（或本 README + `docs/02-使用方法.md`）作为上下文提供给 AI
2. AI 用「终端执行 `python3 jira.py <命令>`」完成拉单/读单/流转，用自身编码能力改代码
3. 状态写操作（start / resolve / assign / comment）**必须等用户明确指令**再执行；拿不准先 `--dry-run` 预演给用户过目
4. 代码修复按团队规范只留工作区改动，提交由开发者自己完成

用户侧一句话触发即可：

- 「拉一下我未解决的 bug」→ 出清单
- 「1、3、7 分析下根因」→ 只分析不改码
- 「这几张都改」→ 改码+自验证
- 「这张修好了，流转吧」→ resolve 填字段+转测试
- 「接一下张三的单 / 把这几张转给我」→ assign --to me 接管
- 「把配置换成 XX 环境」→ --profile 切换实例

## 典型输出

```
$ python3 jira.py search --jql 'project = X AND resolution = Unresolved ...'
共 21 张（已取 21）
key            | 类型     | 状态      | 优先级       | 经办人            | 更新         | 摘要
STRUCTURING-25 | ST-BUG | 未开始     | Medium-一般 | Ronnie Li(李一鸣) | 2026-09-08 | 【我的空间列表】置顶按钮常显…
```

```
$ python3 jira.py resolve BPM-1597 --impact "…" --cause 需求理解偏差 --dry-run
字段映射: 影响范围 -> customfield_13209
字段映射: 发生原因 -> customfield_15817
  可选值: 需求文档遗漏 / 需求理解偏差 / …（词表运行时自动发现）
[DRY-RUN] 将 POST /rest/api/2/issue/BPM-1597/transitions payload: {…}
[DRY-RUN] 流转屏幕字段清单（新实例探查用）:
  customfield_12249	解决方案	必填=否	类型=option
  …
（未发任何写请求；去掉 --dry-run 即执行）
```

```
$ python3 jira.py resolve X-3 --impact "…" --cause "需求理解偏差" --solution "调整UI/样式" --prevention "无需额外措施"
已流转 X-3: 解决 -> ST Check @ https://ticket.你的公司.com；读回: X-3 -> ST Check (resolution=-)
[校验] 已读回字段: 影响范围、发生原因、解决方法、预防措施
已转交经办人: 测试员甲 (name=xxx) ✓
```

## 适用与限制

- **适用**：JIRA Server / Data Center 8.x（HTTP Basic）。Bearer token（Server PAT）与 Cloud「邮箱+API Token」也可用（Cloud 部分用户字段差异见 docs/03 §4）
- **限制**：不支持需要浏览器验证码/双因子且无独立密码的实例（可考虑浏览器同源 fetch 方案）；工具不创建/删除 issue，只处理已存在的单
- 下拉词表、状态机名称因项目而异——工具全部运行时发现，**换项目、换实例不用改代码**（见 docs/03 接入清单、docs/07 场景指南）

## 开发者自测（改完 jira.py 跑一遍）

```bash
python3 scripts/selftest_offline.py    # 离线 mock：26 项断言，零真实请求，退出码 0 = 全过
```

## License

MIT
