#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jira.py — JIRA REST API CLI（纯数据流、零浏览器、零第三方依赖，单文件）。

适用: JIRA Server / Data Center 8.x（HTTP Basic）为主；可选 Bearer token（Server PAT、
      Cloud 用「邮箱+API Token」当 Basic——差异见 docs/03）。

认证: 凭据文件（flat yaml），默认 %LOCALAPPDATA%\\hermes\\jira-api-creds.yaml
      （macOS/Linux ~/hermes/...；环境变量 JIRA_CREDS_PATH 可覆盖）。
      多实例/多账号: --profile <名字> 读 profiles 目录 <名字>.yaml
      （默认 %LOCALAPPDATA%\\hermes\\jira-profiles\\，JIRA_PROFILES_DIR 可覆盖）。
      键: base_url / username / password，可选 token（Bearer）/ insecure / timeout /
          projects（登记的项目）/ active_project（当前活动项目）/ default_jql（可选）。

子命令: init / use / pconfig（初始化、活动项目、项目级配置）/ search / issue /
        transitions / editmeta / start / resolve / assign / comment / whoami /
        projects / fields / attachments
写操作（start/resolve/assign/comment）均支持 --dry-run 预演，执行后自动读回验证。
不带 --jql/--url/--project 的 search 默认聚焦「当前活动项目」（use 切换）；
未限定 project 的 --jql/--url 查询默认被拦截（确要跨项目需显式 --all-projects）。
拉单模板优先级：项目配置（hermes/jira-project-configs/<KEY>.yaml）> 全局配置（default_jql）> 内置默认。
纯数字单号（如 issue 25）自动补活动项目前缀。
每个子命令 --help 有详细用法。错误退出码 1，正常 0。
"""
import argparse
import base64
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse, parse_qs

PROG = os.path.basename(sys.argv[0])
VERSION = "2.2.0"

# 运行期配置（main 里填充）：base / user / pw / token / insecure / timeout /
#   projects / active / default_jql / path
C = {}


def die(msg, code=1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def _norm(s):
    """名称归一化：去空格/括号/星号/换行/分隔符差异（、，,;/与／视同不存在），用于中文标签与选项的宽容匹配。"""
    if not s:
        return ""
    return re.sub(r"[\s（）()*，,、;；/／\u3000]+", "", s).lower()


def _human(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _safe_name(name):
    """附件文件名安全化（去掉路径分隔符/控制字符，防目录穿越）。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip(" .")
    return name or "attachment"


# ---------------- 凭据 ----------------

def _hermes_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes")


def _default_creds_path():
    return os.path.join(_hermes_dir(), "jira-api-creds.yaml")


def _profiles_dir():
    return os.environ.get("JIRA_PROFILES_DIR") or os.path.join(_hermes_dir(), "jira-profiles")


def resolve_creds_path(args, for_init=False):
    """凭据文件定位：--config > --profile/JIRA_PROFILE > JIRA_CREDS_PATH > 默认路径。

    for_init=True 时 profile 文件不存在也不报错（init 可直接创建新 profile）。
    """
    if args.config and args.profile:
        die("--config 与 --profile 只能用一个（--config 指具体文件，--profile 指 profiles 目录里的名字）")
    if args.config:
        return args.config
    name = args.profile or os.environ.get("JIRA_PROFILE")
    if name:
        d = _profiles_dir()
        for ext in (".yaml", ".yml"):
            p = os.path.join(d, name + ext)
            if os.path.exists(p):
                return p
        if for_init:
            return os.path.join(d, name + ".yaml")
        avail = []
        if os.path.isdir(d):
            avail = sorted(f[:-5] if f.lower().endswith(".yaml") else f[:-4]
                           for f in os.listdir(d) if f.lower().endswith((".yaml", ".yml")))
        die(f"找不到 profile「{name}」：期望 {os.path.join(d, name + '.yaml')}\n"
            f"可用 profiles: {avail or '（目录不存在或为空；新建方法见 docs/01）'}")
    return os.environ.get("JIRA_CREDS_PATH") or _default_creds_path()


