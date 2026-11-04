#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jira.py — JIRA REST API CLI（纯数据流、零浏览器、零第三方依赖，单文件）。

适用: JIRA Server / Data Center 8.x（HTTP Basic）为主；可选 Bearer token（Server PAT）。
      Jira Cloud 不在本版本支持范围，使用说明见 references/usage.md。

认证: 凭据文件（flat yaml），默认 <技能目录>/config/jira-api-creds.yaml
      （各平台一致；环境变量 JIRA_CREDS_PATH 可覆盖）。
      多实例/多账号: --profile <名字> 读 profiles 目录 <名字>.yaml
      （默认 <技能目录>/config/profiles/，JIRA_PROFILES_DIR 可覆盖）。
      键: base_url / username / password，可选 token（Bearer）/ insecure / timeout /
          projects（登记的项目）/ active_project（当前活动项目）/ default_jql（可选）。

子命令: init / use / pconfig（初始化、活动项目、项目级配置）/ search / issue /
        changelog（变更历史）/ create（建单）/ transitions / editmeta / start /
        resolve / assign / comment / whoami / projects / fields /
        attachments（下载 / 列表 / 上传）
写操作（start/resolve/assign/comment/create/附件上传）均支持 --dry-run 预演，执行后自动读回验证。
不带 --jql/--url/--project 的 search 默认聚焦当前上下文项目：
     项目文件夹配置（就近 .jira-project.yaml，pconfig --here 管理）> 活动项目（use 切换）。
未限定 project 的 --jql/--url 查询默认被拦截（确要跨项目需显式 --all-projects）。
拉单模板优先级：项目文件夹配置 > 项目配置（config/projects/<KEY>.yaml）>
     全局配置（default_jql）> 内置默认。纯数字单号（如 issue 25）自动补上下文项目前缀。
