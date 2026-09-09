#!/usr/bin/env python3
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = int(os.getenv("PORT", "8082"))
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "http://host.docker.internal:28082").rstrip("/")
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "").strip()
TRIGGER_TIMEOUT_SEC = int(os.getenv("TRIGGER_TIMEOUT_SEC", "300"))
APP_TITLE = os.getenv("APP_TITLE", "对话助手").strip() or "对话助手"
UI_VERSION = os.getenv("UI_VERSION", str(int(time.time())))
LOG_FILE = os.getenv("LOG_FILE", "/logs/voice_remote.log").strip()
MAX_TASKS = int(os.getenv("MAX_TASKS", "120"))
STATIC_DIR = Path(__file__).with_name("static")
APP_JS_FILE = STATIC_DIR / "app.js"
TURNS_FILE = Path(os.getenv("TURNS_FILE", "/logs/voice_turns.jsonl"))

_TASKS = {}
_TASK_ORDER = []
_TASK_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _log(msg: str) -> None:
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    if not LOG_FILE:
        return
    try:
        path = Path(LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def _json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler, status, html):
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler, status, text, content_type):
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _load_static_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _upstream_url(path, extra_query=None):
    query = {}
    if TRIGGER_TOKEN:
        query["token"] = TRIGGER_TOKEN
    if extra_query:
        query.update(extra_query)
    q = urllib.parse.urlencode(query)
    return f"{UPSTREAM_BASE_URL}{path}" + (f"?{q}" if q else "")


def _call_upstream_trigger(text=""):
    payload = {"text": text} if text else {}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    req = urllib.request.Request(
        _upstream_url("/trigger"),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"} if body else {},
    )
    with urllib.request.urlopen(req, timeout=TRIGGER_TIMEOUT_SEC) as resp:
        raw = resp.read()
        return resp.status, raw.decode("utf-8", errors="ignore")


