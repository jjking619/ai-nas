#!/usr/bin/env python3
import json
import os
import re
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request

DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/downloads")).resolve()
YTDLP_TIMEOUT_SEC = int(os.getenv("YTDLP_TIMEOUT_SEC", "1800"))
TTS_WEBHOOK_URL = os.getenv("TTS_WEBHOOK_URL", "").strip()
DEFAULT_TTS_TEXT = os.getenv("DOWNLOAD_TTS_TEXT", "下载已完成").strip() or "下载已完成"

_ALLOWED_SEGMENT = re.compile(r"[^\w\u4e00-\u9fff\-()（）\[\]【】 .]+")


def _json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _safe_segment(seg):
    seg = seg.strip().replace("\\", "/").replace("..", "")
    seg = _ALLOWED_SEGMENT.sub("_", seg)
    seg = re.sub(r"_+", "_", seg).strip("._ ")
    return seg[:80]


def _safe_subdir(subdir):
    raw = (subdir or "").strip()
    if not raw:
        return "家庭影院"

    lowered = raw.replace(" ", "")
    if any(k in lowered for k in ("剧集", "电视剧", "连续剧")):
        return "家庭影院/剧集"
    if "电影" in lowered:
        return "家庭影院/电影"
    if "家庭影院" in lowered:
        return "家庭影院"
    if not lowered:
        return "家庭影院"

    parts = []
    for part in raw.replace("\\", "/").split("/"):
        cleaned = _safe_segment(part)
        if cleaned:
            parts.append(cleaned)

    return "/".join(parts) if parts else "家庭影院"


def _resolve_target_dir(subdir):
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    rel = _safe_subdir(subdir)
    target = (DOWNLOAD_ROOT / rel).resolve()
    if os.path.commonpath([str(DOWNLOAD_ROOT), str(target)]) != str(DOWNLOAD_ROOT):
        raise ValueError("target_subdir is outside download root")
    target.mkdir(parents=True, exist_ok=True)
    return target, rel


def _run_ytdlp(url, query, target_dir):
    src = url.strip() if url else f"bilisearch1:{query.strip()}"  # 国内优先：Bilibili
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--newline",
        "-P",
        str(target_dir),
        "-o",
        "%(title).180B [%(id)s].%(ext)s",
        "--print",
        "after_move:filepath",
        src,
    ]

    p = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        timeout=YTDLP_TIMEOUT_SEC,
    )

    out_lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    files = [ln for ln in out_lines if ln.startswith(str(target_dir))]

    if p.returncode != 0:
        err = (p.stderr or p.stdout or "yt-dlp failed").strip()
        raise RuntimeError(err[-600:])

    return files


def _notify_tts(text):
    if not TTS_WEBHOOK_URL:
        return "skipped"

    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        TTS_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                return "sent"
            return f"http_{resp.status}"
    except Exception:
        return "failed"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/download":
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return

        url = str(payload.get("url", "")).strip()
        query = str(payload.get("query", "")).strip()
        target_subdir = str(payload.get("target_subdir", "")).strip()
        notify_tts = bool(payload.get("notify_tts", True))
        tts_text = str(payload.get("tts_message", "")).strip() or DEFAULT_TTS_TEXT

        if not url and not query:
            _json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "url or query is required"},
            )
            return

        try:
            target_dir, safe_rel = _resolve_target_dir(target_subdir)
            files = _run_ytdlp(url=url, query=query, target_dir=target_dir)
        except subprocess.TimeoutExpired:
            _json_response(self, HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "error": "download timeout"})
            return
        except ValueError as e:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
            return
        except RuntimeError as e:
            _json_response(self, HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(e)})
            return

        notify_status = "disabled"
        if notify_tts:
            notify_status = _notify_tts(tts_text)

        _json_response(
            self,
            HTTPStatus.OK,
            {
                "ok": True,
                "saved_dir": str(target_dir),
                "safe_subdir": safe_rel,
                "files": files,
                "notify_tts": notify_tts,
                "notify_status": notify_status,
                "tts_text": tts_text if notify_tts else "",
                "message": "download completed",
            },
        )

    def log_message(self, fmt, *args):
        # Keep container logs concise.
        return


def main():
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8081"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
