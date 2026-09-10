#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jira.py — JIRA REST API CLI（纯数据流、零浏览器、零第三方依赖，单文件）。

适用: JIRA Server / Data Center 8.x（HTTP Basic）为主；可选 Bearer token（Server PAT、
      Cloud 用「邮箱+API Token」当 Basic——差异见 docs/03）。

认证: 凭据文件（flat yaml），默认 %LOCALAPPDATA%\\hermes\\jira-api-creds.yaml
      （macOS/Linux ~/hermes/...；环境变量 JIRA_CREDS_PATH 可覆盖）。
      多实例/多账号: --profile <名字> 读 profiles 目录 <名字>.yaml
      （默认 %LOCALAPPDATA%\\hermes\\jira-profiles\\，JIRA_PROFILES_DIR 可覆盖）。
      键: base_url / username / password，可选 token（Bearer）/ insecure / timeout。

子命令: search / issue / transitions / editmeta / start / resolve / assign /
        comment / whoami / projects / fields / attachments
写操作（start/resolve/assign/comment）均支持 --dry-run 预演，执行后自动读回验证。
每个子命令 --help 有详细用法。错误退出码 1，正常 0。
"""
import argparse
import base64
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
VERSION = "2.0.0"

# 运行期配置（main 里填充）：base / user / pw / token / insecure / timeout / path
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


def resolve_creds_path(args):
    """凭据文件定位：--config > --profile/JIRA_PROFILE > JIRA_CREDS_PATH > 默认路径。"""
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
        avail = []
        if os.path.isdir(d):
            avail = sorted(f[:-5] if f.lower().endswith(".yaml") else f[:-4]
                           for f in os.listdir(d) if f.lower().endswith((".yaml", ".yml")))
        die(f"找不到 profile「{name}」：期望 {os.path.join(d, name + '.yaml')}\n"
            f"可用 profiles: {avail or '（目录不存在或为空；新建方法见 docs/01）'}")
    return os.environ.get("JIRA_CREDS_PATH") or _default_creds_path()


def load_creds(path):
    if not os.path.exists(path):
        die(f"凭据文件不存在: {path}\n请创建（base_url/username/password 三键）后重试；"
            f"多实例可用 --profile（见 docs/01）。")
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
    missing = []
    if not creds.get("base_url"):
        missing.append("base_url")
    if not (creds.get("token") or (creds.get("username") and creds.get("password"))):
        missing.append("username+password（或 token）")
    if missing:
        die(f"凭据文件 {path} 缺字段: {', '.join(missing)}")
    timeout = 60
    if creds.get("timeout"):
        try:
            timeout = int(float(creds["timeout"]))
        except ValueError:
            print(f"[警告] timeout 值无法解析: {creds['timeout']}，改用默认 60", file=sys.stderr)
    return {
        "base": creds["base_url"].rstrip("/"),
        "user": creds.get("username", ""),
        "pw": creds.get("password", ""),
        "token": creds.get("token", ""),
        "insecure": str(creds.get("insecure", "")).strip().lower() in ("1", "true", "yes", "on"),
        "timeout": timeout,
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


# ---------------- search ----------------

def _jql_from_url(u):
    q = urlparse(u).query
    if not q and "?" in u:
        q = u.split("?", 1)[1]
    qs = parse_qs(q)
    if not qs.get("jql"):
        die("URL 里没有 jql 参数。请用过滤器页地址（含 /issues/?jql=… 的链接）；"
            "browse 单页链接不含 JQL，不能直接当清单用。")
    return qs["jql"][0]


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
    jql = args.jql or _jql_from_url(args.url)
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
    fields = ("summary,description,status,priority,issuetype,assignee,reporter,"
              "created,updated,comment,resolution,attachment")
    _, d = _request("GET", f"/rest/api/2/issue/{args.key}", params={"fields": fields})
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
    _, d = _request("GET", f"/rest/api/2/issue/{args.key}/transitions", params={"fields": "status"})
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
    _, d = _request("GET", f"/rest/api/2/issue/{args.key}/transitions")
    ts = d.get("transitions", [])
    if not ts:
        die(f"当前账号对 {args.key} 无任何可用流转——常见原因：单子经办人不是你（工作流限制只有经办人能流转）或已到终态。"
            f"如要接手处理，可先执行: {PROG} assign {args.key} --to me")
    if args.transition_id:
        tid = next((t for t in ts if t["id"] == str(args.transition_id)), None)
        if not tid:
            die(f"transition id {args.transition_id} 不在可用列表: {[t['id'] for t in ts]}")
        pick = [tid]
    else:
        pick = _pick_transition(ts, name_hint="接受") or _pick_transition(ts, to_category="indeterminate")
        if not pick:
            die(f"找不到「接受」类流转，可用: {[(t['id'], t['name'], t['to']['name']) for t in ts]}")
        if len(pick) > 1:
            die(f"「接受」类流转不唯一，请 --transition-id 指定: {[(t['id'], t['name']) for t in pick]}")
    tid = pick[0]
    payload = {"transition": {"id": tid["id"]}}
    if args.dry_run:
        print(f"[DRY-RUN] 当前: {_status_line(args.key)}")
        print(f"[DRY-RUN] 将 POST /rest/api/2/issue/{args.key}/transitions body: "
              f"{json.dumps(payload, ensure_ascii=False)}（{tid['name']} -> {tid['to']['name']}）")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return
    _request("POST", f"/rest/api/2/issue/{args.key}/transitions", body=payload)
    print(f"已流转: {tid['name']} -> {tid['to']['name']} @ {C['base']}；读回: {_status_line(args.key)}")


# ---------------- editmeta ----------------

def cmd_editmeta(args):
    _, d = _request("GET", f"/rest/api/2/issue/{args.key}/editmeta")
    fields = d.get("fields", {})
    if not fields:
        print("（无任何可编辑字段）")
        return
    for fid, meta in fields.items():
        print(f"{fid}\t{meta.get('name','?')}\t必填={meta.get('required')}\t类型={meta.get('schema',{}).get('type','?')}")


# ---------------- 人员 / 协作 ----------------

def _resolve_user(key, query):
    _, users = _request("GET", "/rest/api/2/user/assignable/search",
                        params={"issueKey": key, "query": query, "maxResults": 20})
    exact = [u for u in users if u.get("displayName") == query or u.get("name") == query]
    cand = exact or users
    if not cand:
        die(f"未找到可指派人匹配「{query}」（该人不属于本项目 assignable 用户？换登录名/显示名再试）")
    target = cand[0]
    if len(cand) > 1 and not exact:
        print(f"[警告] 匹配到多个用户，取第一个 {target.get('displayName')}："
              f"{[u.get('displayName') for u in users]}", file=sys.stderr)
    return target


def cmd_assign(args):
    key = args.key
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
        print(f"[DRY-RUN] 将 POST /rest/api/2/issue/{args.key}/comment body: "
              f"{json.dumps(payload, ensure_ascii=False)}")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return
    _, d = _request("POST", f"/rest/api/2/issue/{args.key}/comment", body=payload)
    cid = d.get("id")
    _, chk = _request("GET", f"/rest/api/2/issue/{args.key}/comment/{cid}")
    ok = str(chk.get("id")) == str(cid)
    print(f"已评论 {args.key} (comment id={cid}) @ {C['base']}；"
          f"读回: id={chk.get('id')} by {(chk.get('author') or {}).get('displayName','-')} {'✓' if ok else '⚠'}")


def cmd_whoami(args):
    _, d = _request("GET", "/rest/api/2/myself")
    auth = "token" if C.get("token") else "basic"
    print(f"身份: {d.get('displayName')} (name={d.get('name')}, email={d.get('emailAddress', '-')})")
    print(f"实例: {C['base']}（凭据: {C['path']}，认证: {auth}）")


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
    _, d = _request("GET", f"/rest/api/2/issue/{args.key}", params={"fields": "attachment"})
    atts = (d.get("fields") or {}).get("attachment") or []
    if not atts:
        print("（该单无附件）")
        return
    if args.id:
        atts2 = [a for a in atts if str(a.get("id")) == str(args.id)]
        if not atts2:
            die(f"未找到附件 id={args.id}（可用: {[(a.get('id'), a.get('filename')) for a in atts]}）")
        atts = atts2
    save_dir = args.save_dir or os.path.join("jira-attachments", args.key)
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
    key = args.key
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
        description=f"JIRA REST CLI（纯数据流，v{VERSION}）—— 拉单/读单/流转/指派/评论，全部走 REST API，零浏览器。")
    p.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    p.add_argument("--config", metavar="PATH", help=f"凭据文件路径（默认 {_default_creds_path()}）")
    p.add_argument("--profile", metavar="NAME", help=f"凭据 profile 名（读 {_profiles_dir()}/NAME.yaml；多实例/多账号）")
    p.add_argument("--timeout", type=int, metavar="SEC", help="请求超时秒数（默认 60）")
    p.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验（默认失败后自动降级重试）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="JQL 搜索 bug 清单（超 100 自动分页）")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--jql", help="原始 JQL，如 'project = X AND resolution = Unresolved'")
    g.add_argument("--url", help="过滤器链接（自动提取 jql 参数），如 https://主机/issues/?jql=…")
    s.add_argument("--max", type=int, default=100, help="最多取多少张（默认 100）")
    s.add_argument("--all", action="store_true", help="取全部（忽略 --max）")
    s.add_argument("--format", choices=["table", "json", "md"], default="table")
    s.add_argument("--fields", default="key,summary,status,priority,issuetype,updated,assignee",
                   help="逗号分隔字段列表")
    s.set_defaults(fn=cmd_search)

    i = sub.add_parser("issue", help="拉单详情+评论+附件")
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

    w = sub.add_parser("whoami", help="当前登录身份/实例（冒烟验证首选）")
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
    path = resolve_creds_path(args)
    C = load_creds(path)
    if args.timeout:
        C["timeout"] = args.timeout
    if args.insecure:
        C["insecure"] = True
    args.fn(args)


if __name__ == "__main__":
    main()
