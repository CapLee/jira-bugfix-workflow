#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_offline.py — jira.py 离线自测（起本地 mock JIRA，跑真实 CLI 子进程）。

零外部依赖、零真实请求：不会碰任何真实 JIRA。
用法:  python3 scripts/selftest_offline.py     （退出码 0 = 全过）
覆盖: 认证(Basic/Bearer)、profile、init 初始化向导、use 活动项目切换、
      --url 解析、自动分页、429/5xx 重试、dry-run 零写入、resolve 读回校验、
      assign/comment/attachments、纯数字单号、跨项目提示、各类错误路径、
      项目文件夹配置（就近发现、优先级、pconfig --here）。
      改完 jira.py 跑一遍。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote_plus

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jira.py")
BASE_TMP = tempfile.mkdtemp(prefix="jira-selftest-")
PROJCFG = os.path.join(BASE_TMP, "proj-cfg")
os.makedirs(PROJCFG, exist_ok=True)

PORT = 0
state = {"requests": [], "fail_search": 0, "assignee": {"name": "tester", "displayName": "测试员甲"},
         "posts": [], "puts": []}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj, extra=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _empty(self, code, extra=None):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _body(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(ln).decode() if ln else ""

    def _route(self, method):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        auth = self.headers.get("Authorization", "")
        state["requests"].append((method, self.path, auth))
        if path == "/__ctl/set":
            for k, v in qs.items():
                state[k] = int(v[0]) if v[0].isdigit() else v[0]
            return self._json(200, {"ok": True})
        if auth.startswith("Basic "):
            try:
                user = base64.b64decode(auth[6:]).decode().split(":", 1)[0]
                if user == "nouser":
                    return self._json(401, {"errorMessages": ["Basic auth failed"]})
            except Exception:
                pass
        if method == "GET":
            if path == "/rest/api/2/myself":
                return self._json(200, {"name": "tester", "displayName": "测试员甲", "emailAddress": "t@x.com"})
            if path == "/rest/api/2/search":
                if state["fail_search"] > 0:
                    state["fail_search"] -= 1
                    return self._empty(503, {"Retry-After": "1"})
                total = 150
                start = int((qs.get("startAt") or ["0"])[0])
                mr = int((qs.get("maxResults") or ["100"])[0])
                issues = [{
                    "key": f"T-{i}",
                    "fields": {"summary": f"bug {i}", "status": {"name": "未开始"},
                               "priority": {"name": "P2-一般"}, "issuetype": {"name": "BUG"},
                               "updated": "2026-09-09T10:00:00.000+0800",
                               "assignee": {"name": "tester", "displayName": "张三同事"}},
                } for i in range(start, min(start + mr, total))]
                return self._json(200, {"total": total, "startAt": start, "issues": issues})
            if path == "/rest/api/2/issue/T-1/transitions":
                ts = [{"id": "41", "name": "接受", "to": {"name": "Working", "statusCategory": {"key": "indeterminate"}}},
                      {"id": "11", "name": "解决", "to": {"name": "ST Check", "statusCategory": {"key": "indeterminate"}}}]
                if "expand" in self.path:
                    ts[1]["fields"] = {
                        "customfield_13209": {"name": "影响范围", "required": True, "schema": {"type": "textarea"}},
                        "customfield_15817": {"name": "发生原因", "required": True, "schema": {"type": "option"},
                                              "allowedValues": [{"id": "1", "value": "需求理解偏差"}, {"id": "2", "value": "逻辑边界遗漏"}]},
                        "customfield_15818": {"name": "解决方法", "required": True, "schema": {"type": "option"},
                                              "allowedValues": [{"id": "3", "value": "调整UI/样式"}, {"id": "4", "value": "增加边界处理"}]},
                        "customfield_15819": {"name": "预防措施", "required": True, "schema": {"type": "option"},
                                              "allowedValues": [{"id": "5", "value": "无需额外措施"}]},
                    }
                return self._json(200, {"transitions": ts})
            if path == "/rest/api/2/issue/W-1":
                return self._json(200, {"key": "W-1", "fields": {
                    "status": {"name": "Working", "statusCategory": {"key": "indeterminate"}},
                    "resolution": None}})
            if path == "/rest/api/2/issue/W-1/transitions":
                return self._json(200, {"transitions": [
                    {"id": "11", "name": "解决", "to": {"name": "ST Check", "statusCategory": {"key": "indeterminate"}}}]})
            if path in ("/rest/api/2/issue/T-1", "/rest/api/2/issue/STRUCTURING-1"):
                k = path.rsplit("/", 1)[-1]
                fields = {
                    "status": {"name": "Working"}, "resolution": None, "assignee": state["assignee"],
                    "summary": "示例 bug", "description": "步骤A\n\n期望X\n\n实际Y",
                    "priority": {"name": "P2-一般"}, "issuetype": {"name": "BUG"},
                    "reporter": {"displayName": "测试员甲"}, "created": "2026-09-01", "updated": "2026-09-09",
                    "comment": {"comments": [{"author": {"displayName": "测试员甲"},
                                              "created": "2026-09-02", "body": "复测未通过"}]},
                    "attachment": [{"id": "55", "filename": "shot.png", "size": 1234,
                                    "content": f"http://127.0.0.1:{PORT}/att/1/shot.png"}],
                    "customfield_13209": "范围X", "customfield_15817": {"value": "需求理解偏差"},
                    "customfield_15818": {"value": "调整UI/样式"}, "customfield_15819": {"value": "无需额外措施"},
                }
                return self._json(200, {"key": k, "fields": fields})
            if path == "/rest/api/2/issue/NOKEY":
                return self._json(404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]})
            if path == "/rest/api/2/issue/T-1/comment/9001":
                return self._json(200, {"id": "9001", "body": "hello", "author": {"displayName": "测试员甲"}})
            if path == "/rest/api/2/user/assignable/search":
                # 模拟本实例行为：忽略 query 参数、返回全量列表（匹配交由客户端过滤）
                return self._json(200, [{"name": "admin", "displayName": "admin"},
                                        {"name": "target1", "displayName": "目标人乙"},
                                        {"name": "target2", "displayName": "目标人丙"}])
            if path == "/rest/api/2/project":
                return self._json(200, [{"id": "1", "key": "T", "name": "测试项目"},
                                        {"id": "2", "key": "STRUCTURING", "name": "结构化文档"},
                                        {"id": "3", "key": "BPM", "name": "BPM流程"}])
            if path == "/rest/api/2/field":
                return self._json(200, [{"id": "customfield_13209", "name": "影响范围", "custom": True,
                                         "schema": {"type": "textarea"}},
                                        {"id": "summary", "name": "Summary", "custom": False,
                                         "schema": {"type": "string"}}])
            if path == "/att/1/shot.png":
                b = b"PNGDATA-BYTES"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
        return self._json(404, {"errorMessages": [f"mock 未实现: {method} {path}"]})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        state["posts"].append((path, body))
        if path == "/rest/api/2/issue/T-1/transitions":
            return self._empty(204)
        if path == "/rest/api/2/issue/T-1/comment":
            return self._json(201, {"id": "9001", "body": "hello", "author": {"displayName": "测试员甲"}})
        return self._json(404, {"errorMessages": ["mock 未实现"]})

    def do_PUT(self):
        path = urlparse(self.path).path
        body = self._body()
        state["puts"].append((path, body))
        if path == "/rest/api/2/issue/T-1/assignee":
            name = json.loads(body)["name"]
            dn = {"tester": "测试员甲", "target1": "目标人乙", "target2": "目标人丙"}.get(name, name)
            state["assignee"] = {"name": name, "displayName": dn}
            return self._empty(204)
        return self._json(404, {"errorMessages": ["mock 未实现"]})


server = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


def ctl(**kw):
    import urllib.request
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    urllib.request.urlopen(f"{BASE}/__ctl/set?{q}").read()


def run(argv, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["JIRA_PROJECT_CONFIGS_DIR"] = PROJCFG   # 隔离：不受用户真实项目配置影响
    env["JIRA_NO_FOLDER_CFG"] = "1"             # 默认禁用「项目文件夹配置」（专项用例再打开）
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True,
                          text=True, encoding="utf-8", env=env, cwd=cwd or BASE_TMP,
                          stdin=subprocess.DEVNULL)


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   <<< {detail}"))


def last_search_jql():
    for method, path, auth in reversed(state["requests"]):
        if path.startswith("/rest/api/2/search"):
            return parse_qs(urlparse(path).query)["jql"][0]
    return None


def read_cfg(path):
    return open(path, encoding="utf-8").read()


creds_basic = os.path.join(BASE_TMP, "jira-api-creds.yaml")
with open(creds_basic, "w", encoding="utf-8") as fp:
    fp.write(f"base_url: {BASE}\nusername: tester\npassword: pw123\n")
creds_401 = os.path.join(BASE_TMP, "creds401.yaml")
with open(creds_401, "w", encoding="utf-8") as fp:
    fp.write(f"base_url: {BASE}\nusername: nouser\npassword: x\n")