每个子命令 --help 有详细用法。错误退出码 1，正常 0。
"""
import contextlib
import hashlib
import uuid
import argparse
import base64
import getpass
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse, parse_qs

PROG = os.path.basename(sys.argv[0])
DEFAULT_BASE_URL = "https://ticket.quectel.com"
VERSION = "2.5.2"

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

def _package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_creds_path():
    return os.path.join(_package_root(), "config", "jira-api-creds.yaml")


def _profiles_dir():
    return os.environ.get("JIRA_PROFILES_DIR") or os.path.join(_package_root(), "config", "profiles")


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


def _plugin_env():
    """Read only the plugin's documented settings, never the CLI's SSO token."""
    names = ("JIRA_URL", "JIRA_TOKEN", "JIRA_TOKEN_STORE")
    values = {name: os.environ[name] for name in names if name in os.environ}
    if os.name == "nt" and len(values) < len(names):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                for name in names:
                    if name not in values:
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                            if isinstance(value, str):
                                values[name] = value
                        except FileNotFoundError:
                            pass
        except FileNotFoundError:
            pass
    return values


def _decrypt_plugin_password(key, nonce, encrypted, user):
    """Authenticated AES-GCM via the OS library; no extra Python dependency."""
    import ctypes as ct
    if len(nonce) != 12 or len(encrypted) < 16:
        raise ValueError("JIRA 插件授权内容不完整，请在 CLI 中重新授权")
    aad = user.encode("utf-8")
    ciphertext, tag = encrypted[:-16], encrypted[-16:]
    if os.name == "nt":
        from ctypes import wintypes as wt

        class AuthInfo(ct.Structure):
            _fields_ = [("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
                        ("pbNonce", ct.c_void_p), ("cbNonce", wt.ULONG),
                        ("pbAuthData", ct.c_void_p), ("cbAuthData", wt.ULONG),
                        ("pbTag", ct.c_void_p), ("cbTag", wt.ULONG),
                        ("pbMacContext", ct.c_void_p), ("cbMacContext", wt.ULONG),
                        ("cbAAD", wt.ULONG), ("cbData", ct.c_ulonglong),
                        ("dwFlags", wt.ULONG)]

        lib = ct.WinDLL("bcrypt")
        signatures = {
            "BCryptOpenAlgorithmProvider": [ct.POINTER(ct.c_void_p), ct.c_wchar_p, ct.c_wchar_p, wt.ULONG],
            "BCryptSetProperty": [ct.c_void_p, ct.c_wchar_p, ct.c_void_p, wt.ULONG, wt.ULONG],
            "BCryptGenerateSymmetricKey": [ct.c_void_p, ct.POINTER(ct.c_void_p), ct.c_void_p,
                                           wt.ULONG, ct.c_void_p, wt.ULONG, wt.ULONG],
            "BCryptDecrypt": [ct.c_void_p, ct.c_void_p, wt.ULONG, ct.POINTER(AuthInfo), ct.c_void_p,
                              wt.ULONG, ct.c_void_p, wt.ULONG, ct.POINTER(wt.ULONG), wt.ULONG],
            "BCryptDestroyKey": [ct.c_void_p],
            "BCryptCloseAlgorithmProvider": [ct.c_void_p, wt.ULONG],
        }
        for name, argtypes in signatures.items():
            getattr(lib, name).argtypes = argtypes
            getattr(lib, name).restype = ct.c_long

        def checked(status):
            if status != 0:
                raise ValueError("JIRA 插件授权校验失败，请在 CLI 中重新授权")

        algorithm, handle = ct.c_void_p(), ct.c_void_p()
        output = ct.create_string_buffer(max(1, len(ciphertext)))
        try:
            checked(lib.BCryptOpenAlgorithmProvider(ct.byref(algorithm), "AES", None, 0))
            mode = ct.create_unicode_buffer("ChainingModeGCM")
            checked(lib.BCryptSetProperty(algorithm, "ChainingMode", mode, ct.sizeof(mode), 0))
            secret = ct.create_string_buffer(key)
            checked(lib.BCryptGenerateSymmetricKey(algorithm, ct.byref(handle), None, 0,
                                                  secret, len(key), 0))
            iv, auth, mac = (ct.create_string_buffer(value) for value in (nonce, aad, tag))
            info = AuthInfo()
            info.cbSize, info.dwInfoVersion = ct.sizeof(info), 1
            info.pbNonce, info.cbNonce = ct.addressof(iv), len(nonce)
            info.pbAuthData, info.cbAuthData = ct.addressof(auth), len(aad)
            info.pbTag, info.cbTag = ct.addressof(mac), len(tag)
            source = ct.create_string_buffer(ciphertext)
            length = wt.ULONG()
            checked(lib.BCryptDecrypt(handle, source, len(ciphertext), ct.byref(info), None, 0,
                                      output, len(ciphertext), ct.byref(length), 0))
            return output.raw[:length.value].decode("utf-8")
        finally:
            ct.memset(output, 0, ct.sizeof(output))
            if handle.value:
                lib.BCryptDestroyKey(handle)
            if algorithm.value:
                lib.BCryptCloseAlgorithmProvider(algorithm, 0)
    else:
        import ctypes.util
        library = ctypes.util.find_library("crypto")
        if not library:
            raise ValueError("本机缺少系统加密库，无法复用插件授权；可使用原有账号配置")
        lib = ct.CDLL(library)
        signatures = {
            "EVP_CIPHER_CTX_new": ([], ct.c_void_p),
            "EVP_CIPHER_CTX_free": ([ct.c_void_p], None),
            "EVP_aes_256_gcm": ([], ct.c_void_p),
            "EVP_DecryptInit_ex": ([ct.c_void_p] * 5, ct.c_int),
            "EVP_DecryptUpdate": ([ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_int), ct.c_void_p, ct.c_int], ct.c_int),
            "EVP_CIPHER_CTX_ctrl": ([ct.c_void_p, ct.c_int, ct.c_int, ct.c_void_p], ct.c_int),
            "EVP_DecryptFinal_ex": ([ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_int)], ct.c_int),
        }
        for name, (argtypes, restype) in signatures.items():
            getattr(lib, name).argtypes, getattr(lib, name).restype = argtypes, restype
        context = lib.EVP_CIPHER_CTX_new()
        if not context:
            raise ValueError("无法创建系统加密上下文")
        output = ct.create_string_buffer(len(ciphertext) + 16)
        try:
            n, final = ct.c_int(), ct.c_int()
            if (lib.EVP_DecryptInit_ex(context, lib.EVP_aes_256_gcm(), None, key, nonce) != 1
                    or lib.EVP_DecryptUpdate(context, None, ct.byref(n), aad, len(aad)) != 1
                    or lib.EVP_DecryptUpdate(context, output, ct.byref(n), ciphertext, len(ciphertext)) != 1
                    or lib.EVP_CIPHER_CTX_ctrl(context, 0x11, len(tag), tag) != 1
                    or lib.EVP_DecryptFinal_ex(context, ct.byref(output, n.value), ct.byref(final)) != 1):
                raise ValueError("JIRA 插件授权校验失败，请在 CLI 中重新授权")
            return output.raw[:n.value + final.value].decode("utf-8")
        finally:
            ct.memset(output, 0, ct.sizeof(output))
            lib.EVP_CIPHER_CTX_free(context)


def _plugin_credentials(base):
    """Resolve the selected local JIRA token; never copy it to the skill config."""
    from datetime import datetime, timezone
    env = _plugin_env()
    token = env.get("JIRA_TOKEN", "")
    if not token:
        return None
    if (env.get("JIRA_URL") or DEFAULT_BASE_URL).rstrip("/") != base.rstrip("/"):
        raise ValueError("JIRA 插件授权属于另一个地址，已停止复用；请为当前实例配置账号")
    if token != token.strip() or token.startswith("JIRA_TOKEN="):
        raise ValueError("JIRA 插件授权格式不正确，请在 CLI 中重新配置")
    path = env.get("JIRA_TOKEN_STORE") or os.path.join(os.path.expanduser("~"), ".quectel-cli", "jira", "tokens.json")
    try:
        with open(path, encoding="utf-8") as file:
            store = json.load(file)
        if store.get("version") != 1 or not isinstance(store.get("tokens"), dict):
            raise ValueError()
        record = store["tokens"].get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in ("user", "nonce", "password")):
            raise ValueError()
        expiry = record.get("expires_at", "")
        if not isinstance(expiry, str) or not expiry:
            raise ValueError()
        if expiry and expiry != "never":
            expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError()
            if expires <= datetime.now(timezone.utc):
                raise ValueError("JIRA 插件授权已过期，请在 CLI 中重新授权；公司 SSO 登录不能代替它")
        cipher = record.get("cipher") or "legacy-v1"
        if cipher not in ("bound-v2", "legacy-v1"):
            raise ValueError("当前 JIRA 插件授权格式暂不支持，请使用原有账号配置")
        material = token + (":quectel-cli-jira::token::20260729::v2" if cipher == "bound-v2" else "")
        key = hashlib.sha256(material.encode("utf-8")).digest()
        def decode(value):
            return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        password = _decrypt_plugin_password(key, decode(record["nonce"]), decode(record["password"]), record["user"])
        if not record["user"] or not password:
            raise ValueError()
        return {"user": record["user"], "pw": password, "token": "", "auth_source": "quectel-cli"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, ValueError) and str(error).startswith(("JIRA 插件", "当前 JIRA", "本机缺少", "无法创建")):
            raise
        raise ValueError("JIRA 插件授权无法读取或校验，请在 CLI 中重新授权") from None


def load_creds(path):
    if not os.path.exists(path):
        die(f"凭据文件不存在: {path}\n请先运行初始化向导: {PROG} init"
            f"（账号 → 验证 → 登记项目 → 设活动项目）；多实例可用 --profile（见 docs/01）。")
    creds = _read_flat(path)
    base = (creds.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    auth = {"user": creds.get("username", ""), "pw": creds.get("password", ""),
            "token": creds.get("token", ""), "auth_source": "config"}
    if not any((auth["user"], auth["pw"], auth["token"])):
        try:
            plugin = _plugin_credentials(base)
        except ValueError as error:
            die(str(error))
        if plugin:
            auth = plugin
        elif creds.get("auth_source") == "quectel-cli":
            die("未找到 JIRA 插件授权，请先在 CLI 中完成 JIRA 授权")
    if not (auth["token"] or (auth["user"] and auth["pw"])):
        die(f"凭据文件 {path} 缺字段: username+password（或 token）；可运行 {PROG} init 补齐。")
    timeout = 60
    if creds.get("timeout"):
        try:
            timeout = int(float(creds["timeout"]))
        except ValueError:
            print(f"[警告] timeout 值无法解析: {creds['timeout']}，改用默认 60", file=sys.stderr)
    projects = [s.strip() for s in (creds.get("projects") or "").split(",") if s.strip()]
    return {
        "base": base,
        **auth,
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


def _http_origin(url):
    """Origin equality includes scheme, host and the effective port."""
    parsed = urlparse(url)
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname, port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() not in ("GET", "HEAD"):
            raise urllib.error.HTTPError(req.full_url, code, "Write redirect requires read-back", headers, fp)
        if urlparse(req.full_url).scheme == "https" and urlparse(newurl).scheme != "https":
            raise urllib.error.HTTPError(req.full_url, code, "Blocked HTTPS downgrade", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _http_origin(req.full_url) != _http_origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


def _http_open(req, data, timeout, context):
    """Use a scoped opener; this CLI is serial, and urlopen stays injectable."""
    previous = urllib.request._opener
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _SafeRedirectHandler())
    try:
        urllib.request.install_opener(opener)
        return urllib.request.urlopen(req, data, timeout=timeout)
    finally:
        urllib.request.install_opener(previous)


def _request(method, path, params=None, body=None, raw=False, raw_body=None, headers=None):
    """带认证的请求；仅 GET/HEAD 自动重试，写入异常先核对、不盲重发。

    path 以 http(s):// 开头时按绝对 URL 用（附件下载等场景），否则拼接 base_url。
    raw_body/headers 用于非 JSON 请求体（附件上传 multipart）。
    """
    url = path if path.startswith(("http://", "https://")) else C["base"] + path
    if urlparse(C["base"]).scheme == "https" and urlparse(url).scheme != "https":
        die("禁止将 HTTPS 请求降级到非 HTTPS 地址")
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    req = urllib.request.Request(url, method=method)
    if _http_origin(url) == _http_origin(C["base"]):
        if C.get("token"):
            req.add_header("Authorization", "Bearer " + C["token"])
        else:
            token = base64.b64encode(f"{C['user']}:{C['pw']}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    data = None
    hdrs = dict(headers or {})
    if raw_body is not None:
        data = raw_body
        req.add_header("Content-Type", hdrs.pop("Content-Type", None)
                       or hdrs.pop("content-type", None) or "application/octet-stream")
    elif body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    for hk, hv in hdrs.items():
        if hk.lower() != "content-type":
            req.add_header(hk, hv)
    if _http_origin(url) != _http_origin(C["base"]):
        req.remove_header("Authorization")

    ctx_ok, ctx_no = _contexts()
    ctxs = [ctx_no] if C.get("insecure") else [ctx_ok]
    if C.get("insecure"):
        print("[警告] 已显式关闭证书校验；请仅在批准的临时排障中使用，完成后恢复校验。", file=sys.stderr)
    total = 3  # 1 次 + 最多 2 次重试（仅限 429/5xx/网络类）
    last_msg = None
    for attempt in range(total):
        wait = None
        for idx, ctx in enumerate(ctxs):
            try:
                with _http_open(req, data, timeout=C["timeout"], context=ctx) as r:
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
                finally:
                    e.close()
                if e.code in (401, 403):
                    die(f"认证失败 HTTP {e.code}: {detail or '检查凭据文件或账号被 SSO 锁'}"
                        f"（凭据: {os.path.basename(C['path'])}）")
                # Only explicit request rejection makes a write safe to retry.
                # Gateways can return any 5xx (including 520) after upstream commit;
                # timeouts, throttling and unrecognised statuses are also ambiguous.
                rejected = e.code in (400, 401, 403, 404, 405, 409, 410, 413, 414, 415, 422)
                if method not in ("GET", "HEAD") and not rejected:
                    raise WriteUncertain(f"JIRA 写入响应 HTTP {e.code}，需要先读回确认结果")
                if e.code in (429, 500, 502, 503, 504):
                    last_msg = f"HTTP {e.code} {method} {path}: {detail or e.reason}"
                    wait = _retry_wait(e)
                    break  # → 重试
                die(f"HTTP {e.code} {method} {path}: {detail or e.reason}")
            except (ssl.SSLError, urllib.error.URLError, OSError) as e:
                if _is_cert_err(e):
                    die("证书校验失败，已停止连接；请安装可信公司 CA 或设置 SSL_CERT_FILE，不会自动关闭校验。")
                if method not in ("GET", "HEAD"):
                    raise WriteUncertain("JIRA 写入连接中断，需要先读回确认结果")
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
    """单号参数：纯数字时自动补上下文项目前缀（项目文件夹配置 > 活动项目，如 25 → STRUCTURING-25）。"""
    k = str(raw or "").strip()
    if re.fullmatch(r"\d+", k):
        ctx, _ = _ctx_project()
        if ctx:
            return f"{ctx}-{k}"
        die(f"「{k}」是纯数字：先设置活动项目（{PROG} use <项目>）、或在项目文件夹放置配置，"
            f"或给完整单号（如 STRUCTURING-{k}）")
    return k


def _warn_project(key):
    """操作的单不属于当前上下文项目（项目文件夹配置 > 活动项目）时给出提醒（不拦截）。"""
    ctx, is_folder = _ctx_project()
    proj = key.split("-", 1)[0] if "-" in key else ""
    if ctx and proj and proj.upper() != ctx.upper():
        if is_folder:
            print(f"[提示] {key} 属于项目 {proj}，当前目录默认是项目 {ctx}"
                  f"（{os.path.basename(_nearby_cfg()['_path'])} 声明；操作其它项目的单请确认无误）", file=sys.stderr)
        else:
            print(f"[提示] {key} 属于项目 {proj}，当前活动项目是 {ctx}"
                  f"（如需切换: {PROG} use {proj}）", file=sys.stderr)


def _project_cfg_dir():
    return os.environ.get("JIRA_PROJECT_CONFIGS_DIR") or os.path.join(_package_root(), "config", "projects")


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


# ---------------- 项目文件夹配置（就近发现，v2.3） ----------------

_NEARBY_NAMES = (".jira-project.yaml", ".jira-project.yml", "jira-project.yaml", "jira-project.yml")
_nearby = None


def _nearby_cfg_path():
    """从当前工作目录向上查找项目文件夹配置；JIRA_PROJECT_CONFIG_PATH 可显式指定，JIRA_NO_FOLDER_CFG=1 可禁用。"""
    env = os.environ.get("JIRA_PROJECT_CONFIG_PATH")
    if env:
        if os.path.isfile(env):
            return env
        print(f"[警告] JIRA_PROJECT_CONFIG_PATH 指向的文件不存在: {env}", file=sys.stderr)
        return None
    if os.environ.get("JIRA_NO_FOLDER_CFG"):
        return None
    d = os.path.abspath(os.getcwd())
    while True:
        for name in _NEARBY_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _nearby_cfg():
    """读取项目文件夹配置（进程内缓存；返回 dict，含 _path/_dir；未找到返回空 dict）。"""
    global _nearby
    if _nearby is None:
        p = _nearby_cfg_path()
        cfg = {}
        if p:
            try:
                cfg = _read_flat(p)
            except OSError as e:
                print(f"[警告] 项目文件夹配置读取失败: {p}（{e}）", file=sys.stderr)
                cfg = {}
            cfg["_path"] = p
            cfg["_dir"] = os.path.dirname(p)
            cfg["project"] = (cfg.get("project") or "").strip().upper()
        _nearby = cfg
    return _nearby


def _ctx_project():
    """当前上下文项目（项目文件夹配置 > 活动项目），返回 (project, is_folder)。"""
    nb = _nearby_cfg()
    if nb.get("project"):
        return nb["project"], True
    return (C.get("active") or "").strip(), False


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
    """生成默认 JQL，返回 (jql, 模板来源)。模板优先级：项目文件夹 > 项目配置 > 全局配置 > 内置默认。"""
    all_set = project in ("ALL", "*", "全部")
    nb = _nearby_cfg()
    nb_proj = (nb.get("project") or "").upper()
    use_nb = bool(nb.get("default_jql")) and (not nb_proj or (not all_set and nb_proj == str(project).upper()))
    if use_nb:
        tpl, src = nb["default_jql"], f"项目文件夹 {nb['_path']}"
    else:
        pcfg, ppath = ({}, None)
        if not all_set:
            pcfg, ppath = _load_project_cfg(project)
        if pcfg.get("default_jql"):
            tpl, src = pcfg["default_jql"], f"项目配置 {os.path.basename(ppath)}"
        elif C.get("default_jql"):
            tpl, src = C["default_jql"], "全局配置"
        else:
            tpl, src = DEFAULT_JQL, "内置默认"
    if all_set:
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
        nb = _nearby_cfg()
        nb_proj = (nb.get("project") or "").strip().upper()
        proj = (args.project or nb_proj or C.get("active") or "").strip()
        if proj and proj.upper() not in ("ALL", "*", "全部"):
            proj = proj.upper()
        if not proj:
            die(f"未给查询条件且未设置活动项目：先用 {PROG} use <项目> 设置（或 --jql/--url/--project 显式指定；"
                f"--project ALL=不限项目；也可在项目文件夹放 .jira-project.yaml，见 {PROG} pconfig --here）")
        jql, tpl_src = _build_default_jql(proj)
        label = "全部" if proj in ("ALL", "*", "全部") else proj
        if args.project:
            src = "指定项目"
        elif nb_proj and nb_proj == proj:
            src = "项目文件夹"
        else:
            src = "活动项目"
        note = f"[项目] {src}: {label}（模板: {tpl_src}）；JQL: {jql}"
        print(note, file=(sys.stderr if args.format == "json" else sys.stdout))
        act0 = (C.get("active") or "").strip().upper()
        if src == "项目文件夹" and act0 and act0 != proj:
            print(f"[提示] 工作目录启用了项目文件夹配置（{nb['_path']}）→ 默认查 {proj}；"
                  f"活动项目是 {C.get('active')}（离开该目录后恢复）", file=sys.stderr)
    if explicit:
        # 防手滑/防 AI 跑偏：未限定项目的查询默认拦截；指向别的项目时给提示
        ctx, is_folder = _ctx_project()
        if ctx and ctx.upper() not in ("ALL", "*", "全部") and not args.all_projects:
            desc = (f"当前目录所属项目是 {ctx}（项目文件夹配置声明）" if is_folder
                    else f"当前活动项目是 {ctx}")
            if not re.search(r"project\s*(?:=|!=|\bin\b)", jql, flags=re.I):
                die(f"该查询未限定项目，会跨【全部项目】拉单。{desc}：\n"
                    f"  · 只查当前项目：直接运行不带条件的 search，或把 project = {ctx} 写进 JQL\n"
                    f"  · 确实要跨项目搜索：显式加 --all-projects")
            if ctx.upper() not in jql.upper():
                if is_folder:
                    print(f"[提示] 查询未指向当前目录所属项目 {ctx}（.jira-project.yaml 声明）——"
                          f"若只是查其它项目可用 --all-projects 消除本提示", file=sys.stderr)
                else:
                    print(f"[提示] 查询未指向当前活动项目 {ctx}——若只是想查其它项目，先 {PROG} use <项目> 切换；"
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


# ---------------- changelog（变更历史） ----------------

def cmd_changelog(args):
    key = _key_arg(args.key)
    _warn_project(key)
    _, d = _request("GET", f"/rest/api/2/issue/{key}",
                    params={"expand": "changelog", "fields": "summary"})
    ch = d.get("changelog") or {}
    hist = ch.get("histories") or []
    if args.format == "json":
        print(json.dumps({"key": d.get("key", key), "total": ch.get("total", len(hist)),
                          "histories": hist}, ensure_ascii=False, indent=1))
        return
    summary = ((d.get("fields") or {}).get("summary") or "")
    print(f"# {d.get('key', key)} 变更历史（{ch.get('total', len(hist))} 条）: {summary[:60]}")
    if not hist:
        print("（无变更记录——从没被改过的单）")
        return
    for i, h in enumerate(hist, 1):
        who = (h.get("author") or {}).get("displayName", "-")
        when = (h.get("created") or "").replace("T", " ")[:19]
        print(f"{i:>3}. {when}  {who}")
        for it in h.get("items") or []:
            fld = it.get("field", "?")
            fr = it.get("fromString")
            to = it.get("toString")
            if fr is None and "from" in it:
                fr = it.get("from")
            if to is None and "to" in it:
                to = it.get("to")
            print(f"     {fld}: {fr or '（空）'} -> {to or '（空）'}")
    print(f"（共 {len(hist)} 条变更；状态流转/字段改动/经办人变化都会在这里出现）")


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
    result = _safe_start(_key_arg(args.key), args.dry_run, args.transition_id)
    print(('[DRY-RUN] 预演（未写入）: ' if args.dry_run else '接受结果: ') + json.dumps(result, ensure_ascii=False))


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


def _pick_assignee(key, q):
    """解析目标经办人（assign / create --assign 共用）。me=当前账号；否则按显示名/登录名解析。"""
    q = (q or "").strip()
    if not q:
        die("人员不能为空（显示名/登录名，或 me 表示自己）")
    if q.lower() in ("me", "self", "我", "自己"):
        _, me = _request("GET", "/rest/api/2/myself")
        target = {"name": me.get("name"), "displayName": me.get("displayName")}
        if not target["name"]:
            die("无法获取当前用户名（/myself 未返回 name）")
        return target
    return _resolve_user(key, q)


def cmd_assign(args):
    key = _key_arg(args.key)
    _warn_project(key)
    target = _pick_assignee(key, args.to)
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
        with open(args.body_file, encoding='utf-8') as f:
            body = f.read()
    else:
        body = args.body
    result = _safe_comment(_key_arg(args.key), body, args.dry_run)
    print(('[DRY-RUN] 预演（未写入）: ' if args.dry_run else '评论结果: ') + json.dumps(result, ensure_ascii=False))


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
    nb = _nearby_cfg()
    if nb.get("_path"):
        nbtail = f"（project={nb['project']}）" if nb.get("project") else ""
        print(f"项目文件夹配置: {nb['_path']}{nbtail}")


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
    try:
        C = load_creds(path)
    except SystemExit:
        print("  状态: 未初始化(不完整) ✗ → 请补齐 JIRA 授权或账号配置")
        raise
    print(f"  实例: {C['base']}")
    print(f"  账号: {C.get('user') or '(token 认证)'}")
    if C.get("auth_source") == "quectel-cli":
        print("  授权: 复用本机 JIRA 插件")
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
    nb = _nearby_cfg()
    if nb.get("_path"):
        nbtail = f"（project={nb['project']}）" if nb.get("project") else ""
        print(f"  项目文件夹配置: {nb['_path']}{nbtail}")
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
    base = (args.base_url or creds.get("base_url") or DEFAULT_BASE_URL).strip()
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
    plugin = None
    if not any((user, pw, token)):
        try:
            plugin = _plugin_credentials(base)
        except ValueError as error:
            die(str(error))
        if plugin:
            print("[初始化] 复用本机 JIRA 插件授权，无需再次输入账号密码")
        elif creds.get("auth_source") == "quectel-cli":
            die("未找到 JIRA 插件授权，请先在 CLI 中完成 JIRA 授权")
    if not token and not plugin:
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
    if plugin:
        updates["auth_source"] = "quectel-cli"
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
    print("（注：若所在目录启用了项目文件夹配置 .jira-project.yaml，搜索默认以它为准）")


def cmd_pconfig(args):
    nb = _nearby_cfg()
    if getattr(args, "here", False):
        if args.project:
            die("pconfig <KEY> 管理的是 包内项目配置；--here 管理当前目录的 .jira-project.yaml，两者不要同时用")
        cwd_file = os.path.join(os.getcwd(), ".jira-project.yaml")
        if args.clear:
            if os.path.exists(cwd_file):
                os.remove(cwd_file)
                print(f"已删除项目文件夹配置: {cwd_file}")
            else:
                print(f"（当前目录没有 .jira-project.yaml，无需删除）: {cwd_file}")
            return
        ups = {}
        if getattr(args, "set_project", None):
            ups["project"] = args.set_project.strip().upper()
        if args.set_default_jql is not None:
            if "{project}" not in args.set_default_jql:
                die("模板里必须包含 {project} 占位符（例如: project = {project} AND status = \"Initial\" order by updated DESC）")
            ups["default_jql"] = args.set_default_jql
        if ups:
            if not os.path.exists(cwd_file):
                with open(cwd_file, "w", encoding="utf-8") as fp:
                    fp.write("# JIRA 项目文件夹配置（工具会从当前目录向上查找本文件；放在项目根目录即可）\n"
                             "# 优先级: 项目文件夹配置 > 包内项目配置(<KEY>.yaml) > 全局配置(default_jql) > 内置默认\n"
                             "# 键: project=JIRA项目KEY / default_jql=拉单模板(含{project}) / profile=多实例名(可选)\n")
            _update_cfg(cwd_file, ups)
            print(f"已写入项目文件夹配置: {cwd_file}")
            for k, v in ups.items():
                print(f"  {k}: {v}")
            anc = nb.get("_path")
            if anc and os.path.abspath(anc) != os.path.abspath(cwd_file):
                print(f"[提示] 上层目录已有配置文件 {anc}；本文件更靠近当前目录，优先生效")
            return
        if nb.get("_path"):
            print(f"项目文件夹配置（就近）: {nb['_path']}")
            with open(nb["_path"], encoding="utf-8") as fp:
                print(fp.read().strip())
        else:
            print("当前目录及上层未发现 .jira-project.yaml —— 创建示例:")
            print(f"  {PROG} pconfig --here --set-project STRUCTURING "
                  f"--default-jql 'project = {{project}} AND resolution = Unresolved order by updated DESC'")
        return
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
        print(f"（{proj} 的拉单将优先用该模板；优先级：项目文件夹 > 项目配置 > 全局配置 > 内置默认）")
        return
    if args.project:
        p = _project_cfg_path(proj)
        print(f"项目 {proj} 的项目配置: {p or '（未配置）'}")
        if p:
            with open(p, encoding="utf-8") as fp:
                print(fp.read().strip())
        g = C.get("default_jql")
        nbp = nb.get("_path")
        print(f"生效顺序: 项目文件夹（{'有: ' + nbp if nbp else '无'}）> 项目配置（{'有' if p else '无'}）"
              f"> 全局配置（{'有 default_jql' if g else '无'}）> 内置默认")
        print(f"内置默认: {DEFAULT_JQL}")
        return
    if nb.get("_path"):
        nbtail = f"（project={nb['project']}）" if nb.get("project") else ""
        print(f"项目文件夹配置（就近生效）: {nb['_path']}{nbtail}")
    else:
        print(f"项目文件夹配置: （当前目录及上层未启用；在项目根目录放 .jira-project.yaml 即可，见 {PROG} pconfig --here）")
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


# ---------------- 附件（下载 / 列表 / 上传） ----------------

def _build_multipart(field, filename, data, mime):
    """构造 multipart/form-data 请求体（纯标准库，无第三方依赖）。"""
    boundary = "----jira-py-" + os.urandom(8).hex()
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n")
    body = head.encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def _upload_attachments(key, args):
    results = [_safe_upload(key, f, args.dry_run) for f in args.upload]
    if args.comment:
        results.append(_safe_comment(key, args.comment, args.dry_run))
    print(('[DRY-RUN] 预演（未写入）: ' if args.dry_run else '附件结果: ') + json.dumps(results, ensure_ascii=False))


def cmd_attachments(args):
    key = _key_arg(args.key)
    _warn_project(key)
    if args.upload:
        if args.id or args.save_dir or args.list:
            die("--upload 与 --id / --save-dir / --list 不能同时用（上传模式不下载）")
        _upload_attachments(key, args)
        return
    if args.comment:
        die("--comment 仅配合 --upload 使用")
    if args.dry_run:
        die("--dry-run 仅配合 --upload 使用（下载 / --list 是只读操作）")
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
    if args.list:
        for a in atts:
            print(f"{a.get('id')}\t{a.get('filename')}\t{_human(a.get('size', 0))}\t"
                  f"{(a.get('author') or {}).get('displayName', '-')}\t{(a.get('created') or '')[:19]}")
        print(f"共 {len(atts)} 个附件（下载: {PROG} attachments {key}；上传: --upload <文件>）")
        return
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
    if t in ("priority", "resolution"):
        opts = [o for o in (meta.get("allowedValues") or [])
                if isinstance(o, dict) and (o.get("name") or o.get("value"))]
        for o in opts:
            lbl = o.get("name") or o.get("value")
            if _norm(lbl) == _norm(str(raw)):
                return {"id": str(o["id"])}
        if opts:
            vals = "、".join((o.get("name") or o.get("value")) for o in opts[:20])
            die(f"字段 {fid}({meta.get('name','?')}) 值「{raw}」不在可选值内，可选项: {vals}")
        return {"name": str(raw)}
    if t == "user":
        return {"name": str(raw)}
    if t == "array" and sch.get("items") == "version":
        return [{"name": str(raw)}]
    return raw


def cmd_resolve(args):
    opts = vars(args).copy()
    opts.pop('fn', None)
    result = _safe_resolve(_key_arg(args.key), opts, args.dry_run)
    print(('[DRY-RUN] 预演（未写入）: ' if args.dry_run else '解决结果: ') + json.dumps(result, ensure_ascii=False))


# ---------------- create（建单） ----------------

def _match_fields(fields_meta, label):
    """按中文标签在字段元数据里匹配（归一化子串双向），返回 [(fid, meta)]。"""
    nl = _norm(label)
    hits = []
    for fid, meta in (fields_meta or {}).items():
        name = _norm((meta or {}).get("name", ""))
        if name and (nl in name or name in nl):
            hits.append((fid, meta))
    return hits


def cmd_create(args):
    ctx, is_folder = _ctx_project()
    proj = (args.project or ctx or "").strip().upper()
    if not proj:
        die(f"未指定项目且无上下文项目：加 --project <KEY>，或先 {PROG} use <项目>")
    if args.project and ctx and proj != ctx.upper():
        print(f"[提示] 将在项目 {proj} 建单（当前{'目录所属' if is_folder else '活动'}项目是 {ctx}）"
              f"——确认无误", file=sys.stderr)
    _, d = _request("GET", "/rest/api/2/issue/createmeta",
                    params={"projectKeys": proj, "expand": "projects.issuetypes.fields"})
    pk = ((d.get("projects") or []) or [{}])[0]
    if not pk:
        die(f"项目 {proj} 不存在或当前账号不可建单（createmeta 未返回）；"
            f"先核对 KEY: {PROG} projects --query {proj}")
    types = pk.get("issuetypes") or []
    tname = (args.type or "").strip()
    if not tname:
        if len(types) == 1:
            tname = types[0].get("name", "")
        else:
            die(f"请用 --type 指定工作项类型；{pk.get('key', proj)} 可建: "
                + "、".join(t.get("name", "?") for t in types))
    cands = [t for t in types if _norm(t.get("name", "")) == _norm(tname)] \
        or [t for t in types if _norm(tname) and _norm(tname) in _norm(t.get("name", ""))]
    if not cands:
        die(f"类型「{tname}」在 {pk.get('key', proj)} 不可用（可能没权限或拼写不同）；可建: "
            + "、".join(t.get("name", "?") for t in types))
    if len(cands) > 1:
        die(f"类型「{tname}」匹配到多个: " + "、".join(t.get("name", "?") for t in cands) + "；请写完整名称")
    it = cands[0]
    cm_fields = it.get("fields") or {}

    desc = args.description
    if args.description_file:
        if not os.path.exists(args.description_file):
            die(f"文件不存在: {args.description_file}")
        with open(args.description_file, encoding="utf-8") as fp:
            desc = fp.read()

    body_fields = {"project": {"key": pk.get("key", proj)},
                   "issuetype": ({"id": str(it["id"])} if it.get("id") else {"name": it.get("name")}),
                   "summary": args.summary}
    if desc:
        body_fields["description"] = desc
    field_names = {"summary": "summary"}
    raw_fields = {}
    if args.priority:
        meta = cm_fields.get("priority") or {}
        raw_fields["priority"] = args.priority
        field_names["priority"] = meta.get("name", "优先级")
        opts = [o for o in (meta.get("allowedValues") or []) if isinstance(o, dict)]
        if opts:
            print("优先级可选值: " + " / ".join((o.get("name") or o.get("value") or "") for o in opts[:12]))
    for kv in args.field or []:
        if "=" not in kv:
            die(f"--field 格式应为 标签=值: {kv}")
        label, val = kv.split("=", 1)
        hits = _match_fields(cm_fields, label.strip())
        if not hits:
            die(f"字段「{label}」在建单屏幕中不存在（可先 --dry-run 查看全部字段名）")
        if len(hits) > 1:
            req = [h for h in hits if h[1].get("required")]
            hits = req or hits
        fid, meta = hits[0]
        raw_fields[fid] = val.strip()
        field_names.setdefault(fid, meta.get("name", fid))
        opts = [o for o in (meta.get("allowedValues") or []) if isinstance(o, dict)]
        if opts:
            print(f"字段映射: {label} -> {fid}；可选值: "
                  + " / ".join((o.get("value") or o.get("name") or "") for o in opts[:12]))
    for fid, v in raw_fields.items():
        body_fields[fid] = _wrap_value(cm_fields.get(fid) or {}, fid, v)

    unfilled = []
    for fid, meta in cm_fields.items():
        if meta.get("required") and fid not in body_fields and not meta.get("hasDefaultValue"):
            unfilled.append(f"{fid}({meta.get('name', '?')})")
    if unfilled:
        die(f"以下必填字段未填，已中止不建单: {unfilled}；"
            f"可先 --dry-run 查看建单屏幕字段清单，再用 --field '字段名=值' 补齐")

    payload = {"fields": body_fields}
    if args.dry_run:
        print(f"[DRY-RUN] 将 POST /rest/api/2/issue"
              f"（项目 {pk.get('key', proj)} / 类型 {it.get('name')}）payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        if args.assign:
            print(f"[DRY-RUN] 建单成功后将转交经办人 → {args.assign}（届时解析人员并读回）")
        print("[DRY-RUN] 建单屏幕字段清单（新项目探查用）:")
        for fid, meta in sorted(cm_fields.items()):
            req = "是" if meta.get("required") else "否"
            print(f"  {fid}\t{meta.get('name', '?')}\t必填={req}"
                  f"\t类型={(meta.get('schema') or {}).get('type', '?')}")
        print("（未发任何写请求；去掉 --dry-run 即执行）")
        return

    _, res = _request("POST", "/rest/api/2/issue", body=payload)
    nk = res.get("key") or res.get("id") or "?"
    print(f"已创建 {nk}（{pk.get('key', proj)} / {it.get('name')}）@ {C['base']}")
    fids = [f for f in body_fields if f not in ("project", "issuetype")]
    base = ["summary", "status", "resolution", "issuetype", "priority", "assignee", "created"]
    px = ",".join(dict.fromkeys(base + fids))
    _, chk = _request("GET", f"/rest/api/2/issue/{nk}", params={"fields": px})
    f = chk.get("fields") or {}
    print(f"读回: {nk} -> {(f.get('status') or {}).get('name', '?')} | "
          f"类型: {(f.get('issuetype') or {}).get('name', '-')} | "
          f"经办人: {(f.get('assignee') or {}).get('displayName', '未指派')} | "
          f"摘要: {(f.get('summary') or '')[:60]}")
    ok_names = [field_names.get(fid, fid) for fid in fids if f.get(fid)]
    miss = [field_names.get(fid, fid) for fid in fids if not f.get(fid)]
    if miss:
        print(f"[警告] 以下字段读回为空，请人工核对: {miss}", file=sys.stderr)
    if ok_names:
        print(f"[校验] 已读回字段: {'、'.join(ok_names)}")
    if args.assign:
        tgt = _pick_assignee(nk, args.assign)
        _request("PUT", f"/rest/api/2/issue/{nk}/assignee", body={"name": tgt["name"]})
        _, a = _request("GET", f"/rest/api/2/issue/{nk}", params={"fields": "assignee"})
        cur = (a["fields"].get("assignee") or {})
        mark = "✓" if cur.get("name") == tgt["name"] else "⚠ 读回与目标不一致，请核对"
        print(f"已转交经办人: {tgt.get('displayName')} (name={tgt['name']}) {mark}")


# ---------------- production: durable, read-back verified operations ----------------

class WriteUncertain(SystemExit):
    """Write may have reached JIRA; never blindly resend."""
    def __init__(self, message):
        super().__init__(1)
        self.message = message
        print(f'[结果待核对] {message}；不会自动再次写入。', file=sys.stderr)


def _runs_dir():
    return os.environ.get('JIRA_RUNS_DIR') or os.path.join(_package_root(), 'data', 'runs')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(tmp, 'x', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _load_json(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            result = json.load(f)
        if not isinstance(result, dict):
            raise ValueError('not object')
        return result
    except (OSError, ValueError):
        die('任务记录不存在或已损坏，已停止；请保留原记录，不要盲目重建并再次执行。')


@contextlib.contextmanager
def _file_lock(path):
    """Atomic process lock. Crash leaves a lock intentionally (manual inspection required)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        die(f'另一个任务可能仍在执行，已停止避免重复写入。若上次异常退出，请先确认旧进程已结束再移除锁：{path}')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'created': time.time()}, f)
        yield
    finally:
        os.remove(path)


