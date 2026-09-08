# jira-bugfix-workflow

JIRA Bug 处理「纯数据流」命令行工具：**拉清单 → 读详情 → 改代码 → 更新状态**，全程走 REST API，零浏览器、零第三方依赖（Python 3 标准库单文件）。

解决的是「让 AI（或人）不靠模拟浏览器点击，直接通过数据流转处理 JIRA bug」的问题。工具 = **一个 Python 文件** + 一份凭据配置；docs/ 是完整说明书，任何 AI 助手（Hermes / Claude Code / WorkBuddy / Cursor…）读了文档后都能驱动它执行完整 bug 处理流程。

## 特性

- **纯 API 数据流**：JQL 拉 bug 清单、拉详情+评论、查流转、点「接受」、填自定义字段流转「解决」+ 转交测试，全部 REST 直连
- **动态发现，零硬编码**：transition id、自定义字段 id、下拉选项词表全部运行时从 JIRA 读取；选项值自动校验，填错在请求发出前中止并列出可选项（不会盲发 400）
- **字段类型自适应**：select 选项自动映射 option id、版本字段自动包数组、用户字段自动包 name、文本框直接传值
- **写操作有读回验证**：每次状态流转后自动读回确认（状态/解决结果）
- **零依赖**：Python 3 标准库（urllib），Windows / macOS / Linux 通用
- **安全**：凭据存本地文件，脚本永不打印密码；支持 `JIRA_CREDS_PATH` 环境变量

## 快速开始（3 步）

```bash
# 1. 拿工具（整个工具就是一个文件，可单独拷走）
git clone https://github.com/CapLee/jira-bugfix-workflow.git
# 或只拷贝 scripts/jira.py 到任何目录

# 2. 配凭据（一次性，详见 docs/01-配置指南.md）
#    Windows 默认读 %LOCALAPPDATA%\hermes\jira-api-creds.yaml
#    macOS/Linux 默认读 ~/hermes/jira-api-creds.yaml
#    也可 export JIRA_CREDS_PATH=/任意/路径/creds.yaml
mkdir -p ~/hermes
cat > ~/hermes/jira-api-creds.yaml <<'EOF'
base_url: https://ticket.你的公司.com
username: 你的JIRA登录名
password: 你的密码
EOF

# 3. 冒烟验证：拉你未解决的 bug
python3 scripts/jira.py search --jql 'resolution = Unresolved AND assignee in (currentUser()) order by updated DESC'
```

## 常用命令速览

```bash
python3 jira.py search --jql 'JQL' [--format table|json|md]   # 拉 bug 清单
python3 jira.py issue KEY [--save x.md]                        # 详情+全部评论
python3 jira.py transitions KEY                                # 看可用状态流转
python3 jira.py start KEY                                      # 「接受」（未开始→Working）
python3 jira.py resolve KEY --impact 影响范围 --cause 发生原因 \
    --solution 解决方法 --prevention 预防措施 [--assign 测试员] [--field '标签=值']...
                                                               # 流转「解决」+填必填+转交
```

## 文档目录

| 文档 | 内容 |
|---|---|
| [docs/01-配置指南.md](docs/01-配置指南.md) | 环境要求、凭据文件配置（含常见坑）、冒烟验证、切换 JIRA 实例、安全说明 |
| [docs/02-使用方法.md](docs/02-使用方法.md) | 全部命令参考、6 步数据处理工作流、自然语言驱动对照表、完整实测示例（含选项词表样例） |
| [docs/03-接入新项目与FAQ.md](docs/03-接入新项目与FAQ.md) | 工作原理（动态发现机制）、新 JIRA 项目接入检查清单、故障排查表 |

## 给 AI 的使用方式（重要）

1. 把本 README + `docs/02-使用方法.md` 作为上下文提供给 AI（或让 AI 先读这两个文件）
2. AI 用「终端执行 `python3 jira.py <命令>`」完成拉单/读单/流转，用自身编码能力改代码
3. 状态写操作（start / resolve）**必须等用户明确指令**再执行，AI 只提议不擅自流转
4. 代码修复按团队规范只留工作区改动，提交由开发者自己完成

用户侧一句话触发即可：
- 「拉一下我未解决的 bug」→ 出清单
- 「1、3、7 分析下根因」→ 只分析不改码
- 「这几张都改」→ 改码+自验证
- 「这张修好了，流转吧」→ resolve 填字段+转测试

## 典型输出

```
$ python3 jira.py search --jql 'project = X AND resolution = Unresolved ...'
共 21 张（已取 21）
key            | 类型     | 状态      | 优先级       | 更新         | 摘要
STRUCTURING-25 | ST-BUG | 未开始     | Medium-一般 | 2026-09-08 | 【我的空间列表】置顶按钮常显…
```

```
$ python3 jira.py resolve X-3 --impact "…" --cause "需求理解偏差" --solution "调整UI/样式" --prevention "无需额外措施"
字段映射: 影响范围 -> customfield_13209
字段映射: 发生原因 -> customfield_15817
  可选值: 需求文档遗漏 / 需求理解偏差 / …（词表运行时自动发现）
已流转 X-3: 解决 -> ST Check；读回: X-3 -> ST Check (resolution=-)
```

## 适用与限制

- **适用**：JIRA Server / Data Center 8.x（HTTP Basic 认证）。REST 匿名可达、登录账号可用即可
- **限制**：JIRA Cloud 用 API Token 认证，需小改认证段（思路见 docs/03）；不支持需要浏览器验证码/双因子才能登录的实例（此时可考虑同源 fetch 的浏览器方案）
- 下拉词表、状态机名称因项目而异——工具全部运行时发现，**换项目不用改代码**（见 docs/03 接入清单）

## License

MIT