profdir = os.path.join(BASE_TMP, "profiles")
os.makedirs(profdir, exist_ok=True)
with open(os.path.join(profdir, "token-inst.yaml"), "w", encoding="utf-8") as fp:
    fp.write(f"base_url: {BASE}\ntoken: SECRET123\ntimeout: 30\ninsecure: true\n")

r = run(["--version"])
check("01 version", r.returncode == 0 and "2.3.0" in r.stdout, r.stdout + r.stderr)

r = run(["--config", creds_basic, "whoami"])
last_auth = state["requests"][-1][2]
check("02 whoami basic", r.returncode == 0 and "测试员甲" in r.stdout and last_auth.startswith("Basic "),
      r.stdout + r.stderr + last_auth)

state["requests"].clear()
r = run(["--profile", "token-inst", "whoami"], {"JIRA_PROFILES_DIR": profdir})
last_auth = state["requests"][-1][2]
check("03 profile+bearer", r.returncode == 0 and last_auth == "Bearer SECRET123" and "实例:" in r.stdout,
      r.stdout + r.stderr + last_auth)

r = run(["--profile", "nope", "whoami"], {"JIRA_PROFILES_DIR": profdir})
check("04 profile missing", r.returncode == 1 and "可用 profiles" in r.stderr, r.stdout + r.stderr)

r = run(["--config", creds_basic, "--profile", "x", "whoami"])
check("05 conflict", r.returncode == 1 and "只能用一个" in r.stderr, r.stdout + r.stderr)

ctl(fail_search=2)
state["requests"].clear()
url = f"{BASE}/issues/?jql=" + quote_plus("project = T AND resolution = Unresolved")
r = run(["--config", creds_basic, "search", "--url", url, "--max", "150"])
n_search = sum(1 for x in state["requests"] if x[1].startswith("/rest/api/2/search"))
check("06 search retry+paging",
      r.returncode == 0 and "共 150 张（已取 150）" in r.stdout and "张三同事" in r.stdout
      and "经办人" in r.stdout and "重试" in r.stderr and n_search >= 4,
      f"rc={r.returncode} n_search={n_search}\n{r.stdout}\n{r.stderr}")

r = run(["--config", creds_basic, "search", "--jql", "project = T", "--max", "3", "--format", "json"])
try:
    j = json.loads(r.stdout)
    ok = j.get("total") == 150 and j.get("count") == 3 and len(j.get("issues", [])) == 3
except Exception as e:
    ok = False
    j = str(e)
check("07 search json", r.returncode == 0 and ok, f"{r.stdout} {r.stderr}")