def _effect(key, operation, payload, send, verify, dry_run=False):
    """Keep an unresolved operation pinned even when a caller changes its payload."""
    if dry_run:
        return _effect_once(key, operation, payload, send, verify, True)
    scope = {'instance': C['base'].rstrip('/'), 'user': _current_user(), 'key': key,
             'operation': operation}
    identity = dict(scope, payload=payload)
    token = _digest(identity)
    guard = os.path.join(_runs_dir(), 'pending', _digest(scope) + '.json')
    effect = os.path.join(_runs_dir(), 'effects', token + '.json')
    with _file_lock(guard + '.lock'):
        old = _load_task(guard) if os.path.isfile(guard) else None
        if old and (old.get('scope') != scope or old.get('active') not in (None, token)):
            die(f'{key} 仍有结果未确认的同类操作，已停止；请使用原任务核对，不能改参数绕过。')
        record = {'schema': 1, 'scope': scope, 'active': token}
        _save_task(guard, record)
        try:
            result = _effect_once(key, operation, payload, send, verify)
        except BaseException:
            saved = _load_json(effect) if os.path.isfile(effect) else {}
            if saved.get('status') == 'rejected':
                record['active'] = None
                _save_task(guard, record)
            raise
        record['active'] = None
        _save_task(guard, record)
        return result


