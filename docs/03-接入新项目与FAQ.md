# 03 · 接入新项目与故障排查

## 1. 工作原理（为什么换项目不用改代码）

工具所有「项目相关」的信息都**在运行时从 JIRA 读取**：

| 需要知道的东西 | 来源 | 机制 |
|---|---|---|
| 可用状态流转 | `GET /rest/api/2/issue/{KEY}/transitions` | 按名称匹配「接受」「解决」（中文名优先，兼容英文/目标状态名）；歧义时要求 `--transition-id` |
| 解决屏幕字段（含必填自定义字段） | 同上 + `?expand=transitions.fields` | **只有这里能拿到流转屏幕字段**（`editmeta` 拿的是编辑页，不含） |
| 中文标签 → 字段 id | 字段名与「影响范围/发生原因/解决方法/预防措施」归一化后互相包含匹配 | 归一化忽略空格/括号/标点差异 |
| select 下拉可选值 | 同一展开里的 `allowedValues` | 注意选项键是 `value`（不是 `name`）；传值自动校验并映射为 option id |
| 字段值格式 | 字段 `schema.type` | option→`{"id":…}`；version 数组→`[{"name":…}]`；user→`{"name":…}`；文本框→原值 |

标准 resolution 字段如出现，自动从 allowedValues 挑「Fixed/已修复」类值；很多中文工作流用的是**自定义 select 字段「解决方案」**（已修复/不修复/延迟修复…），工具按名称匹配即可，传 `--field "解决方案=已修复"`。

## 2. 新 JIRA 项目接入检查清单

1. **确认实例类型**：JIRA Server / Data Center 8.x + 你的账号可用 Basic 认证（REST 匿名可达只是第一步，认证不行则本工具不可用）
2. **凭据**：按文档 01 配置并冒烟（`search --max 3`）
3. **跑一张有代表性的单**：`transitions KEY` 看状态机名称（接受/解决叫法是否不同，如 Accept/Resolve/处理中）；若自动按名匹配失败，输出会列出候选 → 用 `--transition-id` 兜底
4. **跑一张可解决的单**（用 `resolve` 只读部分观察即可——填一个肯定错的词表值，验证拦截信息里打印的项目词表）：
   ```bash
   python3 jira.py resolve KEY --cause 不存在的选项XYZ --solution x --prevention y
   # [错误] … 不在可选值内，可选项: <本项目词表>   ← 单子状态未被改动，放心试
   ```
5. **确认解决屏幕必填字段**：若 resolve 预检报「必填字段未填」→ 用 `--field "标签=值"` 补齐（多个就写多个 `--field`）。公司常见的额外必填：修复的版本、解决方案、BUG解决人
6. **转交策略**：本项目「解决」流转自带 post-function 会把经办人改成报告人（= 转测试），无需 `--assign`；如果你们项目没有，用 `--assign "测试员显示名"` 显式转交
7. 每个团队约定词表后，可以把词表抄进自己项目的备注文档，但**工具不依赖它**——随时以运行时输出为准

## 3. 故障排查速查

| 现象 | 处理 |
|---|---|
| `认证失败 HTTP 401/403` | 凭据错/含 `<>`/账号被 SSO 锁/该实例禁 Basic。逐项对照文档 01 |
| `凭据文件不存在` / `缺字段` | 路径或三键不全；`export JIRA_CREDS_PATH=…` 指定 |
| `HTTP 400 … “resolution”域中没有…` | 你手写 JQL 里用了本项目没有的 resolution 词（如 Fixed）→ 用 `resolution is not EMPTY` 或 `statusCategory = done` |
| 选项值不在词表 | resolve 自动列出可选项 → 换词重跑（**已在 POST 前中止，单子未动**） |
| 「接受/解决」类流转不唯一或找不到 | 输出会列出候选 → `--transition-id <id>` 强制指定（先 `transitions KEY` 查 id） |
| 必填字段未填，已中止 | 预检会列出缺失字段 id+名称 → `--field "标签=值"` 补齐 |
| 流转成功但读回不符 | `transitions KEY` 看当前状态可用流转再决定下一步 |
| `[警告] 证书校验失败，已退回不校验重试` | 内网自签证书，正常；在意可给系统装公司根证书后删除降级逻辑 |
| 中文乱码 | Windows 用 git-bash/PowerShell/VS Code 终端；脚本已内置 UTF-8 重连 |
| 请求超时/网络错误 | 检查内网/VPN；urllib 自动读取 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 |
| 密码含 `#` 被截断 | `#` 前带空格才会被当注释；避免 `abc #def` 写法 |

## 4. 扩展：Jira Cloud 适配思路

Jira Cloud 的 REST 基本同构，差异在认证与用户名体系：
1. 认证：Basic 账号密码 → 邮箱 + API Token（`base64(email:apitoken)` 作为 Basic 内容即可，urllib 段不用改）
2. 用户字段：`{"name": …}` → `{"accountId": …}`（用户搜索返回 accountId）
3. `transitions?expand=transitions.fields`、中文标签匹配、词表校验机制在 Cloud 同样成立
改造点集中在 `_request()` 的 Authorization 头和 `wrap_value()` 的 user 分支，约 10 行。
