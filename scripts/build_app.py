#!/usr/bin/env python3
"""
src/app.html 템플릿과 data/korea_bundle.json.gz (Base64)를 결합해
완전 자립형(오프라인/CSP 호환) 전국 시각화 HTML 파일을 생성합니다.
"""

import base64
import gzip
import json
import os
import sys

BASE_DIR = "/home/antigravity/gyeonggi-traffic-map"
TEMPLATE_PATH = os.path.join(BASE_DIR, "src", "app.html")
GZ_PATH = os.path.join(BASE_DIR, "data", "korea_bundle.json.gz")
OUT_HTML = os.path.join(BASE_DIR, "index.html")
PLACEHOLDER = "__KOREA_DATA_GZ_B64__"


def build(target_path: str):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    with open(GZ_PATH, "rb") as f:
        gz_bytes = f.read()

    # Validate gzipped JSON integrity
    parsed = json.loads(gzip.decompress(gz_bytes).decode("utf-8"))
    b64_str = base64.b64encode(gz_bytes).decode("ascii")

    if PLACEHOLDER not in tpl:
        raise ValueError(f"Placeholder {PLACEHOLDER} not found in {TEMPLATE_PATH}")

    out_html = tpl.replace(PLACEHOLDER, b64_str, 1)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(
        f"Built {target_path} ({os.path.getsize(target_path) / 1024:.1f} KB) "
        f"[sidos={len(parsed['sidos'])}, sggs={len(parsed['sggs'])}, dongs={len(parsed['dongs'])}, hex={parsed['hexGrid']['count']:,}]"
    )


if __name__ == "__main__":
    targets = sys.argv[1:] if len(sys.argv) > 1 else [OUT_HTML]
    for t in targets:
        build(t)