def _effect_once(key, operation, payload, send, verify, dry_run=False):
    """Local journal plus remote reconciliation, not a claim of distributed exactly-once."""
    identity = {'instance': C['base'].rstrip('/'), 'user': _current_user(), 'key': key,
                'operation': operation, 'payload': payload}
    path = os.path.join(_runs_dir(), 'effects', _digest(identity) + '.json')
    if dry_run:
        return {'status': 'planned', 'key': key, 'operation': operation, 'changes': payload}
    with _file_lock(path + '.lock'):
        old = _load_json(path) if os.path.isfile(path) else None
        if old is not None and (old.get('identity') != identity or old.get('status') not in ('sending', 'uncertain', 'rejected', 'done')):
            die('操作记录已损坏或不匹配，已停止，不能把它当成从未执行。')
        if verify():
            _save_json(path, {'identity': identity, 'status': 'done', 'verified_at': time.time()})
            return {'status': 'skipped', 'key': key, 'operation': operation}
        if old and old.get('status') in ('sending', 'uncertain', 'done'):
            die(f'{key} 上次操作的结果与当前单据不一致，已停止避免重复写入；请先核对该单变更历史。')
        _save_json(path, {'identity': identity, 'status': 'sending', 'started_at': time.time()})
        uncertain = False
        try:
            send()
        except WriteUncertain:
            uncertain = True
        except SystemExit:
            _save_json(path, {'identity': identity, 'status': 'rejected', 'updated_at': time.time()})
            raise
        except (OSError, ValueError):
            uncertain = True
        try:
            matched = verify()
        except (SystemExit, OSError, ValueError):
            matched = False
        if not matched:
            _save_json(path, {'identity': identity, 'status': 'uncertain', 'updated_at': time.time()})
            die(f'{key} 写入结果尚未验证，已停止；恢复时只核对已有结果，不自动重复发送。')
        _save_json(path, {'identity': identity, 'status': 'done', 'verified_at': time.time()})
        return {'status': 'reconciled' if uncertain else 'done', 'key': key, 'operation': operation}


