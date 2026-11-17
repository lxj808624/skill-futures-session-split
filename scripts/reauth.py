"""重走一次 PandaData OAuth 授权，把新令牌写进本地缓存（不覆盖平台 oauth.json）。

用法：
    python scripts/reauth.py            # 打印授权 URL 并等待回调（默认 15 分钟）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import pathlib
import secrets
import threading
import urllib.parse
import urllib.request

HOME = pathlib.Path.home()
OAUTH_FILE = HOME / ".dsh-quantstudio/quantskills/panda-mcp/oauth.json"
TOKEN_CACHE = HOME / ".dsh-logs/panda_token.json"
AUTHORIZE = "https://pandadatamcp.pandaaiquant.com/authorize"
TOKEN_ENDPOINT = "https://pandadatamcp.pandaaiquant.com/token"


def build_url(client_id: str, redirect_uri: str, verifier: str, state: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "pandadata:read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE}?{urllib.parse.urlencode(params)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=900)
    args = ap.parse_args()

    client = json.loads(OAUTH_FILE.read_text())["client"]
    client_id = client["client_id"]
    redirect_uri = client["redirect_uris"][0]
    port = int(urllib.parse.urlparse(redirect_uri).port or 80)
    verifier = json.loads(OAUTH_FILE.read_text()).get("codeVerifier") or base64.urlsafe_b64encode(
        secrets.token_bytes(48)).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write("state 不匹配".encode())
                return
            if "code" in query:
                result["code"] = query["code"][0]
            elif "error" in query:
                result["error"] = query["error"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>授权完成，可以关闭这个页面回到终端。</h3>".encode())
            done.set()

        def log_message(self, *a) -> None:  # 静音
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = build_url(client_id, redirect_uri, verifier, state)
    print("请在浏览器里打开下面这个链接并完成授权（如果已登录会直接跳转回来）：\n")
    print(url)
    print(f"\n等待回调 {redirect_uri} …", flush=True)

    if not done.wait(timeout=args.wait):
        print("超时：没有收到回调")
        return 1

    if "code" not in result:
        print("授权失败：", result.get("error"))
        return 1

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read().decode())
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(tokens, ensure_ascii=False, indent=1))
    print("新令牌已写入", TOKEN_CACHE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