r = run(["--config", creds_basic, "search", "--url", f"{BASE}/browse/T-1"])
check("08 url no jql", r.returncode == 1 and "没有 jql 参数" in r.stderr, r.stdout + r.stderr)

r = run(["--config", creds_basic, "issue", "T-1"])
check("09 issue", r.returncode == 0 and "## 附件" in r.stdout and "shot.png" in r.stdout
      and "1.2KB" in r.stdout and "复测未通过" in r.stdout, r.stdout + r.stderr)

r = run(["--config", creds_basic, "transitions", "T-1"])
check("10 transitions", r.returncode == 0 and "接受" in r.stdout and "41" in r.stdout, r.stdout + r.stderr)

posts0, puts0 = len(state["posts"]), len(state["puts"])
r = run(["--config", creds_basic, "resolve", "T-1", "--impact", "范围X", "--cause", "需求理解偏差",
         "--solution", "调整UI/样式", "--prevention", "无需额外措施", "--dry-run"])
ok = (r.returncode == 0 and "[DRY-RUN]" in r.stdout and "customfield_15817" in r.stdout
      and "字段清单" in r.stdout and len(state["posts"]) == posts0 and len(state["puts"]) == puts0)
check("11 resolve dry-run no-write", ok, f"rc={r.returncode} posts {posts0}->{len(state['posts'])}\n{r.stdout}\n{r.stderr}")

r = run(["--config", creds_basic, "resolve", "T-1", "--cause", "不存在XYZ", "--solution", "调整UI/样式",
         "--prevention", "无需额外措施", "--dry-run"])
check("12 dry-run bad option", r.returncode == 1 and "不在可选值内" in r.stderr, r.stdout + r.stderr)

r = run(["--config", creds_basic, "resolve", "T-1", "--impact", "X", "--cause", "需求理解偏差",
         "--solution", "调整UI/样式", "--dry-run"])
check("13 dry-run required", r.returncode == 1 and "必填字段未填" in r.stderr, r.stdout + r.stderr)

r = run(["--config", creds_basic, "resolve", "T-1", "--impact", "范围X", "--cause", "需求理解偏差",
         "--solution", "调整UI/样式", "--prevention", "无需额外措施",
         "--assign", "目标人乙", "--comment", "已修复"])
body = json.loads(state["posts"][-1][1])
ok = (r.returncode == 0 and "已流转 T-1: 解决 -> ST Check" in r.stdout and "[校验]" in r.stdout
      and "已转交经办人: 目标人乙 (name=target1) ✓" in r.stdout
      and body["fields"]["customfield_13209"] == "范围X"
      and body["fields"]["customfield_15817"] == {"id": "1"}
      and body["update"]["comment"][0]["add"]["body"] == "已修复")
check("14 resolve real", ok, f"rc={r.returncode} body={body}\n{r.stdout}\n{r.stderr}")

r = run(["--config", creds_basic, "assign", "T-1", "--to", "me"])
check("15 assign me", r.returncode == 0 and "已指派 T-1 → 测试员甲" in r.stdout and "读回: 测试员甲 ✓" in r.stdout,
      r.stdout + r.stderr)

puts0 = len(state["puts"])
r = run(["--config", creds_basic, "assign", "T-1", "--to", "目标人乙", "--dry-run"])
check("16 assign dry-run", r.returncode == 0 and "[DRY-RUN]" in r.stdout and len(state["puts"]) == puts0,
      f"rc={r.returncode} puts {puts0}->{len(state['puts'])}\n{r.stdout}{r.stderr}")

r = run(["--config", creds_basic, "assign", "T-1", "--to", "目标人乙"])
check("17 assign real", r.returncode == 0 and "读回: 目标人乙 ✓" in r.stdout, r.stdout + r.stderr)

r = run(["--config", creds_basic, "assign", "T-1", "--to", "丙", "--dry-run"])
check("17b assign client-side partial", r.returncode == 0 and "目标人丙" in r.stdout and "target2" in r.stdout,
      r.stdout + r.stderr)