def _issue_fields(key):
    return _request('GET', f'/rest/api/2/issue/{key}')[1]['fields']


def _user_id(user):
    return (user or {}).get('name') or (user or {}).get('accountId')


def _current_user():
    me = _request('GET', '/rest/api/2/myself')[1]
    uid = _user_id(me)
    if not uid:
        die('无法核实当前登录人，已停止写入。')
    return uid


def _workflow_names(kind):
    defaults = {'working': 'Working,In Progress,处理中,进行中',
                'qa': 'ST Check,待测试,待验证,待验收', 'start': '接受,Start Progress,开始处理',
                'resolve': '解决,Resolve Issue,Resolve'}
    ctx, _ = _ctx_project()
    pc, _ = _load_project_cfg(ctx)
    text = _nearby_cfg().get(kind + '_states' if kind in ('working', 'qa') else kind + '_transitions')
    text = text or pc.get(kind + '_states' if kind in ('working', 'qa') else kind + '_transitions') or defaults[kind]
    return {_norm(x) for x in text.split(',') if x.strip()}


def _stage(fields):
    st = fields.get('status') or {}
    if (st.get('statusCategory') or {}).get('key') == 'done' or fields.get('resolution'):
        return 'done'
    if _norm(st.get('name')) in _workflow_names('qa'):
        return 'qa'
    if _norm(st.get('name')) in _workflow_names('working'):
        return 'working'
    return 'other'


def _assert_owner(fields):
    if _user_id(fields.get('assignee')) != _current_user():
        die('该单不是当前账号经办，已停止；接管必须另外取得授权，批量任务不会替你抢单。')


def _choose_transition(key, kind, transition_id=None):
    ts = _request('GET', f'/rest/api/2/issue/{key}/transitions',
                  params={'expand': 'transitions.fields'})[1].get('transitions') or []
    if transition_id:
        hits = [t for t in ts if str(t['id']) == str(transition_id)]
    else:
        hits = [t for t in ts if _norm(t.get('name')) in _workflow_names(kind)]
        if kind == 'start' and not hits:
            hits = [t for t in ts if _norm((t.get('to') or {}).get('name')) in _workflow_names('working')]
    if len(hits) != 1:
        die(f'{key} 没有唯一可用的目标流转，已停止；先查看当前状态与可用流转，不会猜选。')
    return hits[0]


