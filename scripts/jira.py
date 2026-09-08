#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jira.py — quectel JIRA Server (8.13) REST API CLI，纯数据流、零浏览器、零第三方依赖。

认证: HTTP Basic。凭据文件默认 %LOCALAPPDATA%\hermes\jira-api-creds.yaml
      （环境变量 JIRA_CREDS_PATH 可覆盖），flat yaml 三键：base_url / username / password。

子命令: search / issue / transitions / editmeta / start / resolve
每个子命令 --help 有详细用法。错误退出码 1，正常 0。
"""
import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

PROG = os.path.basename(sys.argv[0])
DEFAULT_CREDS = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes", "jira-api-creds.yaml"
)

# resolve 时按中文标签填写的四个必填自定义字段
RESOLVE_LABELS = {
    "影响范围": "--impact",
    "发生原因": "--cause",
    "解决方法": "--solution",
    "预防措施": "--prevention",
}


def die(msg, code=1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def _norm(s):
    """名称归一化：去空格/括号/星号/换行/分隔符差异（、，,;/与／视同不存在），用于中文标签与选项的宽容匹配。"""
    if not s:
        return ""
    return re.sub(r"[\s（）()*，,、;；/／\u3000]+", "", s).lower()


# ---------------- 凭据 ----------------

def load_creds(path=None):
    path = path or os.environ.get("JIRA_CREDS_PATH") or DEFAULT_CREDS
    if not os.path.exists(path):
        die(f"凭据文件不存在: {path}\n请创建（base_url/username/password 三键）后重试。")
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
    missing = [k for k in ("base_url", "username", "password") if not creds.get(k)]
    if missing:
        die(f"凭据文件 {path} 缺字段: {', '.join(missing)}")
    return creds["base_url"].rstrip("/"), creds["username"], creds["password"], path


# ---------------- HTTP ----------------

_ctx = None
_ctx_unverified = None


def _contexts():
    global _ctx, _ctx_unverified
    if _ctx is None:
        _ctx = ssl.create_default_context()
    if _ctx_unverified is None:
        _ctx_unverified = ssl.create_default_context()
        _ctx_unverified.check_hostname = False
        _ctx_unverified.verify_mode = ssl.CERT_NONE
    return _ctx, _ctx_unverified


def _request(base, user, pw, method, path, params=None, body=None):
    url = base + path
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    req = urllib.request.Request(url, method=method)
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    ctxs = _contexts()
    last_err = None
    for i, ctx in enumerate(ctxs):
        try:
            with urllib.request.urlopen(req, data, timeout=60, context=ctx) as r:
                raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                msgs = err.get("errorMessages") or list(err.get("errors", {}).values())
                detail = " | ".join(str(m) for m in msgs[:3])
            except Exception:
                pass
            if e.code in (401, 403):
                die(f"认证失败 HTTP {e.code}: {detail or '检查凭据文件或账号被 SSO 锁'}（凭据: {os.path.basename(os.environ.get('JIRA_CREDS_PATH') or DEFAULT_CREDS)}）")
            die(f"HTTP {e.code} {method} {path}: {detail or e.reason}")
        except (urllib.error.URLError, ssl.SSLError) as e:
            last_err = e
            if i == 0:
                print("[警告] 证书校验失败，已退回不校验重试（内网自签证书常见）", file=sys.stderr)
    die(f"网络错误: {last_err}")


def _status_line(base, user, pw, key):
    _, data = _request(base, user, pw, "GET", f"/rest/api/2/issue/{key}", params={"fields": "status,resolution"})
    f = data["fields"]
    res = (f.get("resolution") or {}).get("name", "-")
    return f"{key} -> {f['status']['name']} (resolution={res})"


def _fmt_fields_csv(fields):
    if isinstance(fields, (list, tuple)):
        return ",".join(fields)
    return fields


# ---------------- search ----------------

def cmd_search(args):
    _, data = _request(
        args.base, args.user, args.pw, "GET", "/rest/api/2/search",
        params={
            "jql": args.jql,
            "maxResults": args.max,
            "fields": _fmt_fields_csv(args.fields),
        },
    )
    issues = data.get("issues", [])
    rows = []
    for it in issues:
        f = it["fields"]
        rows.append({
            "key": it["key"],
            "状态": f["status"]["name"],
            "优先级": f.get("priority") or {"name": "-"},
            "更新": (f.get("updated") or "")[:10],
            "类型": (f.get("issuetype") or {"name": "-"})["name"],
            "摘要": f.get("summary", ""),
        })
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    print(f"共 {data.get('total', len(rows))} 张（已取 {len(rows)}）")
    if not rows:
        return
    if args.format == "md":
        print("| KEY | 类型 | 状态 | 优先级 | 更新 | 摘要 |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['key']} | {r['类型']} | {r['状态']} | {r['优先级']['name']} | {r['更新']} | {r['摘要']} |")
        return
    # table
    cols = ["key", "类型", "状态", "优先级", "更新", "摘要"]
    widths = {c: max(len(c), *(len(str(r[c])) if not isinstance(r[c], dict) else len(r[c]["name"]) for r in rows)) for c in cols}
    widths["摘要"] = min(widths["摘要"], 60)
    hdr = " | ".join(c.ljust(widths[c]) for c in cols)
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        cells = []
        for c in cols:
            v = r[c] if not isinstance(r[c], dict) else r[c]["name"]
            s = str(v)
            if c == "摘要" and len(s) > 60:
                s = s[:59] + "…"
            cells.append(s.ljust(widths[c]))
        print(" | ".join(cells))


# ---------------- issue ----------------

def cmd_issue(args):
    fields = "summary,description,status,priority,issuetype,assignee,reporter,created,updated,comment,resolution"
    _, d = _request(args.base, args.user, args.pw, "GET", f"/rest/api/2/issue/{args.key}", params={"fields": fields})
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
        "## 评论",
        "",
    ]
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
    _, d = _request(args.base, args.user, args.pw, "GET", f"/rest/api/2/issue/{args.key}/transitions", params={"fields": "status"})
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
    _, d = _request(args.base, args.user, args.pw, "GET", f"/rest/api/2/issue/{args.key}/transitions")
    ts = d.get("transitions", [])
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
            names = [(t["id"], t["name"]) for t in pick]
            die(f"「接受」类流转不唯一，请 --transition-id 指定: {names}")
    tid = pick[0]
    _request(args.base, args.user, args.pw, "POST", f"/rest/api/2/issue/{args.key}/transitions", body={"transition": {"id": tid["id"]}})
    print(f"已流转: {tid['name']} -> {tid['to']['name']}；读回: {_status_line(args.base, args.user, args.pw, args.key)}")


# ---------------- editmeta / resolve ----------------

def cmd_editmeta(args):
    _, d = _request(args.base, args.user, args.pw, "GET", f"/rest/api/2/issue/{args.key}/editmeta")
    fields = d.get("fields", {})
    if not fields:
        print("（无任何可编辑字段）")
        return
    for fid, meta in fields.items():
        print(f"{fid}\t{meta.get('name','?')}\t必填={meta.get('required')}\t类型={meta.get('schema',{}).get('type','?')}")


def cmd_resolve(args):
    key = args.key
    # 1) 找「解决」流转，并带 transitions.fields 展开（transition screen 字段在此）
    _, d = _request(
        args.base, args.user, args.pw, "GET", f"/rest/api/2/issue/{key}/transitions",
        params={"expand": "transitions.fields"},
    )
    ts = d.get("transitions", [])
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
    unfilled_required = []
    assigned_labels = {}
    values = {"影响范围": args.impact, "发生原因": args.cause, "解决方法": args.solution, "预防措施": args.prevention}
    for label, val in values.items():
        if not val:
            continue
        hits = find_field(label)
        if not hits:
            print(f"[警告] 字段标签「{label}」未在流转屏幕中找到，跳过（现有字段: {[m.get('name') for m in screen_fields.values()]}", file=sys.stderr)
            continue
        if len(hits) > 1:
            # 同名多字段（少见）：取必填的，仍多则报错
            req = [h for h in hits if h[1].get("required")]
            hits = req or hits
        fid, meta = hits[0]
        body_fields[fid] = val
        assigned_labels[label] = fid
        print(f"字段映射: {label} -> {fid}")
        opts = [o for o in (meta.get("allowedValues") or []) if isinstance(o, dict) and o.get("value")]
        if opts:
            shown = " / ".join(o["value"] for o in opts[:12])
            print(f"  可选值: {shown}{' …' if len(opts) > 12 else ''}")

    # 3) resolution 值（Fixed）
    if "resolution" in screen_fields and "resolution" not in body_fields:
        meta = screen_fields["resolution"]
        av = meta.get("allowedValues") or []
        fixed = next((v for v in av if "fix" in _norm(v.get("name", ""))), None) or (av[0] if av else None)
        if fixed:
            body_fields["resolution"] = {"id": str(fixed["id"])}
            print(f"resolution -> id {fixed['id']} ({fixed.get('name')})")
        elif meta.get("required"):
            unfilled_required.append("resolution")

    # 4) 用户附加 --field 标签=值
    for kv in args.field or []:
        if "=" not in kv:
            die(f"--field 格式应为 标签=值: {kv}")
        label, val = kv.split("=", 1)
        hits = find_field(label.strip())
        if not hits:
            die(f"附加字段「{label}」未找到")
        body_fields[hits[0][0]] = val.strip()

    # 5) 必填检查（transition screen 层，非 editmeta）
    for fid, meta in screen_fields.items():
        if meta.get("required") and fid not in body_fields:
            unfilled_required.append(f"{fid}({meta.get('name','?')})")
    if unfilled_required:
        die(f"以下必填字段未填，已中止不流转: {unfilled_required}")

    # 5.5) 按字段 schema 包装值（select 选项文本->id 并校验、user->name、version->数组）
    def wrap_value(fid, raw):
        meta = screen_fields.get(fid) or {}
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

    body_fields = {fid: wrap_value(fid, v) for fid, v in body_fields.items()}

    # 6) 发流转
    _request(args.base, args.user, args.pw, "POST", f"/rest/api/2/issue/{key}/transitions", body={"transition": {"id": t["id"]}, "fields": body_fields})
    msg = f"已流转 {key}: {t['name']} -> {t['to']['name']}；读回: {_status_line(args.base, args.user, args.pw, key)}"
    print(msg)

    # 7) 转交测试人员（独立 PUT assignee，便于回滚判断）
    if args.assign:
        _, users = _request(
            args.base, args.user, args.pw, "GET", "/rest/api/2/user/assignable/search",
            params={"issueKey": key, "query": args.assign, "maxResults": 20},
        )
        exact = [u for u in users if u.get("displayName") == args.assign]
        cand = exact or users
        if not cand:
            die(f"未找到可指派人匹配「{args.assign}」（该人不属于本项目 assignable 用户）")
        target = cand[0]
        if len(cand) > 1 and not exact:
            print(f"[警告] 匹配到多个用户，取第一个 {target.get('displayName')}：{[u.get('displayName') for u in cand]}", file=sys.stderr)
        _request(args.base, args.user, args.pw, "PUT", f"/rest/api/2/issue/{key}/assignee", body={"name": target["name"]})
        print(f"已转交经办人: {target.get('displayName')} (name={target['name']})")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog=PROG, description="quectel JIRA REST CLI（纯数据流）")
    p.add_argument("--config", help=f"凭据文件路径（默认 {DEFAULT_CREDS}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="JQL 搜索 bug 清单")
    s.add_argument("--jql", required=True, help="原始 JQL，如 'project = X AND resolution = Unresolved'")
    s.add_argument("--max", type=int, default=100)
    s.add_argument("--format", choices=["table", "json", "md"], default="table")
    s.add_argument("--fields", default="key,summary,status,priority,issuetype,updated", help="逗号分隔字段列表")
    s.set_defaults(fn=cmd_search)

    i = sub.add_parser("issue", help="拉单详情+评论")
    i.add_argument("key")
    i.add_argument("--save", help="保存 markdown 到文件（推荐 AI 阅读）")
    i.set_defaults(fn=cmd_issue)

    tr = sub.add_parser("transitions", help="查看可用流转")
    tr.add_argument("key")
    tr.set_defaults(fn=cmd_transitions)

    st = sub.add_parser("start", help="「接受」流转（未开始→Working/处理中）")
    st.add_argument("key")
    st.add_argument("--transition-id", help="强制指定流转 id（名称匹配歧义时）")
    st.set_defaults(fn=cmd_start)

    em = sub.add_parser("editmeta", help="查看单子可编辑字段 id/必填")
    em.add_argument("key")
    em.set_defaults(fn=cmd_editmeta)

    r = sub.add_parser("resolve", help="流转到「解决」并填四项必填+resolution=Fixed")
    r.add_argument("key")
    r.add_argument("--impact", help="影响范围")
    r.add_argument("--cause", help="发生原因")
    r.add_argument("--solution", help="解决方法")
    r.add_argument("--prevention", help="预防措施")
    r.add_argument("--assign", help="解决后转交的测试人员显示名")
    r.add_argument("--field", action="append", help="附加字段 标签=值（可多次）")
    r.add_argument("--transition-id", help="强制指定流转 id")
    r.set_defaults(fn=cmd_resolve)

    args = p.parse_args()
    base, user, pw, path = load_creds(args.config)
    args.base, args.user, args.pw = base, user, pw
    args.fn(args)


if __name__ == "__main__":
    main()
