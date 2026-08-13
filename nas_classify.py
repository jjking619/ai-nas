#!/usr/bin/env python3
"""NAS 照片语义分类工具（方案 B：基于 immich CLIP 向量，不依赖文件名）。

对每张图片：
1. 调 immich ML 服务的 CLIP 视觉编码，得到图片向量
2. 用英文描述性类别词做 CLIP 文本编码，得到类别向量
3. 计算余弦相似度，取最高者为建议类别

纯标准库实现（urllib），可在 openclaw 容器内直接运行：
  python3 /nas_share/tools/nas_classify.py /nas_share/家庭相册/*.jpg
"""
import argparse
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ML_URL = "http://immich-machine-learning:3003"
MODEL = "ViT-B-32__openai"
CONNECT_TIMEOUT = 3
PREDICT_TIMEOUT = 120
_ML_READY_CHECKED = False

# 类别 -> 英文描述（CLIP 对英文描述的分类效果远好于中文短词）
CATEGORIES = {
    "人物": "a photo of a person, people, portrait, human face",
    "动物": "a photo focused on an animal subject, pet, cat, dog, bird, wildlife",
    "美食": "a photo of food, meal, dish, restaurant",
    "风景": "a photo of landscape scenery, nature view, mountain, sea, beach, river, lake, sunset sky, blue sky, cloudy sky, cloudscape, skyline, horizon, sun rays, cloud reflection, waterscape",
    "植物": "a photo of plants, flowers, trees, garden",
    "建筑": "a photo of buildings, architecture, city, urban",
    "交通工具": "a photo focused on a vehicle, car, bus, train, motorcycle, bicycle, airplane, ship, transportation",
    "日常用品": "a photo of everyday objects, household items",
}


def _assert_ml_service_ready() -> None:
    global _ML_READY_CHECKED
    if _ML_READY_CHECKED:
        return

    parsed = urllib.parse.urlparse(ML_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if not host:
        raise RuntimeError(f"Invalid ML_URL: {ML_URL}")

    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            _ML_READY_CHECKED = True
    except OSError as e:
        raise RuntimeError(
            f"Immich ML服务不可达 ({host}:{port}): {e}. "
            "请先执行: docker start immich-machine-learning"
        ) from e


def _predict(entries: dict, text=None, image=None) -> dict:
    _assert_ml_service_ready()

    boundary = "----nasclassify"
    parts = []
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"entries\"\r\n\r\n"
            f"{json.dumps(entries)}"
        ).encode()
    )
    if text is not None:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n"
                f"{text}"
            ).encode()
        )
    if image is not None:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                f"filename=\"img.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
            ).encode()
            + image
        )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        f"{ML_URL}/predict",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PREDICT_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Immich ML请求失败: {e}. "
            "请检查 immich-machine-learning 容器状态。"
        ) from e


def _embedding(resp: dict):
    v = resp.get("clip")
    if isinstance(v, str):
        return json.loads(v)
    if isinstance(v, list):
        return v
    raise RuntimeError(f"Unexpected ML clip response: {str(resp)[:200]}")


def _encode_text(text: str):
    entries = {"clip": {"textual": {"modelName": MODEL, "options": {}}}}
    return _embedding(_predict(entries, text=text))


def _encode_image(img_bytes: bytes):
    entries = {"clip": {"visual": {"modelName": MODEL}}}
    return _embedding(_predict(entries, image=img_bytes))


def _cosine(a, b):
    import math

    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def classify(img_bytes: bytes, cat_vecs: dict):
    img_vec = _encode_image(img_bytes)
    scores = {cat: _cosine(img_vec, v) for cat, v in cat_vecs.items()}
    return sorted(scores.items(), key=lambda x: -x[1])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="图片路径")
    ap.add_argument("--dir", help="目录，遍历其中图片")
    ap.add_argument("--recursive", action="store_true", help="递归子目录")
    args = ap.parse_args()

    files = [Path(p) for p in args.paths]
    if args.dir:
        d = Path(args.dir)
        it = d.rglob("*") if args.recursive else d.glob("*")
        files += [f for f in it if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]

    if not files:
        print("没有找到图片", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 编码 {len(CATEGORIES)} 个类别文本向量...", file=sys.stderr)
    try:
        cat_vecs = {cat: _encode_text(desc) for cat, desc in CATEGORIES.items()}
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    for f in files:
        try:
            img = f.read_bytes()
            top = classify(img, cat_vecs)
            best, score = top[0]
            top3 = " ".join(f"{k}={v:.3f}" for k, v in top[:3])
            print(f"{f}\t建议: {best} (score={score:.3f})\t{top3}")
        except Exception as e:  # noqa: BLE001
            print(f"{f}\tERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