def _field_equal(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _field_equal(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            any(_field_equal(v, a) for a in actual) for v in expected)
    return expected == actual or (expected is not None and actual is not None and str(expected) == str(actual))


def _status_matches(expected, actual):
    if expected.get('id') and actual.get('id'):
        return str(expected['id']) == str(actual['id'])
    return expected.get('name') == actual.get('name')


def _transition_matches(key, target, values):
    f = _issue_fields(key)
    return _status_matches(target, f.get('status') or {}) and all(
        _field_equal(v, f.get(k)) for k, v in values.items())


def _send_transition(key, snapshot, transition, values):
    current = _issue_fields(key)
    if not _status_matches(snapshot['status'], current['status']) or (
            snapshot.get('updated') and current.get('updated') != snapshot['updated']) or (
            _user_id(snapshot.get('assignee')) != _user_id(current.get('assignee'))):
        die(f'{key} 在检查期间已被改动，已停止；请重新读取，不覆盖别人的操作。')
    _request('POST', f'/rest/api/2/issue/{key}/transitions',
             body={'transition': {'id': transition['id']}, 'fields': values})


def _safe_start(key, dry_run=False, transition_id=None, checkpoint=None, persist=None):
    _validated_keys([key])
    if checkpoint is None and not dry_run:
        identity = [C['base'].rstrip('/'), _current_user(), key, transition_id]
        path = os.path.join(_runs_dir(), 'start', _digest(identity) + '.json')
        with _file_lock(path + '.lock'):
            saved = _load_task(path) if os.path.isfile(path) else {'schema': 1, 'checkpoint': {}}
            cp = saved['checkpoint']
            return _safe_start(key, transition_id=transition_id, checkpoint=cp,
                               persist=lambda: _save_task(path, saved))
    f = _issue_fields(key)
    cp = checkpoint if checkpoint is not None else {}
    if not cp:
        if _stage(f) in ('working', 'qa', 'done'):
            return {'status': 'skipped', 'key': key, 'reason': '已处理或已交测试，不重复接受'}
        _assert_owner(f)
        t = _choose_transition(key, 'start', transition_id)
        cp.update({'snapshot': f, 'transition': t})
        if persist and not dry_run:
            persist()
    t, f = cp['transition'], cp['snapshot']
    return _effect(key, 'start', {'from': f['status'], 'updated': f.get('updated'), 'to': t['to']},
                   lambda: _send_transition(key, f, t, {}),
                   lambda: _transition_matches(key, t['to'], {}), dry_run)


def _resolve_values(screen, opts):
    raw = {label: opts.get(name) for label, name in
           [('影响范围', 'impact'), ('发生原因', 'cause'), ('解决方法', 'solution'), ('预防措施', 'prevention')] if opts.get(name)}
    for kv in opts.get('field') or []:
        if '=' not in kv:
            die('附加字段必须是“字段名=内容”，未修改任何单据。')
        k, v = kv.split('=', 1)
        raw[k] = v
    out = {}
    for label, value in raw.items():
        exact = [(fid, meta) for fid, meta in screen.items() if fid == label or _norm(meta.get('name')) == _norm(label)]
        hits = exact or _match_fields(screen, label)
        if len(hits) != 1:
            die(f'字段“{label}”不存在或有歧义，已停止；请使用准确字段名称或编号。')
        fid, meta = hits[0]
        out[fid] = _wrap_value(meta, fid, value)
    missing = [meta.get('name', fid) for fid, meta in screen.items() if meta.get('required') and fid not in out]
    if missing:
        die(f'还缺这些必填内容，未执行流转：{missing}')
    return out


def _resolve_target(key, fields, query):
    if not query or query == 'reporter':
        uid = _user_id(fields.get('reporter'))
        if not uid:
            die('无法取得报告人账号，已停止；不能猜测转交对象。')
        return {'name': uid}
    return _pick_assignee(key, query)


def _check_handoff(key, cp, final=False):
    """Do not finish an old handoff over a later human change."""
    f = _issue_fields(key)
    target = _user_id(cp['target'])
    actual = _user_id(f.get('assignee'))
    baseline = cp.get('handoff_snapshot') or cp['snapshot']
    matched = _status_matches(cp['transition']['to'], f.get('status') or {})
    matched = matched and all(_field_equal(v, f.get(k)) for k, v in cp['values'].items())
    matched = matched and _user_id(f.get('reporter')) == _user_id(cp['snapshot'].get('reporter'))
    if final or cp.get('assigned'):
        matched = matched and actual == target
    elif actual != target:
        matched = matched and actual == _user_id(baseline.get('assignee'))
        if cp.get('handoff_snapshot') and baseline.get('updated'):
            matched = matched and baseline['updated'] == f.get('updated')
    if not matched:
        die(f'{key} 的状态、字段、报告人或经办人已有变化，停止旧交接，不覆盖他人的操作。')
    return f


def _safe_assign_target(key, target, dry_run=False, cycle=None, checkpoint=None):
    uid = _user_id(target)
    def send():
        if checkpoint is not None:
            _check_handoff(key, checkpoint)
        _request('PUT', f'/rest/api/2/issue/{key}/assignee', body={'name': uid})
    return _effect(key, 'assign', {'name': uid, 'cycle': cycle}, send,
                   lambda: _user_id(_issue_fields(key).get('assignee')) == uid, dry_run)


def _safe_resolve(key, opts, dry_run=False, checkpoint=None, persist=None):
    _validated_keys([key])
    opts = {k: v for k, v in opts.items() if k in _BATCH_OPTIONS and v is not None}
    if opts.get('repo'):
        opts['repo'] = os.path.abspath(opts['repo'])
    if checkpoint is None and not dry_run:
        path = os.path.join(_runs_dir(), 'resolve', _digest([C['base'].rstrip('/'), _current_user(), key]) + '.json')
        with _file_lock(path + '.lock'):
            saved = _load_task(path) if os.path.isfile(path) else {'schema': 1, 'options': opts, 'checkpoint': {}}
            cp = saved['checkpoint']
            if saved['options'] != opts and cp:
                if not cp.get('completed') or _stage(_issue_fields(key)) in ('qa', 'done'):
                    die('该单已有不同口径的交付记录，已停止；先核原任务，不能改参数绕过未完成步骤。')
                saved = {'schema': 1, 'options': opts, 'checkpoint': {}}
                cp = saved['checkpoint']
            if (saved.get('result') or {}).get('status') == 'done':
                _check_handoff(key, cp, final=True)
                matches = cp and _transition_matches(key, cp['transition']['to'], cp['values'])
                matches = matches and _user_id(_issue_fields(key).get('assignee')) == _user_id(cp['target'])
                if opts.get('comment'):
                    matches = matches and _comment_match(key, opts['comment'], _current_user()) is not None
                if not matches:
                    die(f'{key} 已完成任务的结果后来发生变化，已停止；请核对重开或人工变更，不重放旧任务。')
                return {'status': 'skipped', 'key': key, 'reason': '已完成且重新读回一致'}
            result = _safe_resolve(key, opts, checkpoint=cp,
                                   persist=lambda: _save_task(path, saved))
            saved['result'] = result
            _save_task(path, saved)
            return result
    _validated_keys([key])
    f = _issue_fields(key)
    cp = checkpoint if checkpoint is not None else {}
    if cp and cp.get('completed'):
        _check_handoff(key, cp, final=True)
        matched = _transition_matches(key, cp['transition']['to'], cp['values'])
        matched = matched and _user_id(f.get('assignee')) == _user_id(cp['target'])
        if opts.get('comment'):
            matched = matched and _comment_match(key, opts['comment'], _current_user()) is not None
        if not matched:
            die('此前已完成的交付发生后续变化，不能重放旧任务。')
        return {'status': 'skipped', 'key': key, 'reason': '已完成且读回一致'}
    if not cp and _stage(f) in ('qa', 'done'):
        return {'status': 'skipped', 'key': key, 'reason': '已交测试或已结束，不重复流转'}
    if dry_run and not (opts.get('repo') and opts.get('fix_commits')):
        evidence = {'verified': False, 'warning': '仅探查流转字段，未核实代码交付；正式执行必须提供仓库与修复提交。'}
    else:
        evidence = _verify_delivery(opts.get('repo'), opts.get('fix_commits') or [], opts.get('remote'), opts.get('branch'))
    if not cp:
        _assert_owner(f)
        t = _choose_transition(key, 'resolve', opts.get('transition_id'))
        if dry_run:
            print('[DRY-RUN] 流转屏幕字段清单：' + json.dumps(t.get('fields') or {}, ensure_ascii=False), file=sys.stderr)
        values = _resolve_values(t.get('fields') or {}, opts)
        target = _resolve_target(key, f, opts.get('assign'))
        cp.update({'snapshot': f, 'transition': t, 'values': values, 'target': target, 'evidence': evidence})
        if persist and not dry_run:
            persist()
    else:
        t, values, target = cp['transition'], cp['values'], cp['target']
    payload = {'from': cp['snapshot']['status'], 'updated': cp['snapshot'].get('updated'),
               'to': t['to'], 'fields': values}
    step = _effect(key, 'resolve', payload,
                   lambda: _send_transition(key, cp['snapshot'], t, values),
                   lambda: _transition_matches(key, t['to'], values), dry_run)
    if not dry_run and not _transition_matches(key, t['to'], values):
        die('流转后状态或字段与目标不一致，不能标为成功。')
    if not dry_run:
        handoff = _check_handoff(key, cp)
        if 'handoff_snapshot' not in cp:
            cp['handoff_snapshot'] = {k: handoff.get(k) for k in ('status', 'updated', 'assignee', 'reporter')}
            if persist:
                persist()
    assigned = _safe_assign_target(key, target, dry_run, cycle=_digest(payload), checkpoint=cp)
    if not dry_run:
        _check_handoff(key, cp, final=True)
        cp['assigned'] = True
        if persist:
            persist()
    comment = _safe_comment(key, opts['comment'], dry_run) if opts.get('comment') else None
    if not dry_run:
        _check_handoff(key, cp, final=True)
        if opts.get('comment') and _comment_match(key, opts['comment'], _current_user()) is None:
            die('交付评论未读回，不能标为完成。')
        cp['completed'] = True
        if persist:
            persist()
    return {'status': 'planned' if dry_run else 'done', 'key': key, 'transition': step,
            'assign': assigned, 'comment': comment, 'delivery': evidence,
            **({'fields': t.get('fields') or {}} if dry_run else {})}


def _comment_match(key, body, author):
    data = _read_comments(key)
    if not data['complete']:
        die('评论未完整读取，不能判断是否重复，已停止发送。')
    return next((c for c in data['items'] if c.get('body') == body and _user_id(c.get('author')) == author), None)


def _safe_comment(key, body, dry_run=False):
    _validated_keys([key])
    if not isinstance(body, str) or not body.strip():
        die('评论内容为空，未发送。')
    author = _current_user()
    return _effect(key, 'comment', {'body': body, 'author': author},
                   lambda: _request('POST', f'/rest/api/2/issue/{key}/comment', body={'body': body}),
                   lambda: _comment_match(key, body, author) is not None, dry_run)


def _attachment_match(key, name, data):
    digest = hashlib.sha256(data).hexdigest()
    for a in _issue_fields(key).get('attachment') or []:
        if a.get('filename') == name and int(a.get('size', -1)) == len(data):
            content = _request('GET', a['content'], raw=True)[1]
            if hashlib.sha256(content).hexdigest() == digest:
                return a
    return None


def _safe_upload(key, filename, dry_run=False):
    _validated_keys([key])
    if not os.path.isfile(filename):
        die('待上传文件不存在或是目录，未上传。')
    with open(filename, 'rb') as f:
        data = f.read()
    name = _safe_name(os.path.basename(filename))
    mp, content_type = _build_multipart('file', name, data, mimetypes.guess_type(name)[0] or 'application/octet-stream')
    return _effect(key, 'upload', {'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                   lambda: _request('POST', f'/rest/api/2/issue/{key}/attachments', raw_body=mp,
                                    headers={'Content-Type': content_type, 'X-Atlassian-Token': 'no-check'}),
                   lambda: _attachment_match(key, name, data) is not None, dry_run)


# ---------------- production: batch plan / run / resume ----------------

_BATCH_OPTIONS = {'impact', 'cause', 'solution', 'prevention', 'field', 'assign', 'comment',
                  'fix_commits', 'transition_id', 'start_transition_id', 'repo', 'remote', 'branch'}


def _save_task(path, task):
    task['integrity'] = _digest({k: v for k, v in task.items() if k != 'integrity'})
    _save_json(path, task)


def _load_task(path):
    task = _load_json(path)
    if task.get('schema') != 1 or task.get('integrity') != _digest({k: v for k, v in task.items() if k != 'integrity'}):
        die('任务记录校验失败，已停止；请勿直接修改执行记录，需要改口径应重新生成任务。')
    return task


def _batch_plan(manifest, path):
    if set(manifest) - {'action', 'repo', 'remote', 'branch', 'issues'}:
        die('批量清单含未识别配置，未创建任务；请检查文档中的字段名称。')
    action = manifest.get('action')
    if action not in ('start', 'resolve'):
        die('批量任务只支持接受或解决，不执行任意命令。')
    entries = manifest.get('issues')
    if not isinstance(entries, list) or not entries or any(not isinstance(e, dict) for e in entries):
        die('批量清单需要非空的单据列表。')
    keys = _validated_keys([e.get('key') for e in entries])
    if len(keys) != len(entries):
        die('批量清单存在重复单号，已停止；每张单只能出现一次。')
    normalized = []
    for entry in entries:
        if set(entry) - (_BATCH_OPTIONS | {'key'}):
            die(f"{entry['key']} 含不认识的字段，未创建任务。")
        opts = {k: manifest[k] for k in ('repo', 'remote', 'branch') if k in manifest}
        opts.update({k: v for k, v in entry.items() if k != 'key'})
        for name, value in opts.items():
            if name not in ('field', 'fix_commits') and not isinstance(value, str):
                die(f'配置“{name}”应是文本，未读取或修改任何单据。')
        if opts.get('repo'):
            opts['repo'] = os.path.abspath(opts['repo'])
        if 'field' in opts and (not isinstance(opts['field'], list) or any(not isinstance(x, str) or '=' not in x for x in opts['field'])):
            die('附加字段必须是“字段名=值”的列表，未创建任务。')
        commits = opts.get('fix_commits') or []
        if not isinstance(commits, list) or any(not isinstance(x, str) or not re.fullmatch(r'[a-fA-F0-9]{7,64}', x) for x in commits):
            die('修复提交必须为提交哈希列表，不能猜测或填写分支名。')
        if action == 'resolve' and (not commits or not opts.get('repo')):
            die(f"{entry['key']} 缺少修复提交或代码仓库，未创建解决任务。")
        normalized.append({'key': entry['key'], 'options': opts})
    actor = _current_user()
    rows = []
    for entry in normalized:
        f = _issue_fields(entry['key'])
        rows.append({'key': entry['key'], 'options': entry['options'], 'status': 'pending',
                     'snapshot': {k: f.get(k) for k in ('status', 'updated', 'assignee', 'reporter')},
                     'start_checkpoint': {}, 'resolve_checkpoint': {}})
    task = {'schema': 1, 'id': uuid.uuid4().hex, 'created_at': time.time(), 'action': action,
            'instance': C['base'].rstrip('/'), 'actor': actor,
            'project': keys[0].rsplit('-', 1)[0], 'items': rows}
    with _file_lock(path + '.lock'):
        if os.path.exists(path):
            die('目标任务文件已存在，不能覆盖执行记录；请使用新文件名。')
        _save_task(path, task)
    return _batch_summary(task)


def _batch_summary(task):
    counts = {k: 0 for k in ('pending', 'running', 'planned', 'done', 'skipped', 'failed')}
    for item in task['items']:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    return {'id': task['id'], 'action': task['action'], 'project': task['project'],
            'summary': counts, 'ok': not counts['failed'],
            'items': [{k: item[k] for k in ('key', 'status', 'result', 'error') if k in item} for item in task['items']]}


def _snapshot_unchanged(snapshot, fields):
    return all(snapshot.get(k) == fields.get(k) for k in ('status', 'updated', 'assignee', 'reporter'))


def _batch_run(path, execute=False):
    with _file_lock(path + '.lock'):
        task = _load_task(path)
        if task['instance'] != C['base'].rstrip('/') or task['actor'] != _current_user():
            die('当前 JIRA 实例或登录账号与创建任务时不同，已停止，避免串环境。')
        _validated_keys([x['key'] for x in task['items']])
        # Dry-run uses a memory copy and never writes checkpoints/effects.
        save = (lambda: _save_task(path, task)) if execute else (lambda: None)
        for item in task['items']:
            key, opts = item['key'], item['options']
            try:
                current = _issue_fields(key)
                if item['status'] == 'done':
                    cp = item['resolve_checkpoint'] if task['action'] == 'resolve' else item['start_checkpoint']
                    if task['action'] == 'resolve':
                        _check_handoff(key, cp, final=True)
                        matched = _transition_matches(key, cp['transition']['to'], cp['values'])
                        matched = matched and _user_id(_issue_fields(key).get('assignee')) == _user_id(cp['target'])
                        if opts.get('comment'):
                            matched = matched and _comment_match(key, opts['comment'], task['actor']) is not None
                    else:
                        matched = _transition_matches(key, cp['transition']['to'], {})
                    if not matched:
                        die(f'{key} 完成后的状态已有变化，已停止重放旧任务；请核对重开或其他人的改动。')
                    continue
                if item['status'] == 'skipped':
                    if not _snapshot_unchanged(item['skip_snapshot'], current):
                        die(f'{key} 跳过后状态已改变，请重新规划，不重放旧任务。')
                    continue
                if not item['resolve_checkpoint'] and _stage(current) in ('qa', 'done'):
                    item.update(status='skipped', result={'reason': '已交测试或已结束'},
                                skip_snapshot={k: current.get(k) for k in ('status', 'updated', 'assignee', 'reporter')})
                    save()
                    continue
                if not item['start_checkpoint'] and not item['resolve_checkpoint'] and not _snapshot_unchanged(item['snapshot'], current):
                    die(f'{key} 在生成任务后已有改动，请重新核对清单。')
                if task['action'] == 'start' and _stage(current) == 'working' and not item['start_checkpoint']:
                    item.update(status='skipped', result={'reason': '已在处理中'},
                                skip_snapshot={k: current.get(k) for k in ('status', 'updated', 'assignee', 'reporter')})
                    save()
                    continue
                if task['action'] == 'resolve':
                    # Never start a ticket whose fix has not been delivered.
                    _verify_delivery(opts.get('repo'), opts.get('fix_commits'), opts.get('remote'), opts.get('branch'))
                item['status'] = 'running'
                item.pop('error', None)
                save()
                if task['action'] == 'start' or (not item['resolve_checkpoint'] and _stage(current) != 'working'):
                    start = _safe_start(key, not execute, opts.get('start_transition_id'),
                                        checkpoint=item['start_checkpoint'], persist=save)
                    item['result'] = start
                    save()
                    if task['action'] == 'start':
                        item['status'] = 'done' if execute else 'planned'
                        save()
                        continue
                    if not execute:
                        item.update(status='planned', result={'start': start,
                                    'warning': '未开始单需先接受；解决屏幕将在接受后重新读取并校验，当前未执行任何写入。'})
                        continue
                result = _safe_resolve(key, opts, not execute, item['resolve_checkpoint'], save)
                item.update(status='done' if execute else 'planned', result=result)
            except (SystemExit, OSError, ValueError, KeyError) as e:
                item.update(status='failed', error=f'{type(e).__name__}: 未完成；详见错误输出，恢复时先核对已有结果。')
            save()
        return _batch_summary(task)


def cmd_batch(args):
    if args.batch_command == 'plan':
        source = os.path.abspath(args.file)
        manifest = _load_json(source)
        # Paths in manifests are relative to the manifest, never to an arbitrary harness cwd.
        for obj in [manifest] + list(manifest.get('issues') or []):
            if isinstance(obj, dict) and obj.get('repo'):
                obj['repo'] = os.path.abspath(os.path.join(os.path.dirname(source), obj['repo']))
        target = os.path.abspath(args.save) if args.save else os.path.join(_runs_dir(), uuid.uuid4().hex + '.json')
        result = _batch_plan(manifest, target)
        result['task_file'] = target
    elif args.batch_command == 'status':
        result = _batch_summary(_load_task(os.path.abspath(args.task)))
    else:
        result = _batch_run(os.path.abspath(args.task), args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['ok']:
        raise SystemExit(1)


# ---------------- production: delivery evidence ----------------

def _git(repo, *args, allowed=(0,)):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', env=env, timeout=90,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired):
        die('无法核实代码仓库或远端连接，已阻止流转；请先检查 Git 连接。')
    if p.returncode not in allowed:
        die(f'代码交付检查失败（{args[0]}），已阻止流转；请核对仓库、分支和远端权限。')
    return p.stdout.strip(), p.returncode


def _verify_delivery(repo, fix_commits, remote=None, branch=None):
    """A clean checkout alone is not proof: require explicit fixes on fresh remote refs."""
    if not repo or not fix_commits:
        die('尚未提供本单的修复提交与代码仓库，不能确认代码已交付，已阻止流转。')
    for commit in fix_commits:
        if not isinstance(commit, str) or not re.fullmatch(r'[a-fA-F0-9]{7,64}', commit):
            die('修复提交编号格式不正确，请提供实际提交哈希，不接受分支名或自动猜测。')
    repo = os.path.abspath(repo)
    top, _ = _git(repo, 'rev-parse', '--show-toplevel')
    dirty, _ = _git(repo, 'status', '--porcelain', '--untracked-files=normal')
    if dirty:
        die('代码仓库还有未提交或未跟踪改动，不能确认本次修复已完整交付，已阻止流转。')
    head, _ = _git(repo, 'rev-parse', 'HEAD')
    local_branch, _ = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1))
    if not (remote and branch):
        if remote or branch or not local_branch:
            die('没有明确的目标远端分支，已阻止流转；请同时提供远端名与分支名。')
        remote, _ = _git(repo, 'config', '--get', f'branch.{local_branch}.remote', allowed=(0, 1))
        merge, _ = _git(repo, 'config', '--get', f'branch.{local_branch}.merge', allowed=(0, 1))
        branch = merge[len('refs/heads/'):] if merge.startswith('refs/heads/') else ''
    remotes, _ = _git(repo, 'remote')
    if not remote or remote == '.' or remote not in remotes.splitlines() or not branch:
        die('当前分支没有可核实的远端，已阻止流转；不能仅凭本地记录判断已推送。')
    _git(repo, 'check-ref-format', f'refs/heads/{branch}')
    # Unique private ref: a concurrent user fetch must not change our evidence.
    ref = 'refs/jira-bugfix/checks/' + uuid.uuid4().hex
    try:
        _git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', '--', remote,
             f'refs/heads/{branch}:{ref}')
        tip, _ = _git(repo, 'rev-parse', ref + '^{commit}')
    finally:
        _git(repo, 'update-ref', '-d', ref)
    _, rc = _git(repo, 'merge-base', '--is-ancestor', head, tip, allowed=(0, 1))
    if rc:
        die('当前代码尚未完整进入目标远端分支（超前或分叉），已阻止流转。')
    fixes = []
    for commit in fix_commits:
        sha, _ = _git(repo, 'rev-parse', '--verify', commit + '^{commit}')
        for target in (head, tip):
            _, rc = _git(repo, 'merge-base', '--is-ancestor', sha, target, allowed=(0, 1))
            if rc:
                die('指定修复不在当前代码或目标远端分支中，已阻止流转。')
        fixes.append(sha)
    head_after, _ = _git(repo, 'rev-parse', 'HEAD')
    dirty_after, _ = _git(repo, 'status', '--porcelain', '--untracked-files=normal')
    if head_after != head or dirty_after:
        die('检查期间代码仓库发生变化，已阻止流转，请重新核对。')
    return {'repo': top, 'head': head, 'remote': remote, 'branch': branch,
            'remote_sha': tip, 'fix_commits': list(dict.fromkeys(fixes)), 'checked_at': time.time()}


