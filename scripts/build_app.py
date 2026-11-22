#!/usr/bin/env python3
"""
src/app.html 템플릿과 data/gyeonggi_bundle.json을 결합해
완전 자립형(오프라인/CSP 호환) 시각화 HTML 파일을 생성합니다.
"""

import json
import os
import sys

BASE_DIR = "/home/antigravity/gyeonggi-traffic-map"
TEMPLATE_PATH = os.path.join(BASE_DIR, "src", "app.html")
BUNDLE_PATH = os.path.join(BASE_DIR, "data", "gyeonggi_bundle.json")
OUT_HTML = os.path.join(BASE_DIR, "index.html")
PLACEHOLDER = "__GYEONGGI_DATA_JSON__"


def build(target_path: str):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    with open(BUNDLE_PATH, encoding="utf-8") as f:
        bundle_str = f.read()

    # Validate JSON and escape </script> sequences safely
    json.loads(bundle_str)
    safe_json = bundle_str.replace("</", "<\\/")

    if PLACEHOLDER not in tpl:
        raise ValueError(f"Placeholder {PLACEHOLDER} not found in {TEMPLATE_PATH}")

    out_html = tpl.replace(PLACEHOLDER, safe_json, 1)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"Built {target_path} ({os.path.getsize(target_path) / 1024:.1f} KB)")


if __name__ == "__main__":
    targets = sys.argv[1:] if len(sys.argv) > 1 else [OUT_HTML]
    for t in targets:
        build(t)