def _read_flat(path):
    """宽松读取 flat yaml（不做完整性校验），键统一小写。"""
    creds = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # 注释仅当 # 前有空格或是行首（密码里的 # 不带空格不会被截断）
            ci = line.find(" #")
            if ci >= 0:
                line = line[:ci].rstrip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            v = v.strip()
            # 剥掉成对包裹的引号/尖括号（模板占位符习惯 <> 常被误留）
            while (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")) or (v.startswith("<") and v.endswith(">")):
                v = v[1:-1].strip()
            creds[k.strip().lower()] = v
    return creds


def _update_cfg(path, updates):
    """把键值写回配置文件：已有键原地更新，新键追加；其余内容（含注释）原样保留。原子写入。"""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    found = set()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:", line)
        if m and m.group(2).lower() in updates:
            k = m.group(2).lower()
            lines[i] = f"{m.group(1)}{k}: {updates[k]}\n"
            found.add(k)
    for k, v in updates.items():
        if k not in found:
            lines.append(f"{k}: {v}\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, path)


def load_creds(path):
    if not os.path.exists(path):
        die(f"凭据文件不存在: {path}\n请先运行初始化向导: {PROG} init"
            f"（账号 → 验证 → 登记项目 → 设活动项目）；多实例可用 --profile（见 docs/01）。")
    creds = _read_flat(path)
    missing = []
    if not creds.get("base_url"):
        missing.append("base_url")
    if not (creds.get("token") or (creds.get("username") and creds.get("password"))):
        missing.append("username+password（或 token）")
    if missing:
        die(f"凭据文件 {path} 缺字段: {', '.join(missing)}；可运行 {PROG} init 补齐。")
    timeout = 60
    if creds.get("timeout"):
        try:
            timeout = int(float(creds["timeout"]))
        except ValueError:
            print(f"[警告] timeout 值无法解析: {creds['timeout']}，改用默认 60", file=sys.stderr)
    projects = [s.strip() for s in (creds.get("projects") or "").split(",") if s.strip()]
    return {
        "base": creds["base_url"].rstrip("/"),
        "user": creds.get("username", ""),
        "pw": creds.get("password", ""),
        "token": creds.get("token", ""),
        "insecure": str(creds.get("insecure", "")).strip().lower() in ("1", "true", "yes", "on"),
        "timeout": timeout,
        "projects": projects,
        "active": (creds.get("active_project") or "").strip(),
        "default_jql": (creds.get("default_jql") or "").strip(),
        "path": path,
    }


# ---------------- HTTP ----------------

_ctx_cache = {}


def _contexts():
    if "ok" not in _ctx_cache:
        _ctx_cache["ok"] = ssl.create_default_context()
        no = ssl.create_default_context()
        no.check_hostname = False
        no.verify_mode = ssl.CERT_NONE
        _ctx_cache["no"] = no
    return _ctx_cache["ok"], _ctx_cache["no"]


def _is_cert_err(e):
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(reason or e)


def _retry_wait(e):
    try:
        return max(1, min(int(e.headers.get("Retry-After", "")), 10))
    except (TypeError, ValueError, AttributeError):
        return 2


def _request(method, path, params=None, body=None, raw=False):
    """带认证的请求；429/5xx/网络抖动自动重试（≤2 次）；证书失败自动降级不校验重试。

    path 以 http(s):// 开头时按绝对 URL 用（附件下载等场景），否则拼接 base_url。
    """
    url = path if path.startswith(("http://", "https://")) else C["base"] + path
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    req = urllib.request.Request(url, method=method)
    if C.get("token"):
        req.add_header("Authorization", "Bearer " + C["token"])
    else:
        token = base64.b64encode(f"{C['user']}:{C['pw']}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()

    ctx_ok, ctx_no = _contexts()
    ctxs = [ctx_no] if C.get("insecure") else [ctx_ok, ctx_no]
    total = 3  # 1 次 + 最多 2 次重试（仅限 429/5xx/网络类）
    last_msg = None
    for attempt in range(total):
        wait = None
        for idx, ctx in enumerate(ctxs):
            try:
                with urllib.request.urlopen(req, data, timeout=C["timeout"], context=ctx) as r:
                    rawb = r.read()
                    if raw:
                        return r.status, rawb
                    parsed = None
                    if rawb:
                        try:
                            parsed = json.loads(rawb)
                        except ValueError:
                            parsed = None
                    return r.status, parsed
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    err = json.loads(e.read().decode("utf-8", "replace"))
                    msgs = err.get("errorMessages") or list(err.get("errors", {}).values())
                    detail = " | ".join(str(m) for m in msgs[:3])
                except Exception:
                    pass
                if e.code in (401, 403):
                    die(f"认证失败 HTTP {e.code}: {detail or '检查凭据文件或账号被 SSO 锁'}"
                        f"（凭据: {os.path.basename(C['path'])}）")
                if e.code in (429, 500, 502, 503, 504):
                    last_msg = f"HTTP {e.code} {method} {path}: {detail or e.reason}"
                    wait = _retry_wait(e)
                    break  # → 重试
                die(f"HTTP {e.code} {method} {path}: {detail or e.reason}")
            except (ssl.SSLError, urllib.error.URLError, OSError) as e:
                if _is_cert_err(e) and idx + 1 < len(ctxs):
                    print("[警告] 证书校验失败，已退回不校验重试（内网自签证书常见）", file=sys.stderr)
                    continue  # → 换不校验 ctx 再试
                last_msg = f"网络错误: {e}"
                wait = 1
                break
        if wait is None:
            die(f"请求失败: {last_msg or '未知错误'}")
        if attempt < total - 1:
            n = min(wait, 10)
            print(f"[重试] {last_msg}，{n}s 后重试（{attempt + 2}/{total}）…", file=sys.stderr)
            time.sleep(n)
            continue
        break
    die(f"请求失败（已重试）: {last_msg}")


def _status_line(key):
    _, data = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": "status,resolution"})
    f = data["fields"]
    res = (f.get("resolution") or {}).get("name", "-")
    return f"{key} -> {f['status']['name']} (resolution={res})"


def _fmt_fields_csv(fields):
    if isinstance(fields, (list, tuple)):
        return ",".join(fields)
    return fields


# ---------------- 项目上下文（活动项目） ----------------

def _key_arg(raw):
    """单号参数：纯数字时自动补当前活动项目前缀（如 25 → STRUCTURING-25）。"""
    k = str(raw or "").strip()
    if re.fullmatch(r"\d+", k):
        act = (C.get("active") or "").strip()
        if act:
            return f"{act}-{k}"
        die(f"「{k}」是纯数字：先设置活动项目（{PROG} use <项目>），或给完整单号（如 STRUCTURING-{k}）")
    return k


def _warn_project(key):
    """操作的单不属于当前活动项目时给出提醒（不拦截）。"""
    act = (C.get("active") or "").strip()
    proj = key.split("-", 1)[0] if "-" in key else ""
    if act and proj and proj.upper() != act.upper():
        print(f"[提示] {key} 属于项目 {proj}，当前活动项目是 {act}"
              f"（如需切换: {PROG} use {proj}）", file=sys.stderr)


def _project_cfg_dir():
    return os.environ.get("JIRA_PROJECT_CONFIGS_DIR") or os.path.join(_hermes_dir(), "jira-project-configs")


def _project_cfg_path(project):
    """在项目配置目录里按 <KEY>.yaml 查找（不区分大小写；目录不存在返回 None）。"""
    d = _project_cfg_dir()
    if not os.path.isdir(d) or not project:
        return None
    want = project.upper()
    for f in os.listdir(d):
        base, ext = os.path.splitext(f)
        if base.upper() == want and ext.lower() in (".yaml", ".yml"):
            return os.path.join(d, f)
    return None


def _load_project_cfg(project):
    """读取项目级配置（返回 dict, 路径）。文件不存在/读取失败按空处理。"""
    p = _project_cfg_path(project)
    if not p:
        return {}, None
    try:
        return _read_flat(p), p
    except OSError as e:
        print(f"[警告] 项目配置读取失败: {p}（{e}）", file=sys.stderr)
        return {}, p


# ---------------- search ----------------

DEFAULT_JQL = "project = {project} AND resolution = Unresolved AND assignee in (currentUser()) order by updated DESC"


def _jql_from_url(u):
    q = urlparse(u).query
    if not q and "?" in u:
        q = u.split("?", 1)[1]
    qs = parse_qs(q)
    if not qs.get("jql"):
        die("URL 里没有 jql 参数。请用过滤器页地址（含 /issues/?jql=… 的链接）；"
            "browse 单页链接不含 JQL，不能直接当清单用。")
    return qs["jql"][0]


def _build_default_jql(project):
    """生成默认 JQL，返回 (jql, 模板来源)。模板优先级：项目配置 > 全局配置(default_jql) > 内置默认。"""
    pcfg, ppath = ({}, None)
    if project not in ("ALL", "*", "全部"):
        pcfg, ppath = _load_project_cfg(project)
    if pcfg.get("default_jql"):
        tpl, src = pcfg["default_jql"], f"项目配置 {os.path.basename(ppath)}"
    elif C.get("default_jql"):
        tpl, src = C["default_jql"], "全局配置"
    else:
        tpl, src = DEFAULT_JQL, "内置默认"
    if project in ("ALL", "*", "全部"):
        if "{project}" not in tpl:
            return tpl, src
        s = re.sub(r"project\s*=\s*\{project\}\s*AND\s+", "", tpl, count=1, flags=re.I)
        if s == tpl:
            s = re.sub(r"project\s*=\s*\{project\}", "", tpl, count=1, flags=re.I)
        s = re.sub(r"^\s*AND\s+", "", s.strip(), flags=re.I).strip()
        if "{project}" in s:
            die("拉单模板里的 project 条件无法移除：改成「project = {project} AND …」形式，或显式 --jql")
        return s, src
    if "{project}" not in tpl:
        die(f"拉单模板（{src}）缺 {{project}} 占位符，无法套用当前项目；请显式 --jql 或修改配置（docs/01）")
    return tpl.replace("{project}", project), src


def _fetch_issues(jql, fields, max_n, all_flag):
    limit = 10 ** 6 if all_flag else max(1, max_n)
    out, total, start = [], 0, 0
    while True:
        page = min(100, limit - len(out))
        if page <= 0:
            break
        _, d = _request("GET", "/rest/api/2/search", params={
            "jql": jql, "maxResults": page, "startAt": start, "fields": fields,
        })
        total = d.get("total", len(out)) or 0
        issues = d.get("issues") or []
        out.extend(issues)
        start += len(issues)
        if not issues or start >= total:
            break
    return out, max(total, len(out))


def cmd_search(args):
    explicit = bool(args.url or args.jql)
    if args.url:
        jql = _jql_from_url(args.url)
    elif args.jql:
        jql = args.jql
    else:
        proj = (args.project or C.get("active") or "").strip()
        if proj and proj.upper() not in ("ALL", "*", "全部"):
            proj = proj.upper()
        if not proj:
            die(f"未给查询条件且未设置活动项目：先用 {PROG} use <项目> 设置（或 --jql/--url/--project 显式指定；"
                f"--project ALL=不限项目）")
        jql, tpl_src = _build_default_jql(proj)
        label = "全部" if proj in ("ALL", "*", "全部") else proj
        src = "指定项目" if args.project else "活动项目"
        note = f"[项目] {src}: {label}（模板: {tpl_src}）；JQL: {jql}"
        print(note, file=(sys.stderr if args.format == "json" else sys.stdout))
    if explicit:
        # 防手滑/防 AI 跑偏：未限定项目的查询默认拦截；指向别的项目时给提示
        act = (C.get("active") or "").strip()
        if act and act.upper() not in ("ALL", "*", "全部") and not args.all_projects:
            if not re.search(r"project\s*(?:=|!=|\bin\b)", jql, flags=re.I):
                die(f"该查询未限定项目，会跨【全部项目】拉单。当前活动项目是 {act}：\n"
                    f"  · 只查当前项目：直接运行不带条件的 search，或把 project = {act} 写进 JQL\n"
                    f"  · 确实要跨项目搜索：显式加 --all-projects")
            if act.upper() not in jql.upper():
                print(f"[提示] 查询未指向当前活动项目 {act}——若只是想查其它项目，先 {PROG} use <项目> 切换；"
                      f"确认为跨项目搜索请加 --all-projects（可消除本提示）", file=sys.stderr)
    issues, total = _fetch_issues(jql, _fmt_fields_csv(args.fields), args.max, args.all)
    rows = []
    for it in issues:
        f = it.get("fields") or {}
        rows.append({
            "key": it.get("key", "?"),
            "类型": (f.get("issuetype") or {}).get("name", "-"),
            "状态": (f.get("status") or {}).get("name", "-"),
            "优先级": (f.get("priority") or {}).get("name", "-"),
            "经办人": (f.get("assignee") or {}).get("displayName", "未指派"),
            "更新": (f.get("updated") or "")[:10],
            "摘要": f.get("summary", ""),
        })
    if args.format == "json":
        print(json.dumps({"total": total, "count": len(rows), "issues": issues}, ensure_ascii=False, indent=1))
        return
    extra = f"，还有 {total - len(rows)} 张未取（增大 --max 或加 --all）" if total > len(rows) else ""
    print(f"共 {total} 张（已取 {len(rows)}{extra}）")
    if not rows:
        print("（无结果）")
        return
    cols = ["key", "类型", "状态", "优先级", "经办人", "更新", "摘要"]
    caps = {"经办人": 18, "摘要": 60}
    widths = {}
    for c in cols:
        longest = len(c)
        for r in rows:
            longest = max(longest, len(str(r[c])))
        widths[c] = min(longest, caps.get(c, longest))
    if args.format == "md":
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in rows:
            cells = []
            for c in cols:
                s = str(r[c])
                if c in caps and len(s) > caps[c]:
                    s = s[:caps[c] - 1] + "…"
                cells.append(s)
            print("| " + " | ".join(cells) + " |")
        return
    hdr = " | ".join(c.ljust(widths[c]) for c in cols)
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        cells = []
        for c in cols:
            s = str(r[c])
            if c in caps and len(s) > caps[c]:
                s = s[:caps[c] - 1] + "…"
            cells.append(s.ljust(widths[c]))
        print(" | ".join(cells))


# ---------------- issue ----------------

def cmd_issue(args):
    key = _key_arg(args.key)
    _warn_project(key)
    fields = ("summary,description,status,priority,issuetype,assignee,reporter,"
              "created,updated,comment,resolution,attachment")
    _, d = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": fields})
    if args.format == "json":
        text = json.dumps(d, ensure_ascii=False, indent=1)
        if args.save:
            with open(args.save, "w", encoding="utf-8") as fp:
                fp.write(text)
            print(f"已保存: {os.path.abspath(args.save)}")
        else:
            print(text)
        return
    f = d["fields"]
    md = [
        f"# {d['key']} {f.get('summary','')}",
        "",
        f"- 状态: {f['status']['name']} | 优先级: {(f.get('priority') or {}).get('name','-')} | 类型: {(f.get('issuetype') or {}).get('name','-')}",
        f"- 报告人: {(f.get('reporter') or {}).get('displayName','-')} | 经办人: {(f.get('assignee') or {}).get('displayName','未指派')}",
        f"- 创建: {f.get('created','')} | 更新: {f.get('updated','')} | 解决: {(f.get('resolution') or {}).get('name','-')}",
        "",
        "## 描述",
        "",
        f.get("description", "") or "（空）",
        "",
    ]
    atts = f.get("attachment") or []
    if atts:
        md += ["## 附件", ""]
        for a in atts:
            md.append(f"- {a.get('filename')} ({_human(a.get('size', 0))}) {a.get('content','')}")
        md += ["", f"> 下载全部附件: {PROG} attachments {d['key']} --save-dir <目录>", ""]
    md += ["## 评论", ""]
    for c in (f.get("comment") or {}).get("comments", []):
        md += [f"### {c['author']['displayName']} @ {c.get('created','')}", "", c.get("body", ""), ""]
    text = "\n".join(md)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fp:
            fp.write(text)
        print(f"已保存: {os.path.abspath(args.save)}")
    else:
        print(text)


# ---------------- transitions / start ----------------

def cmd_transitions(args):
    key = _key_arg(args.key)
    _warn_project(key)
    _, d = _request("GET", f"/rest/api/2/issue/{key}/transitions", params={"fields": "status"})
    for t in d.get("transitions", []):
        print(f"{t['id']}\t{t['name']}\t-> {t['to']['name']} (category={t['to'].get('statusCategory',{}).get('key','?')})")
    if not d.get("transitions"):
        print("（无可用流转）")


def _pick_transition(transitions, name_hint=None, to_category=None):
    if name_hint:
        hits = [t for t in transitions if name_hint in _norm(t["name"]) or name_hint in _norm(t["to"]["name"])]
        if hits:
            return hits
    if to_category:
        hits = [t for t in transitions if t["to"].get("statusCategory", {}).get("key") == to_category]
        if hits:
            return hits
    return []


def cmd_start(args):
    key = _key_arg(args.key)
    _warn_project(key)
    _, d = _request("GET", f"/rest/api/2/issue/{key}/transitions")
    ts = d.get("transitions", [])
    if not ts:
        die(f"当前账号对 {key} 无任何可用流转——常见原因：单子经办人不是你（工作流限制只有经办人能流转）或已到终态。"
            f"如要接手处理，可先执行: {PROG} assign {key} --to me")
    _, sd = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": "status"})
    st = sd["fields"]["status"]
    cur_name = st.get("name", "?")
    cur_cat = (st.get("statusCategory") or {}).get("key", "")
    if args.transition_id:
        tid = next((t for t in ts if t["id"] == str(args.transition_id)), None)
        if not tid:
            die(f"transition id {args.transition_id} 不在可用列表: {[t['id'] for t in ts]}")
        pick = [tid]
    else:
        pick = _pick_transition(ts, name_hint="接受")
        if not pick and cur_cat not in ("indeterminate", "done"):
            working_like = [t for t in ts
                            if any(k in _norm(t["to"].get("name", ""))
                                   for k in ("working", "处理", "进行", "inprogress"))]
            if len(working_like) == 1:
                pick = working_like
                print(f"[匹配] 按目标状态「{pick[0]['to']['name']}」选取流转（名称未含「接受」）", file=sys.stderr)
        if not pick:
            if cur_cat in ("indeterminate", "done"):
                die(f"当前状态「{cur_name}」已不是「未开始」，无需「接受」；可用流转: "
                    f"{[(t['id'], t['name'], t['to']['name']) for t in ts]}（确要流转用 --transition-id 指定）")
            die(f"找不到「接受」类流转（未开始→处理中），可用: "
                f"{[(t['id'], t['name'], t['to']['name']) for t in ts]}；可 --transition-id 指定")
        if len(pick) > 1:
            die(f"「接受」类流转不唯一，请 --transition-id 指定: {[(t['id'], t['name']) for t in pick]}")
    tid = pick[0]
    payload = {"transition": {"id": tid["id"]}}
    if args.dry_run:
        print(f"[DRY-RUN] 当前: {key} -> {cur_name}")
        print(f"[DRY-RUN] 将 POST /rest/api/2/issue/{key}/transitions body: "
              f"{json.dumps(payload, ensure_ascii=False)}（{tid['name']} -> {tid['to']['name']}）")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return
    _request("POST", f"/rest/api/2/issue/{key}/transitions", body=payload)
    print(f"已流转: {tid['name']} -> {tid['to']['name']} @ {C['base']}；读回: {_status_line(key)}")


# ---------------- editmeta ----------------

def cmd_editmeta(args):
    key = _key_arg(args.key)
    _warn_project(key)
    _, d = _request("GET", f"/rest/api/2/issue/{key}/editmeta")
    fields = d.get("fields", {})
    if not fields:
        print("（无任何可编辑字段）")
        return
    for fid, meta in fields.items():
        print(f"{fid}\t{meta.get('name','?')}\t必填={meta.get('required')}\t类型={meta.get('schema',{}).get('type','?')}")


# ---------------- 人员 / 协作 ----------------

def _resolve_user(key, query):
    """解析目标用户（assign / resolve --assign 共用）。

    注意：部分 JIRA 实例会忽略 search 的 query 参数、直接返回全量 assignable 列表
    （本实例实测如此），且默认 20 条会截断名单——因此拉全量后在客户端按
    显示名/登录名二次过滤，不能信任服务端检索与排序。
    匹配规则：精确（显示名或登录名完全一致）> 归一化子串唯一命中；多候选列出并中止。
    """
    _, users = _request("GET", "/rest/api/2/user/assignable/search",
                        params={"issueKey": key, "query": query, "maxResults": 200})
    q_raw = (query or "").strip()
    q_norm, q_low = _norm(q_raw), q_raw.lower()

    def _exact(u):
        return (u.get("displayName") or "").strip() == q_raw \
            or (u.get("name") or "").strip().lower() == q_low

    def _partial(u):
        dn = _norm(u.get("displayName") or "")
        nm = (u.get("name") or "").lower()
        return (bool(q_norm) and q_norm in dn) or (bool(q_low) and q_low in nm)

    picked = [u for u in users if _exact(u)] or [u for u in users if _partial(u)]
    if not picked:
        die(f"未找到可指派人匹配「{q_raw}」（该人不属于本项目 assignable 用户？换登录名/显示名再试）")
    seen, cand = set(), []  # 按登录名去重，保持服务端顺序
    for u in picked:
        k = u.get("name") or u.get("displayName")
        if k not in seen:
            seen.add(k)
            cand.append(u)
    if len(cand) > 1:
        die(f"「{q_raw}」匹配到多个可指派人，请用完整显示名或登录名精确指定: "
            + "；".join(f"{u.get('displayName')} (name={u.get('name')})" for u in cand))
    return cand[0]


def cmd_assign(args):
    key = _key_arg(args.key)
    _warn_project(key)
    q = (args.to or "").strip()
    if not q:
        die("--to 不能为空（人员显示名/登录名，或 me 表示自己）")
    if q.lower() in ("me", "self", "我", "自己"):
        _, me = _request("GET", "/rest/api/2/myself")
        target = {"name": me.get("name"), "displayName": me.get("displayName")}
        if not target["name"]:
            die("无法获取当前用户名（/myself 未返回 name）")
    else:
        target = _resolve_user(key, q)
    if args.dry_run:
        preview = json.dumps({"name": target["name"]}, ensure_ascii=False)
        print(f"[DRY-RUN] 将 PUT /rest/api/2/issue/{key}/assignee body: {preview}"
              f"（→ {target.get('displayName')}）")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return
    _request("PUT", f"/rest/api/2/issue/{key}/assignee", body={"name": target["name"]})
    _, a = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": "assignee"})
    cur = (a["fields"].get("assignee") or {})
    mark = "✓" if cur.get("name") == target["name"] else "⚠ 读回与目标不一致，请核对"
    print(f"已指派 {key} → {target.get('displayName')} (name={target['name']}) @ {C['base']}；"
          f"读回: {cur.get('displayName') or '未指派'} {mark}")


def cmd_comment(args):
    key = _key_arg(args.key)
    _warn_project(key)
    if args.body_file:
        if not os.path.exists(args.body_file):
            die(f"文件不存在: {args.body_file}")
        with open(args.body_file, encoding="utf-8") as fp:
            body = fp.read()
    else:
        body = args.body
    if not body or not body.strip():
        die("评论内容为空")
    payload = {"body": body}
    if args.dry_run:
        print(f"[DRY-RUN] 将 POST /rest/api/2/issue/{key}/comment body: "
              f"{json.dumps(payload, ensure_ascii=False)}")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return
    _, d = _request("POST", f"/rest/api/2/issue/{key}/comment", body=payload)
    cid = d.get("id")
    _, chk = _request("GET", f"/rest/api/2/issue/{key}/comment/{cid}")
    ok = str(chk.get("id")) == str(cid)
    print(f"已评论 {key} (comment id={cid}) @ {C['base']}；"
          f"读回: id={chk.get('id')} by {(chk.get('author') or {}).get('displayName','-')} {'✓' if ok else '⚠'}")


def cmd_whoami(args):
    _, d = _request("GET", "/rest/api/2/myself")
    auth = "token" if C.get("token") else "basic"
    print(f"身份: {d.get('displayName')} (name={d.get('name')}, email={d.get('emailAddress', '-')})")
    print(f"实例: {C['base']}（凭据: {C['path']}，认证: {auth}）")
    act = (C.get("active") or "").strip()
    n = len(C.get("projects") or [])
    if act:
        print(f"当前活动项目: {act}（已登记 {n} 个项目；{PROG} use 切换）")
    else:
        print(f"当前活动项目: （未设置）→ {PROG} use <项目>（已登记 {n} 个）")


# ---------------- 初始化 / 项目切换 ----------------

def _prompt(label):
    while True:
        v = input(f"{label}: ").strip()
        if v:
            return v
        print("（不能为空，请重试；Ctrl+C 可中止）")


def _init_check(path):
    global C
    print(f"[检查] 配置文件: {path}")
    if not os.path.exists(path):
        print("  状态: 未初始化 ✗")
        print(f"  下一步: python {PROG} init   （交互向导：账号 → 验证 → 登记项目 → 设活动项目）")
        sys.exit(1)
    creds = _read_flat(path)
    missing = []
    if not creds.get("base_url"):
        missing.append("base_url")
    if not (creds.get("token") or (creds.get("username") and creds.get("password"))):
        missing.append("username+password（或 token）")
    if missing:
        print(f"  缺字段: {', '.join(missing)}")
        print(f"  状态: 未初始化(不完整) ✗ → python {PROG} init")
        sys.exit(1)
    print(f"  实例: {creds.get('base_url')}")
    print(f"  账号: {creds.get('username') or '(token 认证)'}")
    C = load_creds(path)
    try:
        _, me = _request("GET", "/rest/api/2/myself")
        print(f"  认证: OK（{me.get('displayName')}）")
    except SystemExit:
        print("  认证: 失败 ✗（检查账号密码/SSO；或重跑 init）")
        sys.exit(1)
    projects = [s.strip() for s in (creds.get("projects") or "").split(",") if s.strip()]
    active = (creds.get("active_project") or "").strip()
    print(f"  已登记项目({len(projects)}): {', '.join(projects) if projects else '（无）→ 运行 init 登记'}")
    print(f"  当前活动项目: {active if active else '（未设置）→ ' + PROG + ' use <项目>'}")
    pd = _project_cfg_dir()
    pf = sorted(f for f in os.listdir(pd) if f.lower().endswith((".yaml", ".yml"))) if os.path.isdir(pd) else []
    if pf:
        print(f"  项目级拉单模板: {', '.join(pf)}（目录: {pd}）")
    print("  状态: 已初始化 ✓")


def _init_projects(args, path):
    _, d = _request("GET", "/rest/api/2/project")
    allp = [(p.get("key", ""), p.get("name", "")) for p in d or []]
    cur = [s.strip() for s in (_read_flat(path).get("projects") or "").split(",") if s.strip()]
    picked = None
    if args.projects is not None:
        if args.projects.strip().lower() in ("all", "全部", "*"):
            picked = [k for k, n in allp]
        else:
            picked = []
            for w in [x.strip() for x in args.projects.split(",") if x.strip()]:
                hits = [k for k, n in allp if k.upper() == w.upper()]
                if not hits:
                    near = [k for k, n in allp if w.upper() in k.upper()][:8]
                    die(f"项目「{w}」在实例中不存在（相近: {near or '（无）'}）")
                picked.append(hits[0])
    elif sys.stdin.isatty():
        print(f"[项目] 实例共 {len(allp)} 个可见项目:")
        for i, (k, n) in enumerate(allp[:300], 1):
            mark = " *" if k in cur else "  "
            print(f"{mark} {i:>3}  {k:<18} {n}")
        if len(allp) > 300:
            print("    …（仅显示前 300，更多用 projects --query 查）")
        print("    （* = 已登记；回车 = 保留现状）")
        raw = input("选择要登记的项目（序号或KEY，逗号分隔）: ").strip()
        if raw:
            picked = []
            for tok in re.split(r"[,\s，、]+", raw):
                if not tok:
                    continue
                if tok.isdigit():
                    i = int(tok) - 1
                    if not (0 <= i < min(len(allp), 300)):
                        die(f"序号 {tok} 超出范围（1-{min(len(allp), 300)}）")
                    picked.append(allp[i][0])
                else:
                    hits = [k for k, n in allp if k.upper() == tok.upper()]
                    if not hits:
                        die(f"项目「{tok}」在实例中不存在")
                    picked.append(hits[0])
    elif not cur:
        print("[提示] 非交互模式未登记项目：可用 --projects 'A,B' 登记，或 use <KEY> 自动登记")
    if picked is not None:
        seen = set()
        keys = [p for p in picked if not (p in seen or seen.add(p))]
        _update_cfg(path, {"projects": ",".join(keys)})
        print(f"[保存] 已登记项目({len(keys)}): {', '.join(keys) if keys else '（空）'}")
        final_list = keys
    else:
        final_list = cur
    active = (_read_flat(path).get("active_project") or "").strip()
    use_key = (args.use or "").strip()
    if use_key:
        hits = [p for p in final_list if p.upper() == use_key.upper()] or \
               [k for k, n in allp if k.upper() == use_key.upper()]
        if not hits:
            die(f"--use 指定的项目「{use_key}」不存在或未登记")
        target = hits[0]
        ups = {"active_project": target}
        if target not in final_list:
            ups["projects"] = ",".join(final_list + [target])
        _update_cfg(path, ups)
        print(f"[保存] 当前活动项目: {target}")
    elif not active and final_list:
        target = final_list[0]
        _update_cfg(path, {"active_project": target})
        print(f"[保存] 当前活动项目: {target}（默认取第一个；{PROG} use 切换）")


def cmd_init(args):
    global C
    path = resolve_creds_path(args, for_init=True)
    if args.check:
        _init_check(path)
        return
    interactive = sys.stdin.isatty()
    creds = _read_flat(path) if os.path.exists(path) else {}
    base = (args.base_url or creds.get("base_url") or "").strip()
    user = (args.username or creds.get("username") or "").strip()
    pw = args.password or creds.get("password") or ""
    token = (args.token or creds.get("token") or "").strip()
    print(f"[初始化] 配置文件: {path}")
    if creds:
        print(f"[初始化] 检测到现有配置（{creds.get('base_url', '?')}），将补齐/更新缺失项")
    if not base:
        if not interactive:
            die("非交互模式须提供 --base-url（或已有 base_url）；交互模式直接运行 init 按提示输入")
        base = _prompt("JIRA 地址（如 https://ticket.你的公司.com）")
    if not token:
        if not user:
            if not interactive:
                die("非交互模式须提供 --username（或已有）")
            user = _prompt("登录名")
        if not pw:
            if not interactive:
                die("非交互模式须提供 --password（或 --token）")
            pw = getpass.getpass("密码（输入不回显）: ").strip()
            if not pw:
                die("密码为空，已中止")
    updates = {"base_url": base.rstrip("/")}
    if user:
        updates["username"] = user
    if pw:
        updates["password"] = pw
    if token:
        updates["token"] = token
    _update_cfg(path, updates)
    C = load_creds(path)
    if getattr(args, "timeout", None):
        C["timeout"] = args.timeout
    if getattr(args, "insecure", False):
        C["insecure"] = True
    print(f"[保存] 配置已写入: {path}")
    _, me = _request("GET", "/rest/api/2/myself")
    print(f"[验证] 身份: {me.get('displayName')} (name={me.get('name')}) @ {C['base']}")
    _init_projects(args, path)
    cfg = _read_flat(path)
    projects = [s.strip() for s in (cfg.get("projects") or "").split(",") if s.strip()]
    active = (cfg.get("active_project") or "").strip()
    print(f"[完成] 已登记项目({len(projects)}): {', '.join(projects) if projects else '（无）'}")
    if active:
        print(f"[完成] 当前活动项目: {active}（不切换就一直用它；{PROG} use 切换）")
        print(f"[完成] 试试: python {PROG} search   ← 当前项目里我的未解决 bug")
    else:
        print(f"[完成] 当前活动项目: （未设置）→ {PROG} use <项目>")
    print(f"[完成] 体检: python {PROG} init --check")


def _resolve_project_arg(arg, projects):
    a = arg.strip()
    if a.isdigit():
        i = int(a) - 1
        if 0 <= i < len(projects):
            return projects[i]
        die(f"序号 {a} 超出范围（已登记 {len(projects)} 个；{PROG} use 查看列表）")
    hits = [p for p in projects if p.upper() == a.upper()]
    if hits:
        return hits[0]
    hits = [p for p in projects if a.upper() in p.upper()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        die(f"「{arg}」匹配多个已登记项目: {hits}；请给完整 KEY")
    _, d = _request("GET", "/rest/api/2/project")
    allp = [(p.get("key", ""), p.get("name", "")) for p in d or []]
    exact = [k for k, n in allp if k.upper() == a.upper()]
    if exact:
        return exact[0]
    near = [k for k, n in allp if a.upper() in k.upper() or a.upper() in (n or "").upper()][:8]
    die(f"项目「{arg}」不在已登记列表、实例里也没有。相近的: {near or '（无）'}\n"
        f"（全量查看: {PROG} projects [--query 关键词]）")


def cmd_use(args):
    act = (C.get("active") or "").strip()
    projects = C.get("projects") or []
    tgt = (getattr(args, "target", None) or "").strip()
    if not tgt:
        print(f"当前活动项目: {act or '（未设置）'}")
        if not projects:
            print(f"已登记项目: （无）—— 用 {PROG} init 登记，或直接 {PROG} use <项目KEY>（会自动校验并登记）")
            return
        print("已登记项目（use 序号或KEY 切换）:")
        for i, p in enumerate(projects, 1):
            mark = "*" if act and p.upper() == act.upper() else " "
            print(f"  {mark} {i:>2}  {p}")
        return
    key = _resolve_project_arg(tgt, projects)
    is_new = key not in projects
    ups = {"active_project": key}
    if is_new:
        ups["projects"] = ",".join(projects + [key])
    _update_cfg(C["path"], ups)
    C["active"] = key
    if is_new:
        C["projects"] = projects + [key]
    tail = f"（新登记，共 {len(C['projects'])} 个）" if is_new else ""
    print(f"已切换当前活动项目: {key} {tail}".rstrip())
    print("之后不带 --jql/--url/--project 的 search 默认只查它；其他项目的单会给出提示（--project KEY 可临时换）。")


def cmd_pconfig(args):
    proj = (args.project or C.get("active") or "").strip().upper()
    d = _project_cfg_dir()
    if args.set_default_jql is not None or args.clear:
        if not proj:
            die(f"未指定项目：pconfig <项目KEY>（或先 {PROG} use 设置活动项目）")
        p = _project_cfg_path(proj) or os.path.join(d, proj + ".yaml")
        if args.clear:
            if os.path.exists(p):
                os.remove(p)
                print(f"已删除项目配置: {p}")
            else:
                print(f"（无项目配置，无需删除）: {p}")
            return
        if "{project}" not in args.set_default_jql:
            die("模板里必须包含 {project} 占位符（例如: project = {project} AND resolution = Unresolved order by updated DESC）")
        _update_cfg(p, {"default_jql": args.set_default_jql})
        print(f"已写入项目配置: {p}")
        print(f"  default_jql: {args.set_default_jql}")
        print(f"（{proj} 的拉单将优先用该模板；优先级：项目配置 > 全局配置 > 内置默认）")
        return
    if args.project:
        p = _project_cfg_path(proj)
        print(f"项目 {proj} 的项目配置: {p or '（未配置）'}")
        if p:
            with open(p, encoding="utf-8") as fp:
                print(fp.read().strip())
        g = C.get("default_jql")
        print(f"生效顺序: 项目配置（{'有' if p else '无'}）> 全局配置（{'有 default_jql' if g else '无'}）> 内置默认")
        print(f"内置默认: {DEFAULT_JQL}")
        return
    print(f"项目配置目录: {d}")
    files = sorted(f for f in os.listdir(d)
                   if f.lower().endswith((".yaml", ".yml"))) if os.path.isdir(d) else []
    if not files:
        print("（空）—— 用 pconfig <项目KEY> --default-jql '…' 创建；或直接编辑该目录下的 <KEY>.yaml")
    for f in files:
        print(f"  {f}")
    act = (C.get("active") or "").strip()
    if act:
        p = _project_cfg_path(act)
        if p:
            print(f"当前活动项目 {act}: 使用 {os.path.basename(p)}")
        else:
            fallback = "全局配置" if C.get("default_jql") else "内置默认"
            print(f"当前活动项目 {act}: （无项目配置，落回{fallback}）")


# ---------------- 探查 / 附件 ----------------

def cmd_projects(args):
    _, d = _request("GET", "/rest/api/2/project")
    q = (args.query or "").lower()
    n = 0
    for p in d or []:
        key, name = p.get("key", ""), p.get("name", "")
        if q and q not in key.lower() and q not in name.lower():
            continue
        print(f"{key}\t{name}\t(id={p.get('id')})")
        n += 1
    print(f"共 {n} 个项目（--query: {args.query or '无'}）")


def cmd_fields(args):
    _, d = _request("GET", "/rest/api/2/field")
    q = (args.query or "").lower()
    n = 0
    for f in d or []:
        fid, name = f.get("id", ""), f.get("name", "")
        if q and q not in name.lower() and q not in fid.lower():
            continue
        kind = "custom" if f.get("custom") else "standard"
        t = (f.get("schema") or {}).get("type", "?")
        print(f"{fid}\t{name}\t{t}\t{kind}")
        n += 1
    print(f"共 {n} 个字段（--query: {args.query or '无'}；不查全量时建议带 --query）")


def cmd_attachments(args):
    key = _key_arg(args.key)
    _warn_project(key)
    _, d = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": "attachment"})
    atts = (d.get("fields") or {}).get("attachment") or []
    if not atts:
        print("（该单无附件）")
        return
    if args.id:
        atts2 = [a for a in atts if str(a.get("id")) == str(args.id)]
        if not atts2:
            die(f"未找到附件 id={args.id}（可用: {[(a.get('id'), a.get('filename')) for a in atts]}）")
        atts = atts2
    save_dir = args.save_dir or os.path.join("jira-attachments", key)
    os.makedirs(save_dir, exist_ok=True)
    for a in atts:
        name = _safe_name(a.get("filename") or f"attachment-{a.get('id')}")
        path = os.path.join(save_dir, name)
        raw = _request("GET", a["content"], raw=True)[1]
        with open(path, "wb") as fp:
            fp.write(raw)
        print(f"已下载: {os.path.abspath(path)}  ({_human(a.get('size', 0))})")


# ---------------- resolve ----------------

def _wrap_value(meta, fid, raw):
    """按字段 schema 包装值：select 选项文本->id 并校验、user->name、version->数组。"""
    sch = meta.get("schema") or {}
    t = sch.get("type")
    if t == "option":
        opts = [o for o in (meta.get("allowedValues") or []) if isinstance(o, dict) and o.get("value")]
        for o in opts:
            if _norm(o["value"]) == _norm(str(raw)):
                return {"id": str(o["id"])}
        if opts:
            vals = "、".join(o["value"] for o in opts[:20])
            die(f"字段 {fid}({meta.get('name','?')}) 值「{raw}」不在可选值内，可选项: {vals}")
        return {"value": str(raw)}
    if t == "user":
        return {"name": str(raw)}
    if t == "array" and sch.get("items") == "version":
        return [{"name": str(raw)}]
    return raw


def cmd_resolve(args):
    key = _key_arg(args.key)
    _warn_project(key)
    # 1) 找「解决」流转，并带 transitions.fields 展开（transition screen 字段在此）
    _, d = _request("GET", f"/rest/api/2/issue/{key}/transitions",
                    params={"expand": "transitions.fields"})
    ts = d.get("transitions", [])
    if not ts:
        die(f"当前账号对 {key} 无任何可用流转——常见原因：单子经办人不是你（工作流限制只有经办人能流转）或已到终态。"
            f"如要接手处理，可先执行: {PROG} assign {key} --to me")
    if args.transition_id:
        t = next((x for x in ts if x["id"] == str(args.transition_id)), None)
        if not t:
            die(f"transition id {args.transition_id} 不在可用列表: {[x['id'] for x in ts]}")
        pick = [t]
    else:
        pick = _pick_transition(ts, name_hint="解决") or [
            x for x in ts if x["to"].get("statusCategory", {}).get("key") == "done"
        ]
        if not pick:
            die(f"找不到「解决」类流转，可用: {[(x['id'], x['name'], x['to']['name']) for x in ts]}")
        if len(pick) > 1:
            die(f"「解决」类流转不唯一，请 --transition-id 指定: {[(x['id'], x['name']) for x in pick]}")
    t = pick[0]
    screen_fields = t.get("fields", {}) or {}

    # 2) 构造 transition body 字段：中文标签 -> fieldId 动态匹配
    def find_field(label):
        nl = _norm(label)
        hits = []
        for fid, meta in screen_fields.items():
            name = _norm(meta.get("name", ""))
            if name and (nl in name or name in nl):
                hits.append((fid, meta))
        return hits

    body_fields = {}
    field_names = {}
    values = {"影响范围": args.impact, "发生原因": args.cause,
              "解决方法": args.solution, "预防措施": args.prevention}
    for label, val in values.items():
        if not val:
            continue
        hits = find_field(label)
        if not hits:
            print(f"[警告] 字段标签「{label}」未在流转屏幕中找到，跳过"
                  f"（现有字段: {[m.get('name') for m in screen_fields.values()]}）", file=sys.stderr)
            continue
        if len(hits) > 1:
            # 同名多字段（少见）：取必填的，仍多则取第一个
            req = [h for h in hits if h[1].get("required")]
            hits = req or hits
        fid, meta = hits[0]
        body_fields[fid] = val
        field_names[fid] = meta.get("name", fid)
        print(f"字段映射: {label} -> {fid}")
        opts = [o for o in (meta.get("allowedValues") or []) if isinstance(o, dict) and o.get("value")]
        if opts:
            shown = " / ".join(o["value"] for o in opts[:12])
            print(f"  可选值: {shown}{' …' if len(opts) > 12 else ''}")

    # 3) resolution 值（Fixed 类自动挑）
    if "resolution" in screen_fields and "resolution" not in body_fields:
        meta = screen_fields["resolution"]
        av = meta.get("allowedValues") or []
        fixed = next((v for v in av if "fix" in _norm(v.get("name", ""))), None) or (av[0] if av else None)
        if fixed:
            body_fields["resolution"] = {"id": str(fixed["id"])}
            field_names["resolution"] = "resolution"
            print(f"resolution -> id {fixed['id']} ({fixed.get('name')})")

    # 4) 用户附加 --field 标签=值
    for kv in args.field or []:
        if "=" not in kv:
            die(f"--field 格式应为 标签=值: {kv}")
        label, val = kv.split("=", 1)
        hits = find_field(label.strip())
        if not hits:
            die(f"附加字段「{label}」未找到（可先 --dry-run 查看流转屏幕全部字段名）")
        fid = hits[0][0]
        body_fields[fid] = val.strip()
        field_names.setdefault(fid, hits[0][1].get("name", fid))

    # 5) 按字段 schema 包装值（先校验已填值：选项词表错会在这里拦截）
    body_fields = {fid: _wrap_value(screen_fields.get(fid) or {}, fid, v) for fid, v in body_fields.items()}

    # 6) 必填检查（transition screen 层，非 editmeta）
    unfilled = []
    for fid, meta in screen_fields.items():
        if meta.get("required") and fid not in body_fields:
            unfilled.append(f"{fid}({meta.get('name','?')})")
    if unfilled:
        die(f"以下必填字段未填，已中止不流转: {unfilled}；"
            f"可先 --dry-run 查看屏幕字段清单，再用 --field '字段名=值' 补齐")

    payload = {"transition": {"id": t["id"]}, "fields": body_fields}
    if args.comment:
        payload["update"] = {"comment": [{"add": {"body": args.comment}}]}

    if args.dry_run:
        print(f"[DRY-RUN] 当前: {_status_line(key)}")
        print(f"[DRY-RUN] 将 POST /rest/api/2/issue/{key}/transitions payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        if args.assign:
            tgt = _resolve_user(key, args.assign)
            print(f"[DRY-RUN] 将转交经办人 → {tgt.get('displayName')} (name={tgt['name']})")
        print("[DRY-RUN] 流转屏幕字段清单（新实例探查用）:")
        for fid, meta in screen_fields.items():
            req = "是" if meta.get("required") else "否"
            print(f"  {fid}\t{meta.get('name','?')}\t必填={req}\t类型={(meta.get('schema') or {}).get('type','?')}")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return

    # 7) 发流转 + 读回（状态/resolution/已填字段逐项核对）
    _request("POST", f"/rest/api/2/issue/{key}/transitions", body=payload)
    fids = sorted(body_fields.keys())
    px = "status,resolution" + (("," + ",".join(fids)) if fids else "")
    _, chk = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": px})
    f = chk["fields"]
    res = (f.get("resolution") or {}).get("name", "-")
    print(f"已流转 {key}: {t['name']} -> {t['to']['name']} @ {C['base']}；"
          f"读回: {key} -> {f['status']['name']} (resolution={res})")
    ok_names = [field_names.get(fid, fid) for fid in fids if f.get(fid)]
    empty_names = [field_names.get(fid, fid) for fid in fids if not f.get(fid)]
    if empty_names:
        print(f"[警告] 以下字段读回为空，请人工核对: {empty_names}", file=sys.stderr)
    if ok_names:
        print(f"[校验] 已读回字段: {'、'.join(ok_names)}")

    # 8) 转交经办人（独立 PUT，便于回滚判断）
    if args.assign:
        tgt = _resolve_user(key, args.assign)
        _request("PUT", f"/rest/api/2/issue/{key}/assignee", body={"name": tgt["name"]})
        _, a = _request("GET", f"/rest/api/2/issue/{key}", params={"fields": "assignee"})
        cur = (a["fields"].get("assignee") or {})
        mark = "✓" if cur.get("name") == tgt["name"] else "⚠ 读回与目标不一致，请核对"
        print(f"已转交经办人: {tgt.get('displayName')} (name={tgt['name']}) {mark}")


def main():
    global C
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(
        prog=PROG,
        description=f"JIRA REST CLI（纯数据流，v{VERSION}）—— 初始化/拉单/读单/流转/指派/评论，全部走 REST API，零浏览器。")
    p.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    p.add_argument("--config", metavar="PATH", help=f"凭据文件路径（默认 {_default_creds_path()}）")
    p.add_argument("--profile", metavar="NAME", help=f"凭据 profile 名（读 {_profiles_dir()}/NAME.yaml；多实例/多账号）")
    p.add_argument("--timeout", type=int, metavar="SEC", help="请求超时秒数（默认 60）")
    p.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验（默认失败后自动降级重试）")
    sub = p.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="初始化向导：配置账号 → 验证 → 登记项目 → 设活动项目（--check 只体检）")
    ini.add_argument("--check", action="store_true", help="只检查当前配置状态（返回码 0=已就绪），不做任何修改")
    ini.add_argument("--base-url", help="JIRA 地址（非交互模式必填，除非配置里已有）")
    ini.add_argument("--username", help="登录名")
    ini.add_argument("--password", help="密码（交互模式下建议不传此参数，改用提示输入且不回显）")
    ini.add_argument("--token", help="Bearer token（Server PAT；与密码二选一）")
    ini.add_argument("--projects", help="登记的项目 KEY，逗号分隔（或 all=全部）")
    ini.add_argument("--use", help="顺便把活动项目设为该 KEY")
    ini.set_defaults(fn=cmd_init)

    us = sub.add_parser("use", help="查看/切换当前活动项目（不带参数=查看列表）")
    us.add_argument("target", nargs="?", help="项目 KEY（或列表里的序号；支持前缀模糊匹配）")
    us.set_defaults(fn=cmd_use)

    pc = sub.add_parser("pconfig", help="项目级配置（拉单模板）：查看/设置；优先级 项目配置>全局配置>内置默认")
    pc.add_argument("project", nargs="?", help="项目 KEY（省略=列目录；配合 --default-jql/--clear 时默认用当前活动项目）")
    pc.add_argument("--default-jql", dest="set_default_jql", metavar="JQL",
                    help="写入该项目默认拉单模板（必须含 {project} 占位符）")
    pc.add_argument("--clear", action="store_true", help="删除该项目的配置文件")
    pc.set_defaults(fn=cmd_pconfig)

    s = sub.add_parser("search", help="JQL 搜索 bug 清单（超 100 自动分页；不传条件=当前活动项目我的未解决）")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--jql", help="原始 JQL，如 'project = X AND resolution = Unresolved'")
    g.add_argument("--url", help="过滤器链接（自动提取 jql 参数），如 https://主机/issues/?jql=…")
    g.add_argument("--project", metavar="KEY", help="本次只查该项目（ALL=不限项目；不改活动项目）")
    s.add_argument("--max", type=int, default=100, help="最多取多少张（默认 100）")
    s.add_argument("--all", action="store_true", help="取全部（忽略 --max）")
    s.add_argument("--all-projects", action="store_true",
                   help="显式允许跨项目搜索（未限定 project 的 --jql/--url 默认被拦截）")
    s.add_argument("--format", choices=["table", "json", "md"], default="table")
    s.add_argument("--fields", default="key,summary,status,priority,issuetype,updated,assignee",
                   help="逗号分隔字段列表")
    s.set_defaults(fn=cmd_search)

    i = sub.add_parser("issue", help="拉单详情+评论+附件（单号可只写数字=当前活动项目）")
    i.add_argument("key")
    i.add_argument("--save", help="保存到文件（推荐 AI 阅读）")
    i.add_argument("--format", choices=["md", "json"], default="md")
    i.set_defaults(fn=cmd_issue)

    tr = sub.add_parser("transitions", help="查看可用流转")
    tr.add_argument("key")
    tr.set_defaults(fn=cmd_transitions)

    st = sub.add_parser("start", help="「接受」流转（未开始→Working）")
    st.add_argument("key")
    st.add_argument("--transition-id", help="强制指定流转 id（名称匹配歧义时）")
    st.add_argument("--dry-run", action="store_true", help="只预演不提交")
    st.set_defaults(fn=cmd_start)

    em = sub.add_parser("editmeta", help="查看单子可编辑字段 id/必填")
    em.add_argument("key")
    em.set_defaults(fn=cmd_editmeta)

    r = sub.add_parser("resolve", help="流转「解决」+填必填字段+转交")
    r.add_argument("key")
    r.add_argument("--impact", help="影响范围")
    r.add_argument("--cause", help="发生原因")
    r.add_argument("--solution", help="解决方法")
    r.add_argument("--prevention", help="预防措施")
    r.add_argument("--assign", help="解决后转交的经办人（显示名或登录名）")
    r.add_argument("--comment", help="流转时附带一条评论（JIRA wiki 语法）")
    r.add_argument("--field", action="append", help="附加字段 标签=值（可多次）")
    r.add_argument("--transition-id", help="强制指定流转 id")
    r.add_argument("--dry-run", action="store_true", help="只预演不提交（并打印流转屏幕字段清单）")
    r.set_defaults(fn=cmd_resolve)

    a = sub.add_parser("assign", help="改经办人（接管/转派/指派）；--to me 表示自己")
    a.add_argument("key")
    a.add_argument("--to", required=True, help="目标人显示名/登录名，或 me")
    a.add_argument("--dry-run", action="store_true", help="只预演不提交")
    a.set_defaults(fn=cmd_assign)

    cm = sub.add_parser("comment", help="加评论（协作/交接说明）")
    cm.add_argument("key")
    gc = cm.add_mutually_exclusive_group(required=True)
    gc.add_argument("--body", help="评论内容（JIRA wiki 语法；@人写 [~登录名]）")
    gc.add_argument("--body-file", help="评论内容从文件读取（UTF-8）")
    cm.add_argument("--dry-run", action="store_true", help="只预演不提交")
    cm.set_defaults(fn=cmd_comment)

    w = sub.add_parser("whoami", help="当前登录身份/实例/活动项目（冒烟验证首选）")
    w.set_defaults(fn=cmd_whoami)

    pr = sub.add_parser("projects", help="列出可见项目（接入新实例先跑它）")
    pr.add_argument("--query", help="按 key/名称过滤（不区分大小写）")
    pr.set_defaults(fn=cmd_projects)

    fl = sub.add_parser("fields", help="列出字段（查自定义字段 id/类型）")
    fl.add_argument("--query", help="按名称/id 过滤（不区分大小写）")
    fl.set_defaults(fn=cmd_fields)

    at = sub.add_parser("attachments", help="下载单子附件（截图等）")
    at.add_argument("key")
    at.add_argument("--save-dir", help="保存目录（默认 ./jira-attachments/<KEY>/）")
    at.add_argument("--id", help="只下载指定附件 id（可先跑 issue KEY 看列表）")
    at.set_defaults(fn=cmd_attachments)

    args = p.parse_args()
    if args.cmd == "init":
        # init 自行处理配置的加载/创建（首次运行时文件还不存在）
        args.fn(args)
        return
    path = resolve_creds_path(args)
    C = load_creds(path)
    if args.timeout:
        C["timeout"] = args.timeout
    if args.insecure:
        C["insecure"] = True
    args.fn(args)


if __name__ == "__main__":
    main()