r = run(["--config", creds_basic, "assign", "T-1", "--to", "target2", "--dry-run"])
check("17c assign by login name", r.returncode == 0 and "目标人丙" in r.stdout, r.stdout + r.stderr)

puts1 = len(state["puts"])
r = run(["--config", creds_basic, "assign", "T-1", "--to", "目标", "--dry-run"])
check("17d assign ambiguous die", r.returncode == 1 and "匹配到多个可指派人" in r.stderr
      and "目标人乙" in r.stderr and "目标人丙" in r.stderr and len(state["puts"]) == puts1,
      r.stdout + r.stderr)

r = run(["--config", creds_basic, "comment", "T-1", "--body", "hello"])
check("18 comment real", r.returncode == 0 and "读回: id=9001" in r.stdout, r.stdout + r.stderr)

posts0 = len(state["posts"])
r = run(["--config", creds_basic, "comment", "T-1", "--body", "x", "--dry-run"])
check("19 comment dry-run", r.returncode == 0 and "[DRY-RUN]" in r.stdout and len(state["posts"]) == posts0,
      f"rc={r.returncode} posts {posts0}->{len(state['posts'])}\n{r.stdout}{r.stderr}")

attdir = os.path.join(BASE_TMP, "att")
r = run(["--config", creds_basic, "attachments", "T-1", "--save-dir", attdir])
fpath = os.path.join(attdir, "shot.png")
ok = (r.returncode == 0 and "已下载" in r.stdout and os.path.exists(fpath)
      and open(fpath, "rb").read() == b"PNGDATA-BYTES")
check("20 attachments", ok, f"rc={r.returncode}\n{r.stdout}{r.stderr}")

r = run(["--config", creds_401, "whoami"])
check("21 401", r.returncode == 1 and "认证失败 HTTP 401" in r.stderr and "creds401.yaml" in r.stderr,
      r.stdout + r.stderr)

r = run(["--config", creds_basic, "issue", "NOKEY"])
check("22 404", r.returncode == 1 and "HTTP 404" in r.stderr, r.stdout + r.stderr)

r = run(["--config", creds_basic, "projects"])
check("23 projects", r.returncode == 0 and "STRUCTURING" in r.stdout, r.stdout + r.stderr)

r = run(["--config", creds_basic, "fields", "--query", "影响"])
check("24 fields filter", r.returncode == 0 and "customfield_13209" in r.stdout and "Summary" not in r.stdout,
      r.stdout + r.stderr)

r = run(["--config", creds_basic, "--insecure", "--timeout", "30", "whoami"])
check("25 global flags", r.returncode == 0 and "身份:" in r.stdout, r.stdout + r.stderr)

r = run(["--config", creds_basic, "start", "T-1", "--dry-run"])
check("26 start dry-run", r.returncode == 0 and "[DRY-RUN]" in r.stdout, r.stdout + r.stderr)

# ---------- v2.1: init / use / 活动项目 ----------

newcfg = os.path.join(BASE_TMP, "init-test.yaml")

r = run(["--config", os.path.join(BASE_TMP, "none.yaml"), "init", "--check"])
check("27 init check missing", r.returncode == 1 and "未初始化" in r.stdout, r.stdout + r.stderr)

r = run(["--config", newcfg, "init", "--base-url", BASE, "--username", "tester",
         "--password", "pw123", "--projects", "T,STRUCTURING", "--use", "STRUCTURING"])
txt = read_cfg(newcfg) if os.path.exists(newcfg) else ""
check("28 init create (non-interactive)",
      r.returncode == 0 and "[验证] 身份: 测试员甲" in r.stdout and "已登记项目(2): T, STRUCTURING" in r.stdout
      and "projects: T,STRUCTURING" in txt and "active_project: STRUCTURING" in txt and "password: pw123" in txt,
      f"rc={r.returncode}\n{r.stdout}\n{r.stderr}\n--- cfg ---\n{txt}")

