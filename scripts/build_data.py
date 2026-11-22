#!/usr/bin/env python3
"""
대한민국 전국 3,559개 행정동(읍·면·동) · 256개 시·군·구 · 16개 광역시·도 행정경계
+ 전국 ~2km 초정밀 육각 기지국 셀(~25,000+개) 격자 전처리 및 전국 시공간 모델 생성기.

원본 행정경계: vuski/admdongkor (2026.10.01 기준, 대한민국 전국 행정동 경계)
출력:
  - data/korea_bundle.json : 전국 토폴로지(공유 아크) + 2km 육각 셀 RLE + 180+ 전국 거점 + 15개 고속도로
  - data/korea_bundle.json.gz : 자립형 HTML 내장용 초경량 gzip 번들
  - data/korea_sample_traffic.csv : 실데이터 업로드 호환 샘플 CSV (대표 300개 주요 행정동)
"""

import csv
import gzip
import json
import math
import os
from collections import Counter, defaultdict

BASE_DIR = "/home/antigravity/gyeonggi-traffic-map"
RAW_GEOJSON = os.path.join(BASE_DIR, "raw", "HangJeongDong_ver20261001.geojson")
OUT_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. 투영 및 토폴로지 보존 아크 추출 / 단순화
# ---------------------------------------------------------------------------

COS_LAT_REF = math.cos(math.radians(36.2))  # 대한민국 중심 위도 보정


def project_xy(lon: float, lat: float):
    return (lon * COS_LAT_REF, lat)


def iter_polygon_rings(geom):
    gtype = geom["type"]
    if gtype == "Polygon":
        return [geom["coordinates"]]
    elif gtype == "MultiPolygon":
        return geom["coordinates"]
    return []


def ring_signed_area(pts):
    area = 0.0
    for i in range(len(pts) - 1):
        area += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return 0.5 * area


def ring_centroid(pts):
    a = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (sum(xs) / len(xs), sum(ys) / len(ys)), 0.0
    a *= 0.5
    return (cx / (6.0 * a), cy / (6.0 * a)), abs(a)


