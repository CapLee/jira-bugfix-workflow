---
name: jira-bugfix-workflow
description: Use when 用纯数据流（REST API）处理 JIRA bug——拉单/读单/改码/接受/解决，含改别人的单、多实例切换。不要浏览器点击时。
version: 2.0.0
---

# JIRA Bug 纯数据流处理（jira.py）

工具 = `scripts/jira.py` 单文件（Python 3 标准库，零依赖），全程 REST API，零浏览器。
安装：`git clone <本仓库>`；凭据配置与完整命令手册见 `docs/01`、`docs/02`，场景剧本见 `docs/07`。

## 何时用

- 用户要「拉 JIRA bug / 改 bug / 更新状态」，且环境能直连 JIRA REST（Server/DC 8.x Basic，或 Bearer/Cloud）
- 用户要求「不要浏览器 / 不要模拟点击 / 纯数据流」
- 用户要帮改**别人名下的单**、切换**多个 JIRA 实例/账号**（v2 能力）

## 一次性配置

- 凭据文件（flat yaml：base_url / username / password，可选 token/insecure/timeout），默认 `%LOCALAPPDATA%\hermes\jira-api-creds.yaml`（macOS/Linux `~/hermes/…`）
- 多实例：`hermes/jira-profiles/<名字>.yaml` + `--profile <名字>`（报错会列可用名字）
- 冒烟：`python3 scripts/jira.py whoami`（身份+实例一行看清）

## 命令速查（全部 `--help` 可用）

```
whoami                                   # 身份/实例（冒烟首选）
search --jql '…' [--max|--all] [--format table|json|md]   # 拉清单，自动分页
search --url '…/issues/?jql=…'           # 贴过滤器链接
issue KEY [--save f.md]                  # 详情+评论+附件
attachments KEY [--save-dir 目录]         # 下载附件（截图）
transitions KEY                          # 可用流转（空列表=不是你的单）
start KEY [--dry-run]                    # 接受（未开始→Working）
resolve KEY --impact … --cause … --solution … --prevention … \
  [--field '标签=值']… [--assign 人] [--comment 备注] [--dry-run]   # 解决+填字段+转交
assign KEY --to me|姓名 [--dry-run]       # 接管/转派经办人
comment KEY --body '…' [--dry-run]        # 评论（@人写 [~登录名]）
projects / fields [--query]              # 探查项目/字段（接入新环境）
```

## 标准流程（六步）

1. `search` 拉清单 → **编号呈现给用户**，等圈单（每轮 ≤5 张）
2. `issue KEY` 逐张读详情+评论（需要时 `attachments` 下截图）
3. 用户确认后改码 + 自验证（改动只留工作区，**不 git commit**；涉及表/数据交付 DDL/DML）
4. `start KEY` 接受（读回验证）
5. 用户验收后 `resolve …` 填四项必填 + 版本 + 转交（读回逐字段验证）
6. 汇报：单号+根因+改动文件+验证结果+状态

## 改别人的单（工作流常只允许经办人流转）

1. 探路：`transitions KEY` 或 `resolve KEY … --dry-run`——空列表/无可用流转 = 权限不够
2. 接管：`assign KEY --to me`（先经用户/原负责人同意——这是公开变更记录）
3. 照常 start → 修 → resolve；交回方式按约定（自动转报告人 / `--assign` / 转回原负责人）
4. 留痕：`comment` 一条「代为处理」；关键结论也写进评论

## 铁律（写操作三保险）

- start / resolve / assign / comment **永远等用户明确指令**；拿不准先 `--dry-run` 预演给用户过目（零写请求，打印完整 payload + 字段清单）
- 执行后**必须读回验证**（脚本自动做：状态 + 每个已填字段逐项核对）
- transition id / 字段 id / 下拉词表**永远运行时发现**，不硬编码；词表填错会在 POST 前中止并列出可选项
- 多实例用 `--profile`；写操作输出带 `@ 实例`，执行前扫一眼防串库

## 排错速查

| 现象 | 处理 |
|---|---|
| 401/403 | 凭据/SSO；视环境降级浏览器方案 |
| `当前账号对 X 无任何可用流转` | 不是你的单 → `assign X --to me` |
| 无「接受/解决」类流转或歧义 | `transitions KEY` 看候选 → `--transition-id` |
| 必填未填 | `--dry-run` 看屏幕字段清单 → `--field '标签=值'` |
| 贴 browse 链接报无 jql | 改用 `/issues/?jql=` 过滤器链接 |
| 429/5xx | 自动重试（≤2 次）；持续失败查网络 |
