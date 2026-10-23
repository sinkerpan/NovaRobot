from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger("novarobot.http")

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>NovaRobot 配网</title>
  <style>
    :root { color-scheme: light; --bg:#f3f5f7; --card:#fff; --line:#e5e7eb; --text:#111; --muted:#6b7280; --accent:#0f766e; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
    header { padding:20px 16px 8px; }
    h1 { font-size:1.35rem; margin:0 0 6px; }
    p { margin:0; color:var(--muted); font-size:.95rem; }
    .wrap { padding:8px 16px 28px; max-width:520px; margin:0 auto; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin-top:12px; }
    .row { display:flex; gap:8px; align-items:center; }
    button, .btn {
      appearance:none; border:0; border-radius:10px; padding:10px 14px; background:var(--accent); color:#fff; font-weight:600;
    }
    button.secondary { background:#e5e7eb; color:#111; }
    button:disabled { opacity:.55; }
    label { display:block; margin:10px 0 6px; font-size:.9rem; color:var(--muted); }
    input {
      width:100%; padding:12px; border:1px solid var(--line); border-radius:10px; font-size:1rem; background:#fff;
    }
    .list { list-style:none; margin:0; padding:0; max-height:46vh; overflow:auto; }
    .list li {
      display:flex; justify-content:space-between; gap:10px; padding:12px 10px; border-bottom:1px solid var(--line); cursor:pointer;
    }
    .list li:active, .list li.selected { background:#ecfeff; }
    .ssid { font-weight:600; }
    .meta { color:var(--muted); font-size:.85rem; white-space:nowrap; }
    .tag { display:inline-block; margin-left:6px; padding:1px 6px; border-radius:999px; background:#ecfdf5; color:#047857; font-size:.75rem; }
    .hint { margin-top:10px; color:var(--muted); font-size:.85rem; line-height:1.4; }
    #msg { margin-top:12px; white-space:pre-wrap; font-size:.9rem; }
    .ok { color:#047857; } .err { color:#b91c1c; }
  </style>
</head>
<body>
  <header>
    <h1>连接家庭 Wi-Fi</h1>
    <p>点选附近网络，只需输入密码。已保存的可一键重连。</p>
  </header>
  <div class="wrap">
    <div class="card">
      <div class="row">
        <button type="button" id="scanBtn" class="secondary">刷新列表</button>
        <span id="scanState" class="meta">准备扫描…</span>
      </div>
      <ul class="list" id="netList"></ul>
      <p class="hint">连上机器人热点后打开本页；也可访问 http://10.42.0.1:18080</p>
    </div>

    <div class="card" id="formCard" hidden>
      <div><span class="ssid" id="pickedSsid">-</span> <span class="tag" id="pickedTag"></span></div>
      <label for="password">Wi-Fi 密码</label>
      <input id="password" type="password" autocomplete="current-password" placeholder="开放网络可留空" />
      <div class="row" style="margin-top:12px">
        <button type="button" id="connectBtn">保存并连接</button>
        <button type="button" id="clearBtn" class="secondary">重选</button>
      </div>
      <div id="msg"></div>
    </div>
  </div>
<script>
(() => {
  const listEl = document.getElementById('netList');
  const scanBtn = document.getElementById('scanBtn');
  const scanState = document.getElementById('scanState');
  const formCard = document.getElementById('formCard');
  const pickedSsid = document.getElementById('pickedSsid');
  const pickedTag = document.getElementById('pickedTag');
  const password = document.getElementById('password');
  const connectBtn = document.getElementById('connectBtn');
  const clearBtn = document.getElementById('clearBtn');
  const msg = document.getElementById('msg');
  let selected = null;
  let saved = new Set();

  function setMsg(text, ok) {
    msg.className = ok ? 'ok' : 'err';
    msg.textContent = text;
  }

  function bars(sig) {
    if (sig >= 70) return '强';
    if (sig >= 40) return '中';
    return '弱';
  }

  function render(networks) {
    listEl.innerHTML = '';
    if (!networks.length) {
      listEl.innerHTML = '<li><span class="ssid">未扫到网络</span><span class="meta">请靠近路由后刷新</span></li>';
      return;
    }
    networks.forEach(n => {
      const li = document.createElement('li');
      const left = document.createElement('div');
      left.innerHTML = `<div class="ssid">${n.ssid}${n.saved ? '<span class="tag">已保存</span>' : ''}</div>
        <div class="meta">${n.security || 'Open'}</div>`;
      const right = document.createElement('div');
      right.className = 'meta';
      right.textContent = `${bars(n.signal||0)} ${n.signal||0}%`;
      li.appendChild(left); li.appendChild(right);
      li.onclick = () => pick(n, li);
      listEl.appendChild(li);
    });
  }

  function pick(n, li) {
    selected = n;
    [...listEl.children].forEach(x => x.classList.remove('selected'));
    if (li) li.classList.add('selected');
    formCard.hidden = false;
    pickedSsid.textContent = n.ssid;
    pickedTag.textContent = n.saved ? '可一键重连' : (n.security || '');
    password.value = '';
    password.placeholder = n.saved ? '可留空，将使用已保存密码' : (String(n.security||'').toLowerCase().includes('open') || !n.security ? '开放网络可留空' : '请输入密码');
    password.focus();
    setMsg('', true);
  }

  async function loadSaved() {
    try {
      const st = await fetch('/status').then(r => r.json());
      (st.saved_wifi || []).forEach(s => saved.add(s));
    } catch (e) {}
  }

  async function scan() {
    scanBtn.disabled = true;
    scanState.textContent = '扫描中…';
    try {
      await loadSaved();
      const data = await fetch('/api/wifi/scan').then(r => r.json());
      const nets = (data.networks || []).map(n => ({...n, saved: saved.has(n.ssid)}));
      // put saved first
      nets.sort((a,b) => (b.saved - a.saved) || ((b.signal||0)-(a.signal||0)));
      // also show saved not currently seen
      saved.forEach(ssid => {
        if (!nets.some(n => n.ssid === ssid)) nets.unshift({ssid, signal:0, security:'已保存', saved:true});
      });
      render(nets);
      scanState.textContent = `共 ${nets.length} 个`;
    } catch (e) {
      scanState.textContent = '扫描失败';
      setMsg(String(e), false);
    } finally {
      scanBtn.disabled = false;
    }
  }

  async function connect() {
    if (!selected) return;
    connectBtn.disabled = true;
    setMsg('正在连接，请稍候（约 10–25 秒）…', true);
    try {
      const res = await fetch('/api/wifi', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ssid: selected.ssid, password: password.value || ''})
      });
      const text = await res.text();
      let j; try { j = JSON.parse(text); } catch { j = {raw:text}; }
      if (j.ok) {
        setMsg(`已连接 ${j.ssid}\\nIP: ${j.ip || '-'}\\n手机可改连同一 Wi-Fi，用 App 控制机器人`, true);
      } else {
        setMsg(`连接失败：${j.error || text}\\n请检查密码后重试`, false);
      }
    } catch (e) {
      setMsg('请求失败（机器人可能正在切网）：' + e, false);
    } finally {
      connectBtn.disabled = false;
    }
  }

  scanBtn.onclick = scan;
  connectBtn.onclick = connect;
  clearBtn.onclick = () => { selected=null; formCard.hidden=true; setMsg('', true); };
  scan();
})();
</script>
</body>
</html>
"""

# Common captive-portal probe paths → redirect to config page
CAPTIVE_PATHS = {
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/ncsi.txt",
    "/connecttest.txt",
    "/success.txt",
    "/canonical.html",
}


class HttpApi:
    def __init__(
        self,
        port: int,
        status_fn: Callable[[], dict[str, Any]],
        provision_fn: Callable[[str, str], dict[str, Any]],
        scan_fn: Callable[[], list[dict[str, Any]]],
        mode_fn: Callable[[str], dict[str, Any]],
    ) -> None:
        self.port = port
        self.status_fn = status_fn
        self.provision_fn = provision_fn
        self.scan_fn = scan_fn
        self.mode_fn = mode_fn
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                log.info("%s - %s", self.address_string(), fmt % args)

            def _json(self, code: int, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _html(self, body: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _redirect(self, location: str) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {}

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path in CAPTIVE_PATHS:
                    # Force phone captive portal UI toward our page
                    self._redirect("http://10.42.0.1:18080/")
                    return
                if path in {"/", "/index.html"}:
                    self._html(INDEX_HTML)
                    return
                if path in {"/status", "/api/status", "/discover", "/api/discover"}:
                    self._json(200, api.status_fn())
                    return
                if path in {"/health", "/api/health"}:
                    self._json(200, {"ok": True})
                    return
                if path in {"/api/wifi/scan", "/wifi/scan"}:
                    self._json(200, {"networks": api.scan_fn()})
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                body = self._body()
                if path in {"/api/wifi", "/wifi"}:
                    ssid = str(body.get("ssid") or "").strip()
                    password = str(body.get("password") or "")
                    if not ssid:
                        self._json(400, {"ok": False, "error": "ssid required"})
                        return
                    self._json(200, api.provision_fn(ssid, password))
                    return
                if path in {"/api/mode", "/mode"}:
                    mode = str(body.get("mode") or "").strip().lower()
                    if mode not in {"ap", "sta"}:
                        self._json(400, {"ok": False, "error": "mode must be ap or sta"})
                        return
                    self._json(200, api.mode_fn(mode))
                    return
                self._json(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="novarobot-http", daemon=True)
        self._thread.start()
        log.info("HTTP API listening on 0.0.0.0:%s", self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