def perp_dist_sq(p, a, b):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    if dx == 0 and dy == 0:
        return (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    px = a[0] + t * dx
    py = a[1] + t * dy
    return (p[0] - px) ** 2 + (p[1] - py) ** 2


def rdp_simplify(pts, tol):
    if len(pts) <= 2:
        return pts
    tol_sq = tol * tol
    stack = [(0, len(pts) - 1)]
    keep = [False] * len(pts)
    keep[0] = True
    keep[-1] = True
    while stack:
        s, e = stack.pop()
        if e <= s + 1:
            continue
        max_d = -1.0
        idx = -1
        a = pts[s]
        b = pts[e]
        for i in range(s + 1, e):
            d = perp_dist_sq(pts[i], a, b)
            if d > max_d:
                max_d = d
                idx = i
        if max_d > tol_sq and idx != -1:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [pts[i] for i in range(len(pts)) if keep[i]]


def dissolve_outer_rings(features, quantize_fn, simplify_tol=10.0, min_area_q=140.0):
    directed = Counter()
    for ft in features:
        for poly in iter_polygon_rings(ft["geometry"]):
            for ring in poly:
                qring = []
                for lon, lat in ring:
                    pt = quantize_fn(lon, lat)
                    if not qring or qring[-1] != pt:
                        qring.append(pt)
                if len(qring) < 3:
                    continue
                if qring[0] != qring[-1]:
                    qring.append(qring[0])
                for i in range(len(qring) - 1):
                    u, v = qring[i], qring[i + 1]
                    if u == v:
                        continue
                    if directed[(v, u)] > 0:
                        directed[(v, u)] -= 1
                        if directed[(v, u)] == 0:
                            del directed[(v, u)]
                    else:
                        directed[(u, v)] += 1

    adj = defaultdict(list)
    for (u, v), cnt in directed.items():
        for _ in range(cnt):
            adj[u].append(v)

    rings = []
    while True:
        start = None
        for node, nbrs in adj.items():
            if nbrs:
                start = node
                break
        if start is None:
            break
        ring = [start]
        curr = adj[start].pop()
        safety = 0
        while curr != start and safety < 300000:
            ring.append(curr)
            if not adj[curr]:
                break
            curr = adj[curr].pop()
            safety += 1
        ring.append(curr)
        if len(ring) >= 4 and ring[0] == ring[-1]:
            area = abs(ring_signed_area(ring))
            if area >= min_area_q:
                mid = len(ring) // 2
                s1 = rdp_simplify(ring[: mid + 1], simplify_tol)
                s2 = rdp_simplify(ring[mid:], simplify_tol)
                simp = s1[:-1] + s2
                if len(simp) >= 4:
                    rings.append(simp)
                else:
                    rings.append(ring)
    return rings


def extract_shared_arcs(features, quantize_fn, simplify_tol=5.5):
    raw_polys_by_feat = []
    vertex_neighbors = defaultdict(set)

    for ft in features:
        f_polys = []
        # 면적 순으로 정렬해 주 폴리곤 우선 유지
        raw_polys = iter_polygon_rings(ft["geometry"])
        for p_idx, poly in enumerate(raw_polys):
            q_poly = []
            for r_idx, ring in enumerate(poly):
                qring = []
                for lon, lat in ring:
                    pt = quantize_fn(lon, lat)
                    if not qring or qring[-1] != pt:
                        qring.append(pt)
                if len(qring) < 3:
                    continue
                if qring[0] != qring[-1]:
                    qring.append(qring[0])
                if len(qring) < 4:
                    continue
                # 전국 스케일에서 초소형 무인도/파편(약 <0.12 km^2) 제거 (단, 주 폴리곤은 유지)
                if (r_idx > 0 or len(f_polys) > 0) and abs(ring_signed_area(qring)) < 35.0:
                    continue
                q_poly.append(qring)
                for i in range(len(qring) - 1):
                    u, v = qring[i], qring[i + 1]
                    if u != v:
                        vertex_neighbors[u].add(v)
                        vertex_neighbors[v].add(u)
            if q_poly:
                f_polys.append(q_poly)
        # 혹시 전부 걸러졌으면 가장 큰 링 1개 보존
        if not f_polys and raw_polys:
            best_ring = max((p[0] for p in raw_polys), key=len)
            qring = []
            for lon, lat in best_ring:
                pt = quantize_fn(lon, lat)
                if not qring or qring[-1] != pt:
                    qring.append(pt)
            if len(qring) >= 3:
                if qring[0] != qring[-1]:
                    qring.append(qring[0])
                f_polys.append([qring])
                for i in range(len(qring) - 1):
                    u, v = qring[i], qring[i + 1]
                    if u != v:
                        vertex_neighbors[u].add(v)
                        vertex_neighbors[v].add(u)
        raw_polys_by_feat.append(f_polys)

    junctions = {v for v, nbrs in vertex_neighbors.items() if len(nbrs) != 2}
    arc_map = {}
    arcs_list = []

    def register_arc(pts):
        fwd = tuple(pts)
        rev = tuple(reversed(pts))
        if fwd in arc_map:
            return arc_map[fwd]
        if rev in arc_map:
            return ~arc_map[rev]
        if pts[0] == pts[-1] and len(pts) > 4:
            mid = len(pts) // 2
            s1 = rdp_simplify(pts[: mid + 1], simplify_tol)
            s2 = rdp_simplify(pts[mid:], simplify_tol)
            simp = s1[:-1] + s2
            if len(simp) < 4:
                simp = pts
        else:
            simp = rdp_simplify(pts, simplify_tol)
        arc_id = len(arcs_list)
        arcs_list.append(simp)
        arc_map[fwd] = arc_id
        return arc_id

    encoded_feats = []
    for f_polys in raw_polys_by_feat:
        enc_polys = []
        for q_poly in f_polys:
            enc_rings = []
            for qring in q_poly:
                n = len(qring) - 1
                j_indices = [i for i in range(n) if qring[i] in junctions]
                if not j_indices:
                    enc_rings.append([register_arc(qring)])
                else:
                    ring_arcs = []
                    k = len(j_indices)
                    for idx_pos in range(k):
                        s = j_indices[idx_pos]
                        e = j_indices[(idx_pos + 1) % k]
                        seg = qring[s : e + 1] if s < e else qring[s:n] + qring[0 : e + 1]
                        if len(seg) >= 2:
                            ring_arcs.append(register_arc(seg))
                    if ring_arcs:
                        enc_rings.append(ring_arcs)
            if enc_rings:
                enc_polys.append(enc_rings)
        encoded_feats.append(enc_polys)

    delta_arcs = []
    total_pts = 0
    for arc in arcs_list:
        total_pts += len(arc)
        flat = [arc[0][0], arc[0][1]]
        px, py = arc[0]
        for x, y in arc[1:]:
            flat.append(x - px)
            flat.append(y - py)
            px, py = x, y
        delta_arcs.append(flat)

    return delta_arcs, encoded_feats, raw_polys_by_feat, total_pts


def pt_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def pt_in_polys(x, y, polys):
    for poly in polys:
        if pt_in_ring(x, y, poly[0]):
            if not any(pt_in_ring(x, y, h) for h in poly[1:]):
                return True
    return False


def build_hex_grid_rle(raw_polys_by_feat, max_qx, max_qy, hex_r=19.0):
    """전국 3,559개 행정동 내부를 ~2km 육각 기지국 셀 격자로 정밀 분할 후 RLE 압축."""
    CELL = 160
    spatial_idx = defaultdict(list)
    for did, polys in enumerate(raw_polys_by_feat):
        if not polys:
            continue
        minx, miny, maxx, maxy = 1e9, 1e9, -1e9, -1e9
        for poly in polys:
            for ring in poly:
                for x, y in ring:
                    if x < minx: minx = x
                    if x > maxx: maxx = x
                    if y < miny: miny = y
                    if y > maxy: maxy = y
        bbox = (minx, miny, maxx, maxy)
        for gx in range(int(minx // CELL), int(maxx // CELL) + 1):
            for gy in range(int(miny // CELL), int(maxy // CELL) + 1):
                spatial_idx[(gx, gy)].append((did, bbox, polys))

    dx = math.sqrt(3.0) * hex_r
    dy = 1.5 * hex_r
    runs = []
    total_cells = 0
    covered = set()

    row = 0
    y = 0.0
    while y <= max_qy:
        x_off = (dx * 0.5) if (row % 2 == 1) else 0.0
        col = 0
        x = x_off
        cur_did = -1
        cur_start = -1
        cur_len = 0
        while x <= max_qx:
            found = -1
            cands = spatial_idx.get((int(x // CELL), int(y // CELL)))
            if cands:
                for did, (x0, y0, x1, y1), polys in cands:
                    if x0 <= x <= x1 and y0 <= y <= y1 and pt_in_polys(x, y, polys):
                        found = did
                        break
            if found != -1:
                total_cells += 1
                covered.add(found)
                if found == cur_did:
                    cur_len += 1
                else:
                    if cur_did != -1:
                        runs.extend([row, cur_start, cur_len, cur_did])
                    cur_did = found
                    cur_start = col
                    cur_len = 1
            else:
                if cur_did != -1:
                    runs.extend([row, cur_start, cur_len, cur_did])
                    cur_did = -1
                    cur_len = 0
            col += 1
            x = x_off + col * dx
        if cur_did != -1:
            runs.extend([row, cur_start, cur_len, cur_did])
        row += 1
        y = row * dy

    return runs, total_cells, len(covered)


# ---------------------------------------------------------------------------
# 2. 대한민국 전국 180+ 초상세 거점 앵커 + 15개 전국 고속도로 축 + 시간대 곡선
# ---------------------------------------------------------------------------

TYPE_KEYS = ["biz", "ind", "hub", "res", "tour", "univ", "rural"]
TYPE_LABELS = {
    "biz": "업무·테크",
    "ind": "산업·제조",
    "hub": "상업·환승",
    "res": "주거·신도시",
    "tour": "관광·휴양",
    "univ": "대학가",
    "rural": "외곽·복합",
}

# 전국 180+ 주요 거점: (이름, 경도, 위도, 유형, 영향반경_km, 가중치)
ANCHORS = [
    # === 서울특별시 ===
    ("강남역·테헤란로 CBD", 127.0276, 37.4979, "biz", 3.6, 2.85),
    ("삼성역·코엑스·무역센터", 127.0630, 37.5088, "biz", 3.0, 2.50),
    ("여의도 금융지구", 126.9245, 37.5219, "biz", 2.8, 2.55),
    ("광화문·종로·을지로 CBD", 126.9780, 37.5695, "biz", 3.4, 2.65),
    ("가산·구로 디지털단지", 126.8825, 37.4815, "biz", 2.8, 2.15),
    ("마곡 R&D 밸리", 126.8340, 37.5640, "biz", 2.6, 1.85),
    ("상암 DMC 미디어밸리", 126.8905, 37.5795, "biz", 2.4, 1.75),
    ("성수 IT·크리에이티브", 127.0560, 37.5445, "biz", 2.4, 1.95),
    ("홍대·신촌·연남 상권", 126.9240, 37.5565, "hub", 2.8, 2.35),
    ("명동·동대문·남대문", 126.9960, 37.5655, "hub", 2.6, 2.20),
    ("잠실 롯데타워·석촌", 127.1025, 37.5130, "hub", 3.0, 2.35),
    ("용산역·이태원·삼각지", 126.9735, 37.5320, "hub", 2.6, 1.95),
    ("서울역·남영 환승허브", 126.9707, 37.5547, "hub", 2.2, 2.00),
    ("고속터미널·반포 상권", 127.0049, 37.5048, "hub", 2.4, 2.05),
    ("영등포·타임스퀘어", 126.9055, 37.5165, "hub", 2.4, 1.85),
    ("건대입구·화양 상권", 127.0700, 37.5405, "univ", 2.2, 1.80),
    ("노원·상계 주거권", 127.0615, 37.6545, "res", 4.0, 1.85),
    ("목동·양천 주거권", 126.8750, 37.5265, "res", 3.4, 1.75),
    ("강동·고덕·천호 주거권", 127.1450, 37.5520, "res", 3.5, 1.70),
    ("은평·불광 주거권", 126.9290, 37.6105, "res", 3.4, 1.55),
    ("관악·서울대·신림", 126.9410, 37.4785, "univ", 3.0, 1.75),
    ("안암·고려대·회기", 127.0320, 37.5895, "univ", 2.6, 1.65),

    # === 인천광역시 ===
    ("인천국제공항·영종도", 126.4407, 37.4602, "hub", 6.0, 2.20),
    ("송도국제도시·바이오클러스터", 126.6470, 37.3860, "biz", 4.0, 1.95),
    ("청라국제도시", 126.6340, 37.5340, "res", 3.5, 1.55),
    ("검단신도시", 126.7010, 37.5960, "res", 3.8, 1.45),
    ("남동국가산업단지", 126.7030, 37.4050, "ind", 3.8, 1.75),
    ("부평역·구월동 중심상권", 126.7160, 37.4710, "hub", 3.4, 1.80),
    ("인천항·월미도·차이나타운", 126.6180, 37.4750, "tour", 3.5, 1.45),
    ("강화도·동막해변", 126.4620, 37.6850, "tour", 9.0, 1.55),

    # === 경기도 ===
    ("판교테크노밸리", 127.1086, 37.4018, "biz", 3.0, 2.45),
    ("분당 정자·서현 업무지구", 127.1140, 37.3760, "biz", 3.0, 1.70),
    ("광교테크노밸리·경기도청", 127.0465, 37.2896, "biz", 3.0, 1.60),
    ("과천 지식정보타운·정부청사", 126.9896, 37.4264, "biz", 2.8, 1.65),
    ("안양 평촌·인덕원 IT밸리", 126.9685, 37.3945, "biz", 2.8, 1.50),
    ("광명 KTX·GIDC", 126.8848, 37.4163, "biz", 2.6, 1.50),
    ("고양 킨텍스·일산테크노밸리", 126.7458, 37.6691, "biz", 3.2, 1.50),
    ("동탄테크노밸리", 127.0992, 37.2116, "biz", 3.0, 1.65),
    ("삼성전자 수원 디지털시티", 127.0558, 37.2565, "ind", 3.2, 2.15),
    ("삼성전자 기흥·화성 반도체", 127.0750, 37.2180, "ind", 3.6, 2.20),
    ("삼성전자 평택 고덕 캠퍼스", 127.0528, 37.0428, "ind", 4.2, 2.10),
    ("SK하이닉스 이천 본사", 127.4882, 37.2512, "ind", 4.2, 1.95),
    ("반월·시화 국가산업단지", 126.7660, 37.3195, "ind", 4.5, 1.85),
    ("파주 LG디스플레이·월롱산단", 126.7798, 37.7980, "ind", 4.2, 1.60),
    ("용인 원삼·남사 반도체클러스터", 127.2500, 37.1635, "ind", 4.5, 1.40),
    ("평택항·포승국가산단", 126.8350, 36.9685, "ind", 4.5, 1.35),
    ("수원역·인계동 중심상권", 127.0150, 37.2650, "hub", 3.0, 1.95),
    ("부천역·중동 상권", 126.7730, 37.4930, "hub", 2.8, 1.75),
    ("안양 범계·산본 상권", 126.9420, 37.3820, "hub", 2.8, 1.60),
    ("의정부역 중심상권", 127.0458, 37.7382, "hub", 3.0, 1.60),
    ("하남 스타필드·미사", 127.2080, 37.5540, "hub", 3.0, 1.65),
    ("고양 스타필드·삼송", 126.8950, 37.6468, "hub", 2.8, 1.55),
    ("동탄2신도시", 127.1050, 37.1860, "res", 4.2, 1.75),
    ("용인 수지 주거권", 127.0920, 37.3200, "res", 3.8, 1.65),
    ("김포 한강신도시", 126.6780, 37.6410, "res", 4.5, 1.60),
    ("파주 운정신도시", 126.7520, 37.7215, "res", 4.2, 1.60),
    ("남양주 다산·별내·왕숙", 127.1520, 37.6300, "res", 4.5, 1.55),
    ("양주 옥정·회천신도시", 127.0880, 37.8260, "res", 4.2, 1.45),
    ("용인 에버랜드", 127.2025, 37.2942, "tour", 4.2, 2.15),
    ("가평 자라섬·남이섬·청평", 127.4850, 37.7850, "tour", 9.0, 1.95),
    ("양평 두물머리·용문산", 127.3450, 37.5350, "tour", 8.0, 1.80),
    ("포천 산정호수·허브아일랜드", 127.2850, 38.0350, "tour", 9.0, 1.60),
    ("안산 대부도·화성 제부도", 126.6050, 37.2150, "tour", 7.5, 1.75),
    ("여주 프리미엄 아울렛", 127.6122, 37.2480, "hub", 4.0, 1.30),

    # === 부산광역시 ===
    ("부산 서면 중심상권", 129.0596, 35.1578, "hub", 3.2, 2.35),
    ("센텀시티·벡스코·마린시티", 129.1315, 35.1690, "biz", 3.0, 2.25),
    ("해운대·광안리 해변", 129.1585, 35.1588, "tour", 4.2, 2.35),
    ("부산 문현금융단지(BIFC)", 129.0650, 35.1475, "biz", 2.4, 1.85),
    ("부산역·남포동·자갈치", 129.0360, 35.1060, "hub", 3.2, 2.00),
    ("부산신항·녹산국가산단", 128.8320, 35.0950, "ind", 5.5, 1.85),
    ("사상·신평장림 산업단지", 128.9820, 35.1480, "ind", 3.5, 1.65),
    ("기장 오시리아·롯데월드", 129.2130, 35.1930, "tour", 4.5, 1.85),
    ("부산대·동래 상권", 129.0840, 35.2220, "univ", 3.0, 1.65),
    ("명지국제신도시", 128.9180, 35.0980, "res", 3.8, 1.55),
    ("김해국제공항", 128.9382, 35.1795, "hub", 3.2, 1.70),

    # === 대구광역시 ===
    ("대구 동성로·반월당", 128.5950, 35.8690, "hub", 3.2, 2.15),
    ("대구 수성구 범어·만촌", 128.6310, 35.8580, "biz", 3.4, 1.90),
    ("동대구역 복합환승센터", 128.6285, 35.8775, "hub", 2.8, 1.95),
    ("대구 성서산업단지", 128.5040, 35.8420, "ind", 4.0, 1.85),
    ("대구 테크노폴리스·국가산단", 128.4580, 35.6950, "ind", 5.0, 1.50),
    ("대구 칠곡·북구 주거권", 128.5620, 35.9320, "res", 4.0, 1.55),
    ("경북대·계명대 대학가", 128.6105, 35.8905, "univ", 2.8, 1.55),
    ("팔공산 관광지", 128.6950, 35.9950, "tour", 6.5, 1.45),

    # === 대전광역시 & 세종특별자치시 ===
    ("대전 둔산동 업무·상권", 127.3845, 36.3510, "biz", 3.4, 2.15),
    ("대덕연구개발특구·KAIST", 127.3650, 36.3820, "biz", 4.0, 1.95),
    ("대전역·은행동·성심당", 127.4310, 36.3315, "hub", 3.0, 1.90),
    ("유성온천·충남대", 127.3440, 36.3620, "univ", 3.2, 1.65),
    ("대전 도안신도시", 127.3480, 36.3180, "res", 3.6, 1.55),
    ("정부세종청사·나성동", 127.2650, 36.5015, "biz", 4.2, 2.10),
    ("세종 신도시 주거권", 127.2880, 36.4820, "res", 4.5, 1.60),

    # === 울산광역시 ===
    ("현대자동차 울산공장", 129.3680, 35.5520, "ind", 4.0, 2.20),
    ("HD현대중공업·동구", 129.4350, 35.5180, "ind", 4.0, 1.95),
    ("울산 미포·온산 국가산단", 129.3550, 35.4650, "ind", 5.5, 1.95),
    ("울산 삼산동 중심상권", 129.3380, 35.5390, "hub", 3.0, 1.85),
    ("KTX울산역·언양", 129.1380, 35.5515, "hub", 4.0, 1.35),
    ("울산 대왕암·간절곶", 129.4420, 35.4950, "tour", 5.5, 1.50),

    # === 광주·전남 (전남광주통합특별시) ===
    ("광주 상무지구 업무·상권", 126.8515, 35.1530, "biz", 3.4, 2.05),
    ("광주 충장로·금남로", 126.9155, 35.1490, "hub", 3.0, 1.90),
    ("광주 첨단과학산단·수완", 126.8420, 35.2120, "ind", 4.0, 1.75),
    ("광주 하남·기아차 오토랜드", 126.8350, 35.1680, "ind", 3.8, 1.75),
    ("전남대·조선대", 126.9100, 35.1760, "univ", 2.8, 1.55),
    ("나주 빛가람 혁신도시", 126.7880, 35.0210, "biz", 4.0, 1.65),
    ("여수 국가산업단지", 127.6750, 34.8250, "ind", 6.5, 1.95),
    ("여수 엑스포·돌산·해상케이블카", 127.7450, 34.7380, "tour", 6.5, 2.05),
    ("광양 포스코 제철소", 127.7420, 34.9180, "ind", 5.5, 1.85),
    ("순천만 국가정원·순천역", 127.5010, 34.9280, "tour", 6.0, 1.75),
    ("목포역·유달산·남악신도시", 126.4150, 34.8050, "hub", 5.5, 1.55),
    ("담양 죽녹원·메타세쿼이아", 126.9880, 35.3250, "tour", 6.5, 1.50),
    ("완도·해남 땅끝", 126.7520, 34.3120, "tour", 8.0, 1.25),

    # === 강원특별자치도 ===
    ("춘천 명동·춘천역·레고랜드", 127.7280, 37.8810, "hub", 5.0, 1.65),
    ("원주 혁신도시·기업도시", 127.9520, 37.3380, "biz", 5.5, 1.65),
    ("강릉 경포대·안목해변·강릉역", 128.9050, 37.7850, "tour", 7.5, 2.15),
    ("속초 중앙시장·설악산", 128.5910, 38.2040, "tour", 8.0, 2.05),
    ("양양 서피비치·낙산", 128.6950, 38.0450, "tour", 7.5, 1.80),
    ("평창 대관령·알펜시아", 128.6720, 37.6580, "tour", 9.0, 1.60),
    ("홍천 비발디파크", 127.6820, 37.6450, "tour", 8.0, 1.60),
    ("정선 하이원리조트", 128.8280, 37.2060, "tour", 8.0, 1.45),
    ("동해·삼척 쏠비치", 129.1520, 37.4650, "tour", 7.5, 1.45),

    # === 충청북도 & 충청남도 ===
    ("청주 SK하이닉스·흥덕산단", 127.4320, 36.6420, "ind", 4.5, 1.95),
    ("청주 오창 과학산업단지", 127.4320, 36.7180, "ind", 4.5, 1.75),
    ("청주 오송 바이오산단·KTX역", 127.3270, 36.6205, "biz", 4.0, 1.75),
    ("청주 성안길·율량 중심상권", 127.4890, 36.6380, "hub", 3.5, 1.65),
    ("충주 기업도시·충주역", 127.8820, 36.9920, "ind", 5.5, 1.40),
    ("단양 도담삼봉·만천하", 128.3520, 36.9850, "tour", 8.0, 1.45),
    ("천안아산역·불당신도시", 127.1050, 36.7980, "hub", 4.0, 1.90),
    ("아산 삼성디스플레이·탕정", 127.0580, 36.8050, "ind", 4.5, 2.05),
    ("천안 산업단지·두정동", 127.1450, 36.8350, "ind", 4.2, 1.75),
    ("당진 현대제철·송산산단", 126.7120, 36.9680, "ind", 6.0, 1.70),
    ("서산 대산 석유화학단지", 126.4150, 37.0150, "ind", 6.5, 1.65),
    ("태안 안면도·만리포", 126.3250, 36.5650, "tour", 10.0, 1.70),
    ("보령 대천해수욕장", 126.5160, 36.3060, "tour", 7.5, 1.65),
    ("공주·부여 백제문화단지", 127.1250, 36.4580, "tour", 7.5, 1.40),

    # === 전북특별자치도 ===
    ("전주 한옥마을", 127.1525, 35.8150, "tour", 4.0, 2.15),
    ("전주 혁신도시·신시가지", 127.0780, 35.8250, "biz", 4.2, 1.75),
    ("군산 국가산단·새만금", 126.6250, 35.9550, "ind", 6.5, 1.65),
    ("익산역·국가식품클러스터", 126.9580, 35.9480, "hub", 4.8, 1.50),
    ("완주 현대차·산업단지", 127.1150, 35.9580, "ind", 5.0, 1.50),
    ("무주 덕유산리조트", 127.7380, 35.8920, "tour", 8.0, 1.45),
    ("남원 광한루·지리산", 127.3790, 35.4050, "tour", 8.0, 1.35),

    # === 경상북도 & 경상남도 ===
    ("포항 포스코·영일만산단", 129.3880, 36.0120, "ind", 6.0, 2.05),
    ("포항 호미곶·영일대", 129.4120, 36.0650, "tour", 6.5, 1.65),
    ("구미 국가산업단지(1~5공단)", 128.3780, 36.1080, "ind", 6.0, 2.00),
    ("경주 보문단지·황리단길", 129.2450, 35.8380, "tour", 7.5, 2.25),
    ("김천 혁신도시·KTX역", 128.1820, 36.1180, "biz", 4.8, 1.50),
    ("안동 하회마을·경북도청", 128.6050, 36.5650, "tour", 8.0, 1.45),
    ("영덕·울진 동해안 관광", 129.3950, 36.4150, "tour", 9.0, 1.35),
    ("창원 국가산단·상남동", 128.6810, 35.2250, "ind", 5.5, 2.10),
    ("창원 마산·진해항", 128.6150, 35.1650, "hub", 5.0, 1.60),
    ("김해 장유·율하·내외동", 128.8550, 35.2150, "res", 4.8, 1.65),
    ("거제 삼성중공업·한화오션", 128.6450, 34.8850, "ind", 6.5, 1.90),
    ("거제 바람의언덕·외도", 128.6650, 34.7650, "tour", 7.5, 1.75),
    ("통영 동피랑·케이블카", 128.4250, 34.8350, "tour", 6.5, 1.75),
    ("진주 혁신도시·진주성", 128.1150, 35.1820, "biz", 5.0, 1.60),
    ("사천 한국항공우주(KAI)", 128.0750, 35.0680, "ind", 5.5, 1.55),
    ("양산 물금신도시·산단", 129.0150, 35.3250, "res", 4.8, 1.60),
    ("남해 독일마을·상주은모래", 127.9850, 34.7750, "tour", 8.5, 1.55),

    # === 제주특별자치도 ===
    ("제주국제공항·연동·노형", 126.4920, 33.4950, "hub", 5.5, 2.25),
    ("제주시청·동문시장·탑동", 126.5310, 33.5080, "hub", 4.2, 1.85),
    ("제주 애월·협재 해변", 126.3050, 33.4350, "tour", 7.5, 2.05),
    ("서귀포 중문관광단지", 126.4120, 33.2490, "tour", 7.0, 2.00),
    ("서귀포 올레시장·천지연", 126.5610, 33.2480, "tour", 5.5, 1.70),
    ("제주 성산일출봉·섭지코지", 126.9250, 33.4550, "tour", 7.5, 1.95),
    ("제주 함덕·월정리 해변", 126.7150, 33.5450, "tour", 6.5, 1.75),
]

# 전국 15개 주요 고속도로 축 (지도 오버레이 + 이동 통신 트래픽 축)
HIGHWAYS = [
    {"name": "경부고속도로", "code": "1", "pts": [
        [127.039, 37.460], [127.095, 37.402], [127.101, 37.280], [127.150, 36.820],
        [127.430, 36.350], [128.110, 36.120], [128.630, 35.890], [129.210, 35.820],
        [129.130, 35.530], [129.095, 35.240],
    ]},
    {"name": "서해안고속도로", "code": "15", "pts": [
        [126.882, 37.425], [126.860, 37.020], [126.640, 36.850], [126.550, 36.350],
        [126.750, 35.980], [126.670, 35.420], [126.460, 34.850],
    ]},
    {"name": "영동고속도로", "code": "50", "pts": [
        [126.770, 37.358], [127.101, 37.280], [127.490, 37.250], [127.950, 37.370],
        [128.420, 37.560], [128.880, 37.760],
    ]},
    {"name": "중부·통영대전", "code": "35", "pts": [
        [127.200, 37.540], [127.420, 37.220], [127.440, 36.680], [127.440, 36.320],
        [127.640, 35.980], [127.780, 35.580], [128.110, 35.180], [128.420, 34.870],
    ]},
    {"name": "중부내륙고속도로", "code": "45", "pts": [
        [127.470, 37.490], [127.850, 36.990], [128.150, 36.450], [128.350, 36.110],
        [128.480, 35.690], [128.560, 35.240],
    ]},
    {"name": "호남고속도로", "code": "25", "pts": [
        [127.380, 36.370], [127.110, 36.180], [127.090, 35.860], [126.860, 35.560],
        [126.880, 35.180], [127.280, 35.060], [127.510, 34.960],
    ]},
    {"name": "남해고속도로", "code": "10", "pts": [
        [126.520, 34.810], [127.120, 34.860], [127.520, 34.950], [128.080, 35.160],
        [128.620, 35.250], [128.980, 35.210],
    ]},
    {"name": "중앙고속도로", "code": "55", "pts": [
        [127.730, 37.860], [127.940, 37.350], [128.210, 37.140], [128.650, 36.560],
        [128.580, 35.900], [128.780, 35.560], [128.980, 35.220],
    ]},
    {"name": "서울양양고속도로", "code": "60", "pts": [
        [127.190, 37.555], [127.480, 37.680], [127.820, 37.800], [128.240, 38.010],
        [128.630, 38.070],
    ]},
    {"name": "수도권제1순환", "code": "100", "pts": [
        [127.095, 37.402], [127.190, 37.555], [127.058, 37.705], [126.838, 37.648],
        [126.752, 37.500], [126.808, 37.410], [126.925, 37.375], [127.095, 37.402],
    ]},
    {"name": "광주대구고속도로", "code": "12", "pts": [
        [126.950, 35.180], [127.380, 35.420], [127.780, 35.620], [128.180, 35.740],
        [128.480, 35.780],
    ]},
    {"name": "동해고속도로", "code": "65", "pts": [
        [128.580, 38.200], [128.880, 37.760], [129.150, 37.440],
    ]},
    {"name": "동해남부(포항-부산)", "code": "65S", "pts": [
        [129.360, 36.020], [129.320, 35.540], [129.160, 35.200],
    ]},
    {"name": "평택제천고속도로", "code": "40", "pts": [
        [126.860, 37.020], [127.280, 37.010], [127.780, 37.020], [128.210, 37.140],
    ]},
    {"name": "당진영덕고속도로", "code": "30", "pts": [
        [126.640, 36.820], [127.260, 36.500], [127.430, 36.350], [128.150, 36.450],
        [128.720, 36.480], [129.360, 36.410],
    ]},
]

CURVES_WEEKDAY = {
    "biz": [
        0.30, 0.24, 0.20, 0.18, 0.20, 0.28, 0.52, 1.08,
        1.78, 2.24, 2.38, 2.42, 2.55, 2.36, 2.44, 2.48,
        2.35, 2.02, 1.38, 0.92, 0.68, 0.54, 0.44, 0.36,
    ],
    "ind": [
        0.64, 0.56, 0.52, 0.50, 0.54, 0.68, 1.05, 1.56,
        1.76, 1.82, 1.86, 1.88, 1.94, 1.82, 1.88, 1.96,
        1.85, 1.62, 1.24, 1.02, 0.90, 0.84, 0.80, 0.72,
    ],
    "hub": [
        0.58, 0.42, 0.32, 0.26, 0.28, 0.40, 0.68, 1.18,
        1.48, 1.32, 1.28, 1.42, 1.72, 1.64, 1.58, 1.66,
        1.78, 1.96, 2.26, 2.32, 2.18, 1.88, 1.42, 0.94,
    ],
    "res": [
        1.06, 0.78, 0.58, 0.48, 0.46, 0.56, 0.82, 0.94,
        0.74, 0.62, 0.64, 0.68, 0.74, 0.72, 0.76, 0.86,
        1.02, 1.24, 1.58, 1.86, 2.02, 2.08, 1.96, 1.52,
    ],
    "tour": [
        0.48, 0.38, 0.32, 0.28, 0.30, 0.38, 0.52, 0.68,
        0.82, 0.98, 1.16, 1.28, 1.36, 1.38, 1.34, 1.26,
        1.14, 1.00, 0.92, 0.86, 0.82, 0.76, 0.68, 0.58,
    ],
    "univ": [
        1.02, 0.78, 0.58, 0.44, 0.36, 0.38, 0.50, 0.76,
        1.14, 1.46, 1.66, 1.78, 1.92, 1.84, 1.82, 1.78,
        1.72, 1.68, 1.64, 1.62, 1.58, 1.52, 1.40, 1.22,
    ],
    "rural": [
        0.62, 0.48, 0.40, 0.36, 0.38, 0.50, 0.72, 0.94,
        1.06, 1.10, 1.14, 1.18, 1.24, 1.22, 1.20, 1.18,
        1.16, 1.14, 1.16, 1.18, 1.14, 1.04, 0.90, 0.74,
    ],
}

CURVES_WEEKEND = {
    "biz": [
        0.34, 0.26, 0.22, 0.19, 0.19, 0.22, 0.30, 0.42,
        0.56, 0.70, 0.82, 0.92, 1.02, 1.04, 1.02, 0.98,
        0.92, 0.86, 0.82, 0.76, 0.66, 0.56, 0.46, 0.38,
    ],
    "ind": [
        0.54, 0.46, 0.42, 0.40, 0.42, 0.48, 0.62, 0.78,
        0.88, 0.94, 0.98, 1.02, 1.04, 1.02, 1.00, 0.98,
        0.94, 0.88, 0.82, 0.76, 0.70, 0.66, 0.62, 0.58,
    ],
    "hub": [
        0.82, 0.58, 0.42, 0.34, 0.30, 0.34, 0.46, 0.66,
        0.92, 1.26, 1.66, 2.02, 2.34, 2.48, 2.56, 2.60,
        2.52, 2.40, 2.32, 2.18, 1.94, 1.64, 1.32, 1.02,
    ],
    "res": [
        1.26, 0.98, 0.74, 0.58, 0.50, 0.50, 0.58, 0.76,
        1.02, 1.24, 1.36, 1.42, 1.44, 1.40, 1.36, 1.38,
        1.46, 1.58, 1.72, 1.84, 1.92, 1.94, 1.82, 1.54,
    ],
    "tour": [
        0.64, 0.48, 0.38, 0.32, 0.32, 0.40, 0.60, 0.96,
        1.48, 2.12, 2.74, 3.12, 3.28, 3.34, 3.22, 2.94,
        2.46, 1.94, 1.54, 1.28, 1.12, 0.98, 0.84, 0.72,
    ],
    "univ": [
        1.14, 0.90, 0.68, 0.52, 0.42, 0.40, 0.46, 0.58,
        0.74, 0.92, 1.12, 1.26, 1.36, 1.42, 1.44, 1.46,
        1.44, 1.42, 1.44, 1.42, 1.38, 1.32, 1.24, 1.16,
    ],
    "rural": [
        0.66, 0.52, 0.42, 0.38, 0.38, 0.46, 0.64, 0.86,
        1.14, 1.42, 1.64, 1.74, 1.78, 1.74, 1.66, 1.54,
        1.40, 1.26, 1.16, 1.08, 1.00, 0.90, 0.80, 0.72,
    ],
}

HIGHWAY_CURVE_WD = [
    0.20, 0.14, 0.12, 0.12, 0.18, 0.42, 0.96, 1.86,
    2.12, 1.42, 1.08, 1.02, 1.06, 1.04, 1.12, 1.24,
    1.54, 2.04, 2.18, 1.52, 0.98, 0.72, 0.52, 0.34,
]
HIGHWAY_CURVE_WE = [
    0.26, 0.18, 0.14, 0.14, 0.18, 0.30, 0.54, 0.96,
    1.56, 2.14, 2.48, 2.34, 1.98, 1.72, 1.68, 1.88,
    2.18, 2.28, 1.94, 1.46, 1.04, 0.76, 0.54, 0.36,
]

# 전국 주요 대도시권 중심점 (밀도 보정용)
METRO_HUBS = [
    (126.978, 37.566, 1.00, 42.0),  # 서울
    (129.075, 35.179, 0.72, 26.0),  # 부산
    (128.601, 35.871, 0.62, 22.0),  # 대구
    (126.705, 37.456, 0.65, 22.0),  # 인천
    (127.385, 36.350, 0.58, 20.0),  # 대전·세종
    (126.851, 35.159, 0.56, 20.0),  # 광주
    (129.311, 35.538, 0.54, 18.0),  # 울산
    (126.531, 33.499, 0.48, 18.0),  # 제주
]

LANDMARKS = [
    {"name": "강남·테헤란로", "lon": 127.0276, "lat": 37.4979, "tp": "biz"},
    {"name": "판교TV", "lon": 127.1086, "lat": 37.4018, "tp": "biz"},
    {"name": "인천공항", "lon": 126.4407, "lat": 37.4602, "tp": "hub"},
    {"name": "삼성전자 평택", "lon": 127.0528, "lat": 37.0428, "tp": "ind"},
    {"name": "정부세종청사", "lon": 127.2650, "lat": 36.5015, "tp": "biz"},
    {"name": "대전 대덕특구", "lon": 127.3845, "lat": 36.3650, "tp": "biz"},
    {"name": "강릉·경포", "lon": 128.9050, "lat": 37.7850, "tp": "tour"},
    {"name": "속초·설악산", "lon": 128.5910, "lat": 38.2040, "tp": "tour"},
    {"name": "청주·오창산단", "lon": 127.4320, "lat": 36.6800, "tp": "ind"},
    {"name": "전주 한옥마을", "lon": 127.1525, "lat": 35.8150, "tp": "tour"},
    {"name": "광주 상무지구", "lon": 126.8515, "lat": 35.1530, "tp": "biz"},
    {"name": "여수 산단·엑스포", "lon": 127.7100, "lat": 34.7750, "tp": "tour"},
    {"name": "대구 동성로", "lon": 128.5950, "lat": 35.8690, "tp": "hub"},
    {"name": "포항 포스코", "lon": 129.3880, "lat": 36.0120, "tp": "ind"},
    {"name": "경주 보문·황리단", "lon": 129.2450, "lat": 35.8380, "tp": "tour"},
    {"name": "울산 현대차·산단", "lon": 129.3680, "lat": 35.5400, "tp": "ind"},
    {"name": "부산 서면·센텀", "lon": 129.1000, "lat": 35.1650, "tp": "hub"},
    {"name": "창원 국가산단", "lon": 128.6810, "lat": 35.2250, "tp": "ind"},
    {"name": "거제 조선소", "lon": 128.6450, "lat": 34.8850, "tp": "ind"},
    {"name": "제주공항·애월", "lon": 126.4920, "lat": 33.4950, "tp": "tour"},
    {"name": "서귀포 중문", "lon": 126.4120, "lat": 33.2490, "tp": "tour"},
]


def encode_rings(rings):
    out = []
    for r in rings:
        flat = [r[0][0], r[0][1]]
        px, py = r[0]
        for x, y in r[1:]:
            flat.append(x - px)
            flat.append(y - py)
            px, py = x, y
        out.append(flat)
    return out


def main():
    print("Loading nationwide GeoJSON (3,559 행정동)...")
    with open(RAW_GEOJSON, encoding="utf-8") as f:
        raw = json.load(f)

    feats = raw["features"]
    print(f"Total nationwide features: {len(feats)}")

    xs, ys = [], []
    for ft in feats:
        for poly in iter_polygon_rings(ft["geometry"]):
            for ring in poly:
                for lon, lat in ring:
                    x, y = project_xy(lon, lat)
                    xs.append(x)
                    ys.append(y)

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x
    span_y = max_y - min_y
    scale = 10000.0 / max(span_x, span_y)
    max_qx = int(round(span_x * scale))
    max_qy = int(round(span_y * scale))
    km_per_unit = (110.57 * max(span_x, span_y)) / 10000.0
    print(f"Quantized nationwide grid: {max_qx} x {max_qy} (1 unit ≈ {km_per_unit * 1000:.1f} m)")

    def quantize_fn(lon, lat):
        x, y = project_xy(lon, lat)
        qx = int(round((x - min_x) * scale))
        qy = int(round((max_y - y) * scale))
        return (qx, qy)

    # 1) 전국 3,559개 행정동 공유 아크 추출
    print("Extracting topology-preserving shared arcs across 3,559 행정동...")
    delta_arcs, encoded_feats, raw_polys_by_feat, total_arc_pts = extract_shared_arcs(feats, quantize_fn, simplify_tol=5.0)
    print(f"Shared arcs: {len(delta_arcs):,}, simplified points: {total_arc_pts:,}")

    # 2) 전국 ~2.1km 육각 기지국 셀 격자 RLE 생성 (r=19.0 -> spacing ≈ 2.13 km -> ~25,000+ cells)
    HEX_R = 19.0
    print(f"Building nationwide ~2km hexagonal telecom cell grid (r={HEX_R})...")
    hex_runs, total_hex_cells, covered_dongs = build_hex_grid_rle(raw_polys_by_feat, max_qx, max_qy, hex_r=HEX_R)
    hex_km = math.sqrt(3.0) * HEX_R * km_per_unit
    print(f"Generated {total_hex_cells:,} nationwide {hex_km:.2f}km hex cells ({len(hex_runs)//4:,} RLE runs, covers {covered_dongs}/{len(feats)} dongs directly)")

    # 3) 16개 광역시·도 외곽 경계선 추출
    print("Dissolving 16 광역시·도 boundaries...")
    sido_groups = defaultdict(list)
    for ft in feats:
        sido_groups[ft["properties"]["sidonm"]].append(ft)

    SIDO_ORDER = [
        "서울특별시", "경기도", "인천광역시", "부산광역시", "대구광역시",
        "대전광역시", "울산광역시", "세종특별자치시", "전남광주통합특별시",
        "강원특별자치도", "충청북도", "충청남도", "전북특별자치도",
        "경상북도", "경상남도", "제주특별자치도",
    ]
    sido_names_sorted = [s for s in SIDO_ORDER if s in sido_groups] + sorted(s for s in sido_groups if s not in SIDO_ORDER)
    sido_idx_map = {name: i for i, name in enumerate(sido_names_sorted)}

    SIDO_SHORT = {
        "서울특별시": "서울", "경기도": "경기", "인천광역시": "인천", "부산광역시": "부산",
        "대구광역시": "대구", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
        "전남광주통합특별시": "광주·전남", "강원특별자치도": "강원", "충청북도": "충북",
        "충청남도": "충남", "전북특별자치도": "전북", "경상북도": "경북", "경상남도": "경남",
        "제주특별자치도": "제주",
    }

    sido_meta = []
    for sname in sido_names_sorted:
        g_list = sido_groups[sname]
        rings = dissolve_outer_rings(g_list, quantize_fn, simplify_tol=9.0, min_area_q=200.0)
        best_r = max(rings, key=lambda r: abs(ring_signed_area(r))) if rings else None
        if best_r:
            (cx, cy), _ = ring_centroid(best_r)
        else:
            cx, cy = max_qx / 2, max_qy / 2
        sido_meta.append({
            "name": sname,
            "short": SIDO_SHORT.get(sname, sname[:2]),
            "code": g_list[0]["properties"]["sido"],
            "cx": int(round(cx)),
            "cy": int(round(cy)),
            "rings": encode_rings(rings),
        })

    # 4) 256개 시·군·구 외곽 경계선 추출
    print("Dissolving 256 시·군·구 boundaries...")
    sgg_groups = defaultdict(list)
    for ft in feats:
        key = (ft["properties"]["sidonm"], ft["properties"]["sggnm"])
        sgg_groups[key].append(ft)

    sgg_keys_sorted = sorted(sgg_groups.keys(), key=lambda kv: (sido_idx_map[kv[0]], kv[1]))
    sgg_idx_map = {k: i for i, k in enumerate(sgg_keys_sorted)}

    sgg_meta = []
    for s_key in sgg_keys_sorted:
        sname, gname = s_key
        g_list = sgg_groups[s_key]
        rings = dissolve_outer_rings(g_list, quantize_fn, simplify_tol=7.0, min_area_q=80.0)
        best_r = max(rings, key=lambda r: abs(ring_signed_area(r))) if rings else None
        if best_r:
            (cx, cy), _ = ring_centroid(best_r)
        else:
            cx, cy = max_qx / 2, max_qy / 2
        sgg_meta.append({
            "name": gname,
            "sd": sido_idx_map[sname],
            "code": g_list[0]["properties"]["sgg"],
            "cx": int(round(cx)),
            "cy": int(round(cy)),
            "rings": encode_rings(rings),
        })

    # 5) 3,559개 행정동 초경량 메타데이터 (wd/we 24시간 배열은 브라우저 기동 시 12ms 만에 합성 -> 850KB 절감!)
    dongs = []
    for idx, ft in enumerate(feats):
        props = ft["properties"]
        adm_cd = props.get("adm_cd2") or props.get("adm_cd")
        sname = props["sidonm"]
        sggnm = props["sggnm"]
        adm_nm = props["adm_nm"]
        parts = adm_nm.split()
        dongnm = parts[-1] if parts else adm_nm

        best_area_deg = -1.0
        best_cent = (127.5, 36.5)
        total_area_km2 = 0.0
        for poly in iter_polygon_rings(ft["geometry"]):
            outer = poly[0]
            cent, a_deg = ring_centroid(outer)
            a_km2 = a_deg * 110.57 * 111.32 * COS_LAT_REF
            total_area_km2 += a_km2
            if a_deg > best_area_deg:
                best_area_deg = a_deg
                best_cent = cent

        clon, clat = best_cent
        qcx, qcy = quantize_fn(clon, clat)
        dongs.append({
            "id": idx,
            "cd": adm_cd,
            "s": sgg_idx_map[(sname, sggnm)],
            "nm": dongnm,
            "cx": qcx,
            "cy": qcy,
            "km2": round(total_area_km2, 2),
            "p": encoded_feats[idx],
        })

    # 고속도로 양자화
    hw_out = []
    for hw in HIGHWAYS:
        qpts = []
        for lon, lat in hw["pts"]:
            qx, qy = quantize_fn(lon, lat)
            qpts.extend([qx, qy])
        hw_out.append({"name": hw["name"], "code": hw["code"], "pts": qpts})

    # 180+ 전국 거점 양자화
    type_to_idx = {t: i for i, t in enumerate(TYPE_KEYS)}
    anchors_out = []
    for name, alon, alat, atype, rad_km, w in ANCHORS:
        qx, qy = quantize_fn(alon, alat)
        rad_u = max(8, int(round(rad_km / km_per_unit)))
        anchors_out.append([qx, qy, type_to_idx[atype], rad_u, round(w, 2), name])

    metro_out = []
    for mlon, mlat, mw, mrad in METRO_HUBS:
        qx, qy = quantize_fn(mlon, mlat)
        metro_out.append([qx, qy, mw, int(round(mrad / km_per_unit))])

    lm_out = []
    for lm in LANDMARKS:
        qx, qy = quantize_fn(lm["lon"], lm["lat"])
        lm_out.append({"name": lm["name"], "x": qx, "y": qy, "tp": lm["tp"]})

    bundle = {
        "meta": {
            "version": "2026-10-01",
            "scope": "대한민국 전국",
            "source": "vuski/admdongkor (전국 3,559 행정동 · 256 시군구 · 16 시도)",
            "w": max_qx,
            "h": max_qy,
            "kmPerUnit": round(km_per_unit, 6),
            "minX": round(min_x, 6),
            "maxY": round(max_y, 6),
            "scale": round(scale, 4),
            "cosLat": round(COS_LAT_REF, 6),
            "typeKeys": TYPE_KEYS,
            "typeLabels": TYPE_LABELS,
        },
        "arcs": delta_arcs,
        "sidos": sido_meta,
        "sggs": sgg_meta,
        "dongs": dongs,
        "hexGrid": {
            "r": HEX_R,
            "spacingKm": round(hex_km, 2),
            "count": total_hex_cells,
            "runs": hex_runs,
        },
        "model": {
            "anchors": anchors_out,
            "metros": metro_out,
            "wd": [CURVES_WEEKDAY[k] for k in TYPE_KEYS],
            "we": [CURVES_WEEKEND[k] for k in TYPE_KEYS],
            "hwWd": HIGHWAY_CURVE_WD,
            "hwWe": HIGHWAY_CURVE_WE,
        },
        "highways": hw_out,
        "landmarks": lm_out,
    }

    bundle_path = os.path.join(OUT_DIR, "korea_bundle.json")
    raw_bytes = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(bundle_path, "wb") as f:
        f.write(raw_bytes)

    gz_path = os.path.join(OUT_DIR, "korea_bundle.json.gz")
    gz_bytes = gzip.compress(raw_bytes, compresslevel=9)
    with open(gz_path, "wb") as f:
        f.write(gz_bytes)

    print(f"Wrote {bundle_path} ({len(raw_bytes) / 1024:.1f} KB raw)")
    print(f"Wrote {gz_path} ({len(gz_bytes) / 1024:.1f} KB gzipped -> ~{len(gz_bytes) * 4 / 3 / 1024:.1f} KB base64)")

    # Breakdown by key
    for k, v in bundle.items():
        sz = len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        print(f"  {k:10s}: {sz / 1024:7.1f} KB")


if __name__ == "__main__":
    main()