r = run(["--config", newcfg, "init", "--check"])
check("28b init check ok", r.returncode == 0 and "已初始化 ✓" in r.stdout and "当前活动项目: STRUCTURING" in r.stdout,
      r.stdout + r.stderr)

r = run(["--config", newcfg, "whoami"])
check("29 whoami active", r.returncode == 0 and "当前活动项目: STRUCTURING" in r.stdout, r.stdout + r.stderr)

r = run(["--config", newcfg, "use"])
check("30 use list", r.returncode == 0 and "当前活动项目: STRUCTURING" in r.stdout
      and "T" in r.stdout and "STRUCTURING" in r.stdout, r.stdout + r.stderr)

r = run(["--config", newcfg, "use", "1"])
txt = read_cfg(newcfg)
check("30b use by number", r.returncode == 0 and "已切换当前活动项目: T" in r.stdout
      and "active_project: T" in txt, f"rc={r.returncode}\n{r.stdout}\n{txt}")

r = run(["--config", newcfg, "use", "struc"])
txt = read_cfg(newcfg)
check("30c use by prefix", r.returncode == 0 and "已切换当前活动项目: STRUCTURING" in r.stdout
      and "active_project: STRUCTURING" in txt, f"rc={r.returncode}\n{r.stdout}\n{txt}")

r = run(["--config", newcfg, "use", "nope"])
check("30d use invalid", r.returncode == 1 and "不在已登记列表" in r.stderr, r.stdout + r.stderr)