# ---------------- production: read-only analysis bundle ----------------

def _validated_keys(keys):
    """Never repair malformed keys; constrain new workflows to one explicit context."""
    ctx, _ = _ctx_project()
    out = []
    for key in keys:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", key):
            die(f"单号格式不正确，请核对原文后重试：{key!r}")
        if ctx and key.rsplit('-', 1)[0] != ctx:
            die(f"当前项目是 {ctx}，不能把 {key} 混进本次任务；请先确认项目范围。")
        if key not in out:
            out.append(key)
    if not out or len({k.rsplit('-', 1)[0] for k in out}) != 1:
        die("请提供同一个项目内的单号，不能创建空任务或混合项目。")
    return out


def _read_collection(key, kind, embedded=None):
    """Finish paginated comments/history; preserve a truthful partial flag on failure."""
    data = embedded or {}
    item_key = 'comments' if kind == 'comment' else 'histories'
    if not isinstance(data, dict) or (item_key not in data and 'values' not in data):
        return {'items': [], 'total': None, 'fetched': 0, 'complete': False,
                'warnings': ['接口未返回该类资料，不能视为空记录或已读全。']}
    items = list(data.get(item_key) or data.get('values') or [])
    total = int(data['total']) if data.get('total') is not None else None
    ended = data.get('isLast') is True
    seen = {str(x.get('id')) for x in items if x.get('id') is not None}
    complete = int(data.get('startAt', 0)) == 0
    warnings = []
    while (total is not None and len(items) < total) or (total is None and data.get('isLast') is False):
        start = len(items)
        try:
            _, page = _request('GET', f'/rest/api/2/issue/{key}/{kind}',
                               params={'startAt': start, 'maxResults': 100})
            chunk = page.get(item_key) or page.get('values') or []
            if page.get('total') is not None:
                total = max(total or 0, int(page['total']))
            ended = page.get('isLast') is True
            data = page
            fresh = [x for x in chunk if x.get('id') is None or str(x['id']) not in seen]
            if not fresh or int(page.get('startAt', start)) != start:
                warnings.append('分页接口未继续返回新记录，未把截断数据当作全量。')
                complete = False
                break
            items.extend(fresh)
            seen.update(str(x['id']) for x in fresh if x.get('id') is not None)
        except (SystemExit, OSError, ValueError, AttributeError):
            warnings.append('当前实例无法继续读取分页，以下资料不完整。')
            complete = False
            break
    return {'items': items, 'total': total, 'fetched': len(items),
            'complete': complete and (len(items) == total if total is not None else ended),
            'warnings': warnings + (['接口未提供总数或结束标志，无法证明已读全。'] if total is None and not ended else [])}


def _read_comments(key):
    _, data = _request('GET', f'/rest/api/2/issue/{key}/comment',
                       params={'startAt': 0, 'maxResults': 100})
    return _read_collection(key, 'comment', data)