def _call_upstream_healthz():
    req = urllib.request.Request(_upstream_url("/healthz"), method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return resp.status, raw


def _call_upstream_status():
    req = urllib.request.Request(_upstream_url("/api/status"), method="GET")
    with urllib.request.urlopen(req, timeout=3) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return resp.status, raw


def _new_task(mode: str, text: str):
    task_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    task = {
        "id": task_id,
        "mode": mode,
        "text": text,
        "status": "queued",
        "created_ms": now,
        "updated_ms": now,
        "started_ms": None,
        "finished_ms": None,
        "cost_ms": None,
        "result": None,
        "error": "",
        "http_status": None,
    }
    with _TASK_LOCK:
        _TASKS[task_id] = task
        _TASK_ORDER.append(task_id)
        if len(_TASK_ORDER) > MAX_TASKS:
            old_id = _TASK_ORDER.pop(0)
            _TASKS.pop(old_id, None)
    return task


def _set_task(task_id: str, **kwargs):
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        task.update(kwargs)
        task["updated_ms"] = int(time.time() * 1000)


def _get_task(task_id: str):
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        return dict(task)


def _run_task(task_id: str, text: str):
    mode = "text" if text else "voice"
    started = time.monotonic()
    _set_task(task_id, status="running", started_ms=int(time.time() * 1000))
    _log(f"[voice-remote] task start id={task_id} mode={mode} text_len={len(text)}")
    try:
        code, raw = _call_upstream_trigger(text=text)
        payload = json.loads(raw) if raw else {"ok": code < 400}
        ok = bool(payload.get("ok", code < 400)) and code < 400
        _set_task(
            task_id,
            status="done" if ok else "error",
            result=payload,
            error="" if ok else str(payload.get("error", "upstream failed")),
            http_status=code,
            finished_ms=int(time.time() * 1000),
            cost_ms=int((time.monotonic() - started) * 1000),
        )
        _log(f"[voice-remote] task done id={task_id} mode={mode} status={code} cost_ms={int((time.monotonic() - started) * 1000)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(body) if body else {"ok": False, "error": e.reason}
        except Exception:
            payload = {"ok": False, "error": body or e.reason}
        _set_task(
            task_id,
            status="error",
            result=payload,
            error=str(payload.get("error", e.reason)),
            http_status=e.code,
            finished_ms=int(time.time() * 1000),
            cost_ms=int((time.monotonic() - started) * 1000),
        )
        _log(f"[voice-remote] task http_error id={task_id} mode={mode} status={e.code} cost_ms={int((time.monotonic() - started) * 1000)} err={e}")
    except Exception as e:
        _set_task(
            task_id,
            status="error",
            result={"ok": False, "error": str(e)},
            error=str(e),
            http_status=502,
            finished_ms=int(time.time() * 1000),
            cost_ms=int((time.monotonic() - started) * 1000),
        )
        _log(f"[voice-remote] task failed id={task_id} mode={mode} cost_ms={int((time.monotonic() - started) * 1000)} err={e}")


def _submit_task(text: str):
    mode = "text" if text else "voice"
    task = _new_task(mode, text)
    th = threading.Thread(target=_run_task, args=(task["id"], text), daemon=True)
    th.start()
    return task


def _index_html():
    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{APP_TITLE}</title>
  <style>
    :root {{
      --bg-1: #09121a;
      --bg-2: #143247;
      --card: rgba(255, 255, 255, 0.08);
      --line: rgba(255, 255, 255, 0.15);
      --text: #f6f8fb;
      --muted: #a5b7c6;
      --accent: #f59e0b;
      --accent2: #ea580c;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      min-height: 100dvh;
      font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(245,158,11,0.2), transparent 36%),
        radial-gradient(circle at bottom right, rgba(14,165,233,0.2), transparent 32%),
        linear-gradient(135deg, var(--bg-1), var(--bg-2));
      display: flex;
      flex-direction: column;
      padding: 0;
    }}
    .app {{
      flex: 1;
      width: 100%;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 20px;
      padding: clamp(20px, 4vw, 52px);
    }}
    h1 {{ margin: 0; font-size: clamp(28px, 4vw, 44px); }}
    .lead {{ margin: 0; color: var(--muted); line-height: 1.7; font-size: clamp(15px, 1.6vw, 18px); }}
    .grid {{ display: grid; gap: 20px; grid-template-columns: 1fr 1fr; }}
    .card {{
      background: rgba(0,0,0,0.2);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 16px;
      padding: 16px;
    }}
    .card h2 {{ margin: 0 0 10px; font-size: 20px; }}
    .hint {{ margin: 0 0 12px; color: var(--muted); line-height: 1.65; font-size: 14px; }}
    .btn {{
      width: 100%;
      border: 0;
      border-radius: 16px;
      padding: 18px 16px;
      font-size: 19px;
      font-weight: 700;
      color: #fff;
      cursor: pointer;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      box-shadow: 0 10px 25px rgba(234,88,12,0.33);
    }}
    .btn:disabled {{ opacity: 0.62; cursor: not-allowed; }}
    .row {{ display: flex; gap: 10px; }}
    input[type=text] {{
      flex: 1;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.2);
      background: rgba(0,0,0,0.25);
      color: var(--text);
      padding: 11px 12px;
      outline: none;
      font-size: 15px;
    }}
    .meta {{ margin-top: 10px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 880px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
        .turns-wrap {{
            flex: 1 1 auto;
            min-height: 0;
            display: flex;
            flex-direction: column;
            background: rgba(0,0,0,0.22);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 18px;
        }}
        .turns-hdr {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }}
    .turns-hdr h2 {{ margin: 0; font-size: 18px; }}
    .clear-btn {{ background: none; border: 1px solid rgba(255,255,255,0.2); color: var(--muted); border-radius: 8px; padding: 4px 10px; font-size: 13px; cursor: pointer; }}
    .turns {{ flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; }}
    .turn {{ background: rgba(0,0,0,0.22); border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; padding: 10px 14px; font-size: 14px; line-height: 1.65; }}
        .turn-live {{
            border-style: dashed;
            position: sticky;
            top: 0;
            z-index: 1;
            background: rgba(3,12,19,0.92);
        }}
    .turn-src {{ color: var(--accent); font-size: 12px; margin-bottom: 4px; }}
    .turn-txt {{ color: var(--text); }}
    .turn-rep {{ color: var(--ok); margin-top: 4px; }}
    .turn-meta {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
  </style>
</head>
<body>
  <main class=\"app\" data-ui-version=\"{UI_VERSION}\">
    <h1>{APP_TITLE}</h1>
    <p class=\"lead\">点击即可语音，或输入文本直接执行。两种模式都会触发 TTS 语音播报。</p>

    <section class=\"grid\">
      <article class=\"card\">
        <h2>语音模式</h2>
        <p class=\"hint\">1. 点击开始  2. 立刻对麦克风说话  3. 等待助手播报</p>
        <button id=\"voiceBtn\" class=\"btn\">开始语音指令</button>
      </article>

      <article class=\"card\">
        <h2>文本模式</h2>
                <p class=\"hint\">输入示例：播放测试视频 / 帮我按内容归档手机相册</p>
        <div class=\"row\">
          <input id=\"textInput\" type=\"text\" placeholder=\"输入文本指令\" />
          <button id=\"textBtn\" class=\"btn\" style=\"width: 140px;\">发送文本</button>
        </div>
      </article>
    </section>

        <div id="status" style="display:none;">待机中。请选择语音或文本模式。</div>
        <div class="meta">上游服务：{UPSTREAM_BASE_URL} · 版本：{UI_VERSION}</div>
        <section class="turns-wrap">
            <div class="turns-hdr"><h2>对话历史</h2><button class="clear-btn" id="clearTurns">清空</button></div>
            <div id="turns" class="turns"></div>
        </section>
    </main>

  <script>
    window.__voiceUiLoaded = false;
        function _reportUiStatus(message) {{
            if (typeof window.__voiceSetStatus === 'function') {{
                window.__voiceSetStatus(message);
                return;
            }}
            const turnsEl = document.getElementById('turns');
            if (!turnsEl) return;
            let live = document.getElementById('liveStatusTurn');
            if (!live) {{
                live = document.createElement('div');
                live.id = 'liveStatusTurn';
                live.className = 'turn turn-live';
                live.innerHTML = '<div class="turn-src">🧾 实时状态</div><div class="turn-txt"></div>';
                turnsEl.prepend(live);
            }}
            const txt = live.querySelector('.turn-txt');
            if (txt) txt.textContent = message;
        }}
    window.addEventListener('error', function (event) {{
      const detail = event && event.message ? '：' + event.message : '';
        _reportUiStatus('页面脚本异常' + detail);
    }});
    window.addEventListener('unhandledrejection', function (event) {{
      const detail = event && event.reason ? '：' + String(event.reason) : '';
        _reportUiStatus('页面脚本异常' + detail);
    }});
    window.addEventListener('load', function () {{
      window.setTimeout(function () {{
        if (window.__voiceUiLoaded) return;
            _reportUiStatus('页面脚本异常：脚本未完成加载');
      }}, 1200);
    }});
  </script>
  <script src=\"/static/app.js?v={UI_VERSION}\"></script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *values):
        line = fmt % values
        # 前端页面每秒轮询 /api/status 等会产生大量 200 访问噪音且无诊断价值，
        # 关键事件（trigger/task 结果/错误）均已单独 _log，故跳过所有成功响应。
        if " 200 " in line:
            return
        _log(f"[voice-remote] {self.address_string()} - {line}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            _html_response(self, HTTPStatus.OK, _index_html())
            return
        if parsed.path == "/static/app.js":
            try:
                _text_response(
                    self,
                    HTTPStatus.OK,
                    _load_static_text(APP_JS_FILE),
                    "application/javascript",
                )
            except FileNotFoundError:
                _log(f"[voice-remote] static file missing: {APP_JS_FILE}")
                _text_response(
                    self,
                    HTTPStatus.NOT_FOUND,
                    "console.error('app.js not found');",
                    "application/javascript",
                )
            except Exception as e:  # noqa: BLE001
                _log(f"[voice-remote] static file load failed: {e}")
                _text_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "console.error('app.js load failed');",
                    "application/javascript",
                )
            return
        if parsed.path == "/api/healthz":
            try:
                code, raw = _call_upstream_healthz()
                payload = json.loads(raw) if raw else {}
                payload["proxy_ok"] = True
                payload["ui_version"] = UI_VERSION
                _json_response(self, code, payload)
            except Exception as e:  # noqa: BLE001
                _json_response(self, HTTPStatus.BAD_GATEWAY, {
                    "ok": False,
                    "proxy_ok": False,
                    "error": str(e),
                })
            return
        if parsed.path == "/api/status":
            try:
                code, raw = _call_upstream_status()
                payload = json.loads(raw) if raw else {}
                _json_response(self, code, payload)
            except Exception:  # noqa: BLE001
                _json_response(self, HTTPStatus.OK, {"ok": True, "state": "idle", "busy": False})
            return
        if parsed.path.startswith("/api/task/"):
            task_id = parsed.path.rsplit("/", 1)[-1].strip()
            self._handle_task_get(task_id)
            return
        if parsed.path == "/api/task":
            task_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0].strip()
            self._handle_task_get(task_id)
            return
        if parsed.path == "/api/turns":
            self._handle_turns(parsed)
            return
        if parsed.path == "/api/trigger":
            self._handle_trigger(parsed)
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/trigger":
            self._handle_trigger(parsed)
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def _parse_text(self, parsed):
        text = urllib.parse.parse_qs(parsed.query).get("text", [""])[0].strip()
        if text:
            return text
        if self.command != "POST":
            return ""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return ""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                obj = json.loads(raw.decode("utf-8", errors="ignore"))
                if isinstance(obj, dict):
                    return str(obj.get("text", "")).strip()
            except Exception:
                return ""
        elif "application/x-www-form-urlencoded" in ctype:
            data = urllib.parse.parse_qs(raw.decode("utf-8", errors="ignore"))
            return (data.get("text", [""]) or [""])[0].strip()
        return ""

    def _handle_trigger(self, parsed):
        text = self._parse_text(parsed)
        mode = "text" if text else "voice"
        task = _submit_task(text)
        _log(
            f"[voice-remote] trigger accepted id={task['id']} "
            f"method={self.command} mode={mode} text_len={len(text)} from={self.client_address[0]}"
        )
        _json_response(self, HTTPStatus.ACCEPTED, {
            "ok": True,
            "task_id": task["id"],
            "status": task["status"],
            "poll": f"/api/task/{task['id']}",
        })

    def _handle_task_get(self, task_id: str):
        if not task_id:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing task id"})
            return
        task = _get_task(task_id)
        if not task:
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "task not found"})
            return
        _json_response(self, HTTPStatus.OK, {"ok": True, "task": task})

    def _handle_turns(self, parsed):
        since = float(urllib.parse.parse_qs(parsed.query).get("since", ["0"])[0] or "0")
        turns = []
        try:
            if TURNS_FILE.exists():
                with TURNS_FILE.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            t = json.loads(line)
                            if float(t.get("ts", 0)) > since:
                                turns.append(t)
                        except Exception:
                            pass
        except Exception as e:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(e)})
            return
        _json_response(self, HTTPStatus.OK, {"ok": True, "turns": turns})


def main():
    _log(f"[voice-remote] log file: {LOG_FILE or '(disabled)'}")
    server = ThreadingHTTPServer((LISTEN_HOST, PORT), Handler)
    _log(f"[voice-remote] listening at http://{LISTEN_HOST}:{PORT}, upstream={UPSTREAM_BASE_URL}, ui={UI_VERSION}")
    server.serve_forever()


if __name__ == "__main__":
    main()