state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"])
jql = last_search_jql()
check("31 search default = active project",
      r.returncode == 0 and "[项目] 活动项目: STRUCTURING" in r.stdout
      and jql == "project = STRUCTURING AND resolution = Unresolved AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

state["requests"].clear()
r = run(["--config", newcfg, "search", "--project", "ALL", "--max", "2"])
jql = last_search_jql()
check("32 search --project ALL", r.returncode == 0
      and jql == "resolution = Unresolved AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

noact = os.path.join(BASE_TMP, "noactive.yaml")
with open(noact, "w", encoding="utf-8") as fp:
    fp.write(f"base_url: {BASE}\nusername: tester\npassword: pw123\n")
r = run(["--config", noact, "search"])
check("33 search no active", r.returncode == 1 and "未设置活动项目" in r.stderr, r.stdout + r.stderr)

state["requests"].clear()
r = run(["--config", newcfg, "issue", "1"])
paths = [x[1] for x in state["requests"]]
check("34 bare number key", r.returncode == 0 and any("/rest/api/2/issue/STRUCTURING-1" in p for p in paths)
      and "[提示]" not in r.stderr, f"rc={r.returncode} paths={paths}\n{r.stderr}")

r = run(["--config", newcfg, "issue", "T-1"])
check("35 cross-project hint", r.returncode == 0 and "[提示]" in r.stderr
      and "当前活动项目是 STRUCTURING" in r.stderr, r.stdout + r.stderr)

r = run(["--config", newcfg, "use", "BPM"])
txt = read_cfg(newcfg)
check("36 use auto-register", r.returncode == 0 and "已切换当前活动项目: BPM" in r.stdout
      and "T,STRUCTURING,BPM" in txt, f"rc={r.returncode}\n{r.stdout}\n{txt}")

r = run(["--config", creds_basic, "start", "W-1", "--dry-run"])
check("37 start guard on non-todo", r.returncode == 1 and "已不是「未开始」" in r.stderr, r.stdout + r.stderr)

# ---------- v2.2: 项目级配置 / 搜索硬护栏 ----------

r = run(["--config", newcfg, "use", "STRUCTURING"])
check("38 use back STRUCTURING", r.returncode == 0 and "已切换当前活动项目: STRUCTURING" in r.stdout, r.stdout + r.stderr)

tpl_custom = "project = {project} AND status = Working AND assignee in (currentUser()) order by updated DESC"
r = run(["--config", newcfg, "pconfig", "STRUCTURING", "--default-jql", tpl_custom])
pj = os.path.join(PROJCFG, "STRUCTURING.yaml")
check("39 pconfig set", r.returncode == 0 and os.path.exists(pj) and tpl_custom in open(pj, encoding="utf-8").read(),
      f"rc={r.returncode}\n{r.stdout}{r.stderr}")

r = run(["--config", newcfg, "pconfig"])
check("40a pconfig list", r.returncode == 0 and "STRUCTURING.yaml" in r.stdout and "使用 STRUCTURING.yaml" in r.stdout,
      r.stdout + r.stderr)
r = run(["--config", newcfg, "pconfig", "BPM"])
check("40b pconfig show unset", r.returncode == 0 and "（未配置）" in r.stdout, r.stdout + r.stderr)

state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"])
jql = last_search_jql()
check("41 search uses project config",
      r.returncode == 0 and "模板: 项目配置" in r.stdout
      and jql == "project = STRUCTURING AND status = Working AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

with open(newcfg, "a", encoding="utf-8") as fp:
    fp.write("default_jql: project = {project} AND status != 未开始 AND assignee in (currentUser()) order by updated DESC\n")
r = run(["--config", newcfg, "use", "BPM"])
check("42a use BPM", r.returncode == 0, r.stdout + r.stderr)
state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"])
jql = last_search_jql()
check("42b search uses global config", r.returncode == 0 and "模板: 全局配置" in r.stdout
      and jql == "project = BPM AND status != 未开始 AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

r = run(["--config", newcfg, "search", "--jql", "resolution = Unresolved AND assignee in (currentUser())"])
check("43a guard blocks cross-project jql", r.returncode == 1 and "未限定项目" in r.stderr, r.stdout + r.stderr)
r = run(["--config", newcfg, "search", "--jql", "resolution = Unresolved AND assignee in (currentUser())", "--max", "2", "--all-projects"])
check("43b --all-projects allows", r.returncode == 0, r.stdout + r.stderr)

r = run(["--config", newcfg, "search", "--jql", "project = STRUCTURING AND resolution = Unresolved", "--max", "2"])
check("44a hint other project", r.returncode == 0 and "[提示]" in r.stderr and "当前活动项目" in r.stderr, r.stdout + r.stderr)
r = run(["--config", newcfg, "search", "--jql", "project = BPM AND resolution = Unresolved", "--max", "2"])
check("44b no hint own project", r.returncode == 0 and "[提示]" not in r.stderr, r.stdout + r.stderr)

plain = os.path.join(BASE_TMP, "plain.yaml")
with open(plain, "w", encoding="utf-8") as fp:
    fp.write(f"base_url: {BASE}\nusername: tester\npassword: pw123\nactive_project: T\n")
state["requests"].clear()
r = run(["--config", plain, "search", "--max", "2"])
jql = last_search_jql()
check("45 builtin fallback", r.returncode == 0 and "模板: 内置默认" in r.stdout
      and jql == "project = T AND resolution = Unresolved AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

r = run(["--config", newcfg, "pconfig", "STRUCTURING", "--clear"])
check("46 pconfig clear", r.returncode == 0 and "已删除项目配置" in r.stdout and not os.path.exists(pj),
      r.stdout + r.stderr)

# ---------- v2.3: 项目文件夹配置（就近发现 / pconfig --here） ----------

NB_DIR = os.path.join(BASE_TMP, "proj-folder")
NB_SUB = os.path.join(NB_DIR, "src", "deep")
os.makedirs(NB_SUB, exist_ok=True)
tpl_nb = "project = {project} AND status = Working order by updated DESC"
with open(os.path.join(NB_DIR, ".jira-project.yaml"), "w", encoding="utf-8") as fp:
    fp.write(f"project: T\ndefault_jql: {tpl_nb}\n")

state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"], {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_DIR)
jql = last_search_jql()
check("47 folder cfg used (project+template)",
      r.returncode == 0 and "[项目] 项目文件夹: T" in r.stdout and "模板: 项目文件夹" in r.stdout
      and jql == "project = T AND status = Working order by updated DESC"
      and "工作目录启用了项目文件夹配置" in r.stderr,
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"], {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_SUB)
jql = last_search_jql()
check("48 folder cfg walked up from subdir",
      r.returncode == 0 and "[项目] 项目文件夹: T" in r.stdout
      and jql == "project = T AND status = Working order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

state["requests"].clear()
r = run(["--config", newcfg, "search", "--project", "STRUCTURING", "--max", "2"],
        {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_DIR)
jql = last_search_jql()
check("49 folder cfg skipped on project mismatch",
      r.returncode == 0 and "[项目] 指定项目: STRUCTURING" in r.stdout
      and jql == "project = STRUCTURING AND status != 未开始 AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

NB_EMPTY = os.path.join(BASE_TMP, "proj-empty")
os.makedirs(NB_EMPTY, exist_ok=True)
r = run(["--config", newcfg, "pconfig", "--here", "--set-project", "T", "--default-jql", tpl_nb],
        {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_EMPTY)
nbf = os.path.join(NB_EMPTY, ".jira-project.yaml")
check("50 pconfig --here write",
      r.returncode == 0 and os.path.exists(nbf) and "project: T" in read_cfg(nbf) and tpl_nb in read_cfg(nbf),
      f"rc={r.returncode}\n{r.stdout}{r.stderr}")
r = run(["--config", newcfg, "pconfig", "--here"], {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_EMPTY)
check("51 pconfig --here view",
      r.returncode == 0 and "项目文件夹配置（就近）" in r.stdout and "project: T" in r.stdout,
      r.stdout + r.stderr)
r = run(["--config", newcfg, "pconfig", "--here", "--clear"], {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_EMPTY)
check("52 pconfig --here clear", r.returncode == 0 and not os.path.exists(nbf), r.stdout + r.stderr)

NB_ELSE = os.path.join(BASE_TMP, "proj-else")
os.makedirs(NB_ELSE, exist_ok=True)
elsef = os.path.join(NB_ELSE, "custom-nb.yaml")
with open(elsef, "w", encoding="utf-8") as fp:
    fp.write("project: STRUCTURING\ndefault_jql: project = {project} AND priority = High order by updated DESC\n")
state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"],
        {"JIRA_NO_FOLDER_CFG": "", "JIRA_PROJECT_CONFIG_PATH": elsef}, cwd=NB_EMPTY)
jql = last_search_jql()
check("53 JIRA_PROJECT_CONFIG_PATH override",
      r.returncode == 0 and jql == "project = STRUCTURING AND priority = High order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

state["requests"].clear()
r = run(["--config", newcfg, "search", "--max", "2"], cwd=NB_DIR)   # run() 默认带 JIRA_NO_FOLDER_CFG=1
jql = last_search_jql()
check("54 NO_FOLDER_CFG disables",
      r.returncode == 0 and "[项目] 活动项目: BPM" in r.stdout
      and jql == "project = BPM AND status != 未开始 AND assignee in (currentUser()) order by updated DESC",
      f"rc={r.returncode} jql={jql}\n{r.stdout}\n{r.stderr}")

state["requests"].clear()
r = run(["--config", newcfg, "issue", "1"], {"JIRA_NO_FOLDER_CFG": ""}, cwd=NB_DIR)
paths = [x[1] for x in state["requests"]]
check("55 bare number uses folder project",
      r.returncode == 0 and any("/rest/api/2/issue/T-1" in p for p in paths),
      f"rc={r.returncode} paths={paths}\n{r.stdout}{r.stderr}")

failed = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(failed)}/{len(results)} PASS =====")
if failed:
    print("FAILED: " + ", ".join(failed))
    sys.exit(1)