def _collect_triage(keys):
    keys = _validated_keys(keys)
    rows, errors = [], []
    for key in keys:
        try:
            _, data = _request('GET', f'/rest/api/2/issue/{key}', params={
                'fields': 'summary,description,status,priority,issuetype,updated,assignee,reporter,resolution,comment,attachment',
                'expand': 'changelog'})
            f = data['fields']
            comments = _read_collection(key, 'comment', f.get('comment'))
            history = _read_collection(key, 'changelog', data.get('changelog'))
            _, transitions = _request('GET', f'/rest/api/2/issue/{key}/transitions')
            rows.append({'key': key, 'summary': f.get('summary'), 'description': f.get('description'),
                         'status': f.get('status'), 'assignee': f.get('assignee'), 'reporter': f.get('reporter'),
                         'updated': f.get('updated'), 'resolution': f.get('resolution'),
                         'comments': comments, 'history': history, 'attachments': f.get('attachment') or [],
                         'transitions': transitions.get('transitions') or [],
                         'warnings': comments['warnings'] + history['warnings'],
                         'complete': comments['complete'] and history['complete']})
        except (SystemExit, OSError, ValueError, KeyError) as e:
            errors.append({'key': key, 'error': f'读取失败（{type(e).__name__}），其余单据继续；详情见错误输出。'})
    return {'ok': not errors, 'requested': len(keys), 'count': len(rows), 'issues': rows,
            'errors': errors, 'complete': not errors and all(r['complete'] for r in rows)}


def cmd_triage(args):
    keys = list(args.keys or [])
    if args.key_list:
        keys.extend(args.key_list.split(','))
    if not keys:
        ctx, _ = _ctx_project()
        if not ctx:
            die('未确定项目，请先配置当前项目，避免读取其它项目的单据。')
        query, source = _build_default_jql(ctx)
        found, total = _fetch_issues(query, 'key', args.max, False)
        keys = [x['key'] for x in found]
        if not keys:
            result = {'ok': True, 'requested': 0, 'count': 0, 'issues': [], 'errors': [], 'complete': True}
        else:
            result = _collect_triage(keys)
        result['selection'] = {'jql': query, 'source': source, 'total': total, 'selected': len(keys)}
    else:
        result = _collect_triage(keys)
    if args.format == 'json':
        text = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        parts = [f"# Bug 分析资料：取得 {result['count']}/{result['requested']} 张"]
        for r in result['issues']:
            parts += [f"\n## {r['key']} {r['summary']}",
                      f"状态：{(r['status'] or {}).get('name')}；资料完整：{r['complete']}",
                      str(r['description'] or ''), '\n### 评论',
                      *[str(c.get('body', '')) for c in r['comments']['items']],
                      '\n### 附件/变更/可用流转',
                      json.dumps({k: r[k] for k in ('assignee', 'reporter', 'attachments', 'history', 'transitions', 'warnings')},
                                 ensure_ascii=False, indent=2)]
        parts += [str(e) for e in result['errors']]
        text = '\n'.join(parts)
    if args.save:
        with open(args.save, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
        print(f'分析资料已保存：{os.path.abspath(args.save)}')
    else:
        print(text)
    if not result['ok']:
        raise SystemExit(1)


def main():
    global C
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(
        prog=PROG,
        description=f"JIRA REST CLI（纯数据流，v{VERSION}）—— 初始化/拉单/读单/流转/指派/评论/建单/附件，全部走 REST API，零浏览器。")
    p.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    p.add_argument("--config", metavar="PATH", help=f"凭据文件路径（默认 {_default_creds_path()}）")
    p.add_argument("--profile", metavar="NAME", help=f"凭据 profile 名（读 {_profiles_dir()}/NAME.yaml；多实例/多账号）")
    p.add_argument("--timeout", type=int, metavar="SEC", help="请求超时秒数（默认 60）")
    p.add_argument("--insecure", action="store_true", help="显式跳过 HTTPS 证书校验（仅经授权的临时排障；默认严格校验）")
    sub = p.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="初始化向导：配置账号 → 验证 → 登记项目 → 设活动项目（--check 只体检）")
    ini.add_argument("--check", action="store_true", help="只检查当前配置状态（返回码 0=已就绪），不做任何修改")
    ini.add_argument("--base-url", help=f"JIRA 地址（优先使用已有配置，未配置默认 {DEFAULT_BASE_URL}）")
    ini.add_argument("--username", help="登录名")
    ini.add_argument("--password", help="密码（交互模式下建议不传此参数，改用提示输入且不回显）")
    ini.add_argument("--token", help="Bearer token（Server PAT；与密码二选一）")
    ini.add_argument("--projects", help="登记的项目 KEY，逗号分隔（或 all=全部）")
    ini.add_argument("--use", help="顺便把活动项目设为该 KEY")
    ini.set_defaults(fn=cmd_init)

    us = sub.add_parser("use", help="查看/切换当前活动项目（不带参数=查看列表）")
    us.add_argument("target", nargs="?", help="项目 KEY（或列表里的序号；支持前缀模糊匹配）")
    us.set_defaults(fn=cmd_use)

    pc = sub.add_parser("pconfig", help="项目配置（拉单模板）：包内 config/projects/<KEY>.yaml 或 --here 管当前目录 .jira-project.yaml；优先级 项目文件夹>项目配置>全局>内置")
    pc.add_argument("project", nargs="?", help="项目 KEY（省略=列目录；配合 --default-jql/--clear 时默认用当前活动项目）")
    pc.add_argument("--here", action="store_true",
                    help="管理当前目录的项目文件夹配置 .jira-project.yaml（不带其它参数=查看）")
    pc.add_argument("--set-project", dest="set_project", metavar="KEY", help="（配合 --here）写入 project: KEY")
    pc.add_argument("--default-jql", dest="set_default_jql", metavar="JQL",
                    help="写入默认拉单模板（必须含 {project} 占位符；--here 时写入文件夹配置）")
    pc.add_argument("--clear", action="store_true", help="删除配置文件（--here 时删当前目录的 .jira-project.yaml）")
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

    bt = sub.add_parser('batch', help='批量接受/解决：预演、逐项执行、失败续跑')
    bs = bt.add_subparsers(dest='batch_command', required=True)
    bp = bs.add_parser('plan', help='根据 JSON 清单创建只读计划，不改 JIRA')
    bp.add_argument('--file', required=True, help='JSON 清单路径')
    bp.add_argument('--save', help='执行记录路径，默认存入用户任务目录')
    for verb in ('run', 'resume'):
        br = bs.add_parser(verb, help='默认只预演；执行前需用户明确授权')
        br.add_argument('task', help='plan 返回的任务记录文件')
        mode = br.add_mutually_exclusive_group()
        mode.add_argument('--execute', action='store_true', help='已取得用户授权，实际写入 JIRA')
        mode.add_argument('--dry-run', action='store_true', help='只读预演（默认）')
    bstatus = bs.add_parser('status', help='读取本地执行记录')
    bstatus.add_argument('task')
    bt.set_defaults(fn=cmd_batch)

    ti = sub.add_parser('triage', help='只读：一次拉齐单据分析资料，不改代码或流转')
    ti.add_argument('keys', nargs='*', help='同项目完整单号；不指定则读取默认队列')
    ti.add_argument('--keys', dest='key_list', help='逗号分隔的同项目单号')
    ti.add_argument('--max', type=int, default=10)
    ti.add_argument('--format', choices=['json', 'md'], default='json')
    ti.add_argument('--save', help='保存分析资料到文件')
    ti.set_defaults(fn=cmd_triage)

    i = sub.add_parser("issue", help="拉单详情+评论+附件（单号可只写数字=当前活动项目）")
    i.add_argument("key")
    i.add_argument("--save", help="保存到文件（推荐 AI 阅读）")
    i.add_argument("--format", choices=["md", "json"], default="md")
    i.set_defaults(fn=cmd_issue)

    ch = sub.add_parser("changelog", help="查看单子变更历史（状态流转/字段/经办人变化；排查「更新了但看不出变化」）")
    ch.add_argument("key")
    ch.add_argument("--format", choices=["table", "json"], default="table")
    ch.set_defaults(fn=cmd_changelog)

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
    r.add_argument('--repo', help='修复所在代码仓库（正式流转必须核实已推送）')
    r.add_argument('--fix-commit', dest='fix_commits', action='append', help='本单修复提交哈希，可多次指定；不自动猜测')
    r.add_argument('--remote', help='目标远端名，默认取当前分支上游')
    r.add_argument('--branch', help='目标远端分支，与远端名同时指定')
    r.set_defaults(fn=cmd_resolve)

    cr = sub.add_parser("create", help="建单（按建单屏幕校验必填/词表；--dry-run 可预演）")
    cr.add_argument("--project", metavar="KEY", help="项目 KEY（省略=当前上下文项目：项目文件夹 > 活动项目）")
    cr.add_argument("--type", metavar="类型", help="工作项类型（如 Bug/ST-BUG；省略且唯一时自动取）")
    cr.add_argument("--summary", required=True, help="标题")
    gd = cr.add_mutually_exclusive_group()
    gd.add_argument("--description", help="描述（JIRA wiki 语法）")
    gd.add_argument("--description-file", dest="description_file", help="描述从文件读取（UTF-8）")
    cr.add_argument("--priority", help="优先级（按实例词表校验）")
    cr.add_argument("--assign", help="经办人（显示名/登录名/me；建单后设置并读回）")
    cr.add_argument("--field", action="append", help="附加字段 标签=值（可多次）")
    cr.add_argument("--dry-run", action="store_true", help="只预演不提交（并打印建单屏幕字段清单）")
    cr.set_defaults(fn=cmd_create)

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

    at = sub.add_parser("attachments", help="附件：下载（默认）/ 列表（--list）/ 上传（--upload，可多次）")
    at.add_argument("key")
    at.add_argument("--save-dir", help="保存目录（默认 ./jira-attachments/<KEY>/）")
    at.add_argument("--id", help="只下载指定附件 id（可先跑 issue KEY 看列表）")
    at.add_argument("--list", action="store_true", help="只列出附件（不下载）")
    at.add_argument("--upload", action="append", metavar="FILE", help="上传附件（可多次；与下载/列表互斥）")
    at.add_argument("--comment", help="配合 --upload：上传成功后补一条评论")
    at.add_argument("--dry-run", action="store_true", help="配合 --upload：只预演不提交")
    at.set_defaults(fn=cmd_attachments)

    args = p.parse_args()
    if args.cmd == "init":
        # init 自行处理配置的加载/创建（首次运行时文件还不存在）
        args.fn(args)
        return
    nb = _nearby_cfg()
    if (not args.config) and (not args.profile) and (not os.environ.get("JIRA_PROFILE")) and nb.get("profile"):
        args.profile = nb["profile"]
        print(f"[提示] 使用项目文件夹配置的 profile: {nb['profile']}（{nb['_path']}）", file=sys.stderr)
    path = resolve_creds_path(args)
    C = load_creds(path)
    if args.timeout:
        C["timeout"] = args.timeout
    if args.insecure:
        C["insecure"] = True
    args.fn(args)


if __name__ == "__main__":
    main()
