# 使用说明

所有命令从业务项目目录执行技能脚本实际路径。下文 `jira.py` 代表 `<技能目录>/scripts/jira.py`。

## 初始化与配置

首次使用或改密码：`python jira.py init --dialog`。本机弹窗一次收齐用户名和密码，工具先用它请求 JIRA 验证身份，成功才保存；取消、认证失败或账号不一致时原配置不变。密码不回显，不进聊天、命令参数、日志或发布包。已有配置直接沿用，日常不重复弹窗。明确指定的配置路径/profile 不存在则停止，不自动改用默认账号。要设置默认项目才运行 `use KEY`，不会为查项目而自动登记所有项目。

```bash
python jira.py init
python jira.py init --check
python jira.py whoami
python jira.py use
```

默认地址 `https://ticket.quectel.com`，显式地址和已有账号配置优先。首次用登录弹窗配置；验证通过才落盘，失败不覆盖旧值。公司 SSO 登录不能替代 JIRA 账号。不在聊天或命令参数传密码。

默认配置 `config/jira-api-creds.yaml`；多实例放 `config/profiles/<名字>.yaml` 并用 `--profile <名字>`。可用 `--config` 或 `JIRA_CREDS_PATH` 指向私有配置。首次才初始化，日常沿用当前目录和活动项目，不重复配置；默认只拉当前项目分配给我的未解决单。

无秘密的结构模板：

```yaml
base_url: https://ticket.quectel.com
username: <你的登录名>
password: <你在本机填写的密码>
projects: <项目KEY>
active_project: <项目KEY>
```

占位符必须替换。登录弹窗写出的密码会按需加引号，首尾空格、`#`、引号、反斜杠等多数字符可无损往返；写入前会读回校验，失败则原文件不动。已有密码重跑 `init --dialog` 可改密码；认证失败时不要反复重试，避免账号被锁。

默认严格 TLS；失败先核地址与公司 CA，通过系统证书库或 `SSL_CERT_FILE` 配置。仅经用户明确授权的临时排障可显式 `--insecure`，它会警告且不自动启用；本技能不得自行加这个参数。

## 项目范围

`use KEY` 改活动项目；不传查询条件默认只查当前项目。项目根 `.jira-project.yaml` 可声明 `project/default_jql/profile`，用 `pconfig --here --set-project KEY --default-jql 'project = {project} AND resolution = Unresolved AND assignee in (currentUser())'` 创建。

优先级：显式参数 > 当前目录配置 > 包内 `config/projects/<KEY>.yaml` > 全局查询模板 > 内置默认。未获跨项目授权不能使用 `--all-projects` 或修改项目规避范围检查。

## 常用命令

```bash
python jira.py search
python jira.py triage PROJECT-123 PROJECT-124 --format json
python jira.py issue PROJECT-123
python jira.py changelog PROJECT-123
python jira.py attachments PROJECT-123 --list
python jira.py transitions PROJECT-123
python jira.py resolve PROJECT-123 --dry-run
```

triage 返回评论/历史 `total/fetched/complete/warnings`；缺 total 保持未知，有明确结束标志才可证明读全。附件默认仅元信息；资料有缺口不能当无记录。

写操作均先核用户授权、当前身份与项目。`start` 接受、`assign --to` 指派、`comment --body-file` 评论、`attachments --upload` 上传、`create` 建单支持 `--dry-run`；具体参数用各命令 `--help`。评论可能触发通知，附件/流转并不都能撤回。

## 解决与交付证据

```bash
python jira.py resolve PROJECT-123 --repo <业务仓库> --fix-commit <实际修复SHA> --impact '本单范围' --cause '当前项目合法原因' --solution '当前项目合法方法' --prevention '当前项目合法选项' --assign reporter --dry-run
```

正式执行先核干净工作区、明确修复 SHA 同时在当前 HEAD 和 fresh fetch 到的远端历史中；默认上游，可同时传 `--remote` 与 `--branch`。多笔修复重复 `--fix-commit`。不自动提交推送，不替人证明代码确实修好。未开始单先按授权接受再解决；字段词表/流转 id 每次现场发现。

不带证据的 dry-run 可探查屏幕，交付显示未验证，不能当通过门禁。完成后核状态/字段/经办人；默认逐单报告人，授权其他人时才覆盖。

## 批量与续跑

```json
{
  "action": "resolve",
  "repo": "<业务仓库>",
  "issues": [{
    "key": "PROJECT-123",
    "fix_commits": ["<实际修复SHA>"],
    "impact": "本单影响范围",
    "cause": "当前项目合法选项",
    "solution": "当前项目合法选项",
    "prevention": "当前项目合法选项",
    "assign": "reporter"
  }]
}
```

```bash
python jira.py batch plan --file input.json
python jira.py batch run <返回的任务文件>
python jira.py batch run <返回的任务文件> --execute
python jira.py batch resume <同一任务文件> --execute
python jira.py batch status <同一任务文件>
```

plan、默认run/resume不写JIRA；只有明确授权才 `--execute`。start任务只需单号；resolve清单每单明确证据和口径。仓库相对路径相对清单目录，不自动接管别人单。未开始单接受后才读解决屏幕，预演不能伪称两跳已验证。

成功项续跑读回但不重写；失败保留步骤。超时/5xx/丢响应先核结果，无法确认则停止，不能改参数或删日志绕过。报告人/经办人变化或QA重开停止旧任务。评论按作者与完整正文去重；附件按名称/大小/内容SHA-256。默认记录 `data/runs`，`JIRA_RUNS_DIR` 可覆盖；含业务内容不外发。异常退出留锁时，先核旧进程结束，才能处理提示的锁文件。

## 适用范围

本版本适用于 JIRA Server/Data Center，Jira Cloud 不在支持范围。运行需要 Python 3.8+，交付检查需要 Git，安装目录需要可写。配置和运行记录仅保留在用户本机，不随技能包分享。
