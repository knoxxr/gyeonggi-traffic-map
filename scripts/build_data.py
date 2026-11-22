#!/usr/bin/env python3
"""
경기도 602개 행정동(읍·면·동) 경계 + 1km 초정밀 육각 기지국 셀(11,000+개) 격자 전처리
및 시간대별(1시간/15분) 모바일 통신 트래픽 시공간 모델 생성기.

원본 행정경계: vuski/admdongkor (2026.10.01 기준, 대한민국 행정동 경계)
출력:
  - data/gyeonggi_bundle.json : 602 행정동 공유 아크 + 1km 육각 셀 RLE 격자 + 115+ 초상세 거점 앵커 + 트래픽 모델
  - data/gyeonggi_sample_traffic.csv : 실데이터 업로드 포맷 호환 샘플 CSV
"""

import csv
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

COS_LAT_REF = math.cos(math.radians(37.55))


def project_xy(lon: float, lat: float):
    """경기도 국소 등거리 원통 투영 (종횡비 왜곡 제거)."""
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


def dissolve_outer_rings(features, quantize_fn, simplify_tol=12.0, min_area_q=250.0):
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
        while curr != start and safety < 200000:
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
    return rings


def extract_shared_arcs(gg_features, quantize_fn, simplify_tol=7.5):
    raw_polys_by_feat = []
    vertex_neighbors = defaultdict(set)

    for ft in gg_features:
        f_polys = []
        for poly in iter_polygon_rings(ft["geometry"]):
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
                if r_idx > 0 or len(f_polys) > 0:
                    if abs(ring_signed_area(qring)) < 35.0:
                        continue
                q_poly.append(qring)
                for i in range(len(qring) - 1):
                    u, v = qring[i], qring[i + 1]
                    if u != v:
                        vertex_neighbors[u].add(v)
                        vertex_neighbors[v].add(u)
            if q_poly:
                f_polys.append(q_poly)
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
                    aid = register_arc(qring)
                    enc_rings.append([aid])
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


def build_hex_grid_rle(raw_polys_by_feat, max_qx, max_qy, hex_r=38.0):
    """
    경기도 전역 602개 행정동 내부를 ~1.0km 육각 기지국 셀 격자(r=38 grid units)로 정밀 분할하고
    런-길이 인코딩(RLE: [row, colStart, len, dongId, ...])으로 경량 저장.
    """
    CELL = 250
    spatial_idx = defaultdict(list)
    dong_bboxes = []
    for did, polys in enumerate(raw_polys_by_feat):
        minx, miny, maxx, maxy = 1e9, 1e9, -1e9, -1e9
        for poly in polys:
            for ring in poly:
                for x, y in ring:
                    if x < minx: minx = x
                    if x > maxx: maxx = x
                    if y < miny: miny = y
                    if y > maxy: maxy = y
        bbox = (minx, miny, maxx, maxy)
        dong_bboxes.append(bbox)
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
# 2. 경기도 115+ 초상세 하위 거점 앵커 및 24시간/15분 시공간 트래픽 모델
# ---------------------------------------------------------------------------

TYPE_KEYS = ["biz", "ind", "hub", "res", "tour", "univ", "rural"]
TYPE_LABELS = {
    "biz": "업무·테크",
    "ind": "산업·반도체",
    "hub": "상업·환승",
    "res": "주거·신도시",
    "tour": "관광·휴양",
    "univ": "대학가",
    "rural": "외곽·복합",
}

# (이름, 경도, 위도, 유형, 영향반경_km, 가중치) — 1km / 400m 초미세 셀 해상도 대응 115+ 거점
ANCHORS = [
    # --- 업무·테크 허브 (biz) ---
    ("판교테크노밸리 제1TV", 127.1086, 37.4018, "biz", 2.0, 2.55),
    ("판교 제2테크노밸리", 127.0938, 37.4125, "biz", 1.8, 2.05),
    ("판교 알파돔·판교역", 127.1112, 37.3948, "biz", 1.6, 1.95),
    ("분당 정자 네이버·업무지구", 127.1055, 37.3595, "biz", 1.8, 1.75),
    ("분당 서현·수내 오피스", 127.1235, 37.3848, "biz", 2.0, 1.55),
    ("광교테크노밸리·경기도청", 127.0465, 37.2896, "biz", 2.2, 1.60),
    ("광교 법조타운", 127.0655, 37.2970, "biz", 1.8, 1.25),
    ("과천 지식정보타운", 126.9825, 37.4165, "biz", 1.9, 1.65),
    ("정부과천청사 업무지구", 126.9905, 37.4268, "biz", 1.8, 1.55),
    ("안양 평촌 스마트스퀘어", 126.9685, 37.3945, "biz", 2.0, 1.50),
    ("안양 인덕원 IT밸리", 126.9768, 37.4015, "biz", 1.7, 1.35),
    ("광명 KTX·GIDC 업무지구", 126.8848, 37.4163, "biz", 2.0, 1.55),
    ("광명 하안 테크노밸리", 126.8855, 37.4605, "biz", 1.7, 1.20),
    ("고양 킨텍스·방송영상밸리", 126.7458, 37.6691, "biz", 2.4, 1.45),
    ("고양 일산 테크노밸리", 126.7520, 37.6565, "biz", 1.9, 1.20),
    ("동탄테크노밸리", 127.0992, 37.2116, "biz", 2.4, 1.65),
    ("하남 미사 지식산업센터", 127.1930, 37.5612, "biz", 1.9, 1.35),
    ("용인 플랫폼시티·구성", 127.1095, 37.3042, "biz", 2.2, 1.25),
    ("구리·다산 현대프리미어캠퍼스", 127.1575, 37.6195, "biz", 2.0, 1.15),
    ("수원 시청·인계 업무지구", 127.0286, 37.2636, "biz", 2.0, 1.30),
    ("의정부 경기도청 북부청사", 127.0725, 37.7460, "biz", 2.0, 1.05),
    ("성남 하이테크밸리(상대원)", 127.1695, 37.4368, "biz", 2.0, 1.40),
    ("부천 춘의·도당 테크노파크", 126.7875, 37.5085, "biz", 2.0, 1.35),
    ("안양 명학·금정 지식산업센터", 126.9465, 37.3765, "biz", 1.8, 1.25),

    # --- 첨단 산업단지·반도체 클러스터 (ind) ---
    ("삼성전자 수원 디지털시티", 127.0558, 37.2565, "ind", 2.4, 2.25),
    ("삼성전자 기흥 캠퍼스", 127.0810, 37.2265, "ind", 2.4, 2.15),
    ("삼성전자 화성 캠퍼스", 127.0680, 37.2065, "ind", 2.6, 2.20),
    ("삼성전자 평택 고덕 캠퍼스", 127.0528, 37.0428, "ind", 3.2, 2.15),
    ("SK하이닉스 이천 본사", 127.4882, 37.2512, "ind", 3.4, 2.00),
    ("반월 국가산업단지", 126.7920, 37.3160, "ind", 3.2, 1.85),
    ("시화 국가산업단지·MTV", 126.7260, 37.3260, "ind", 3.4, 1.80),
    ("파주 LG디스플레이·월롱산단", 126.7798, 37.7980, "ind", 3.5, 1.65),
    ("파주 출판도시·문발산단", 126.6890, 37.7115, "ind", 2.4, 1.25),
    ("용인 원삼 반도체클러스터", 127.2890, 37.1635, "ind", 3.6, 1.40),
    ("용인 남사 첨단시스템반도체", 127.1850, 37.1360, "ind", 3.4, 1.30),
    ("평택 포승 국가산단·평택항", 126.8350, 36.9685, "ind", 3.8, 1.35),
    ("평택 송탄·칠괴 산업단지", 127.0880, 37.0540, "ind", 2.8, 1.30),
    ("화성 향남·발안 산업단지", 126.9150, 37.1250, "ind", 3.2, 1.25),
    ("화성 마도·남양 산업단지", 126.7980, 37.2050, "ind", 3.0, 1.15),
    ("김포 양촌·학운 골드밸리", 126.6250, 37.6150, "ind", 3.2, 1.20),
    ("오산 가장 산업단지", 127.0320, 37.1520, "ind", 2.4, 1.20),
    ("안성 제1~4 일반산업단지", 127.2350, 37.0150, "ind", 3.4, 1.10),
    ("화성 기아 오토랜드(우정)", 126.7980, 37.0380, "ind", 3.2, 1.30),
    ("포천 용정·양문 산업단지", 127.1850, 37.9050, "ind", 3.2, 1.05),

    # --- 상업·환승·번화가 허브 (hub) ---
    ("수원역 환승·AK·롯데몰", 127.0016, 37.2659, "hub", 2.1, 2.05),
    ("수원 스타필드·화서역", 126.9912, 37.2875, "hub", 1.8, 1.65),
    ("수원 인계동 중심상권", 127.0325, 37.2640, "hub", 1.8, 1.65),
    ("부천역·심곡 번화가", 126.7830, 37.4840, "hub", 2.0, 1.80),
    ("부천 중동·상동 중심상권", 126.7630, 37.5030, "hub", 2.1, 1.70),
    ("안양 범계 로데오 상권", 126.9508, 37.3898, "hub", 1.9, 1.75),
    ("안양역 일번가 상권", 126.9225, 37.4015, "hub", 1.8, 1.55),
    ("의정부역·신세계 상권", 127.0458, 37.7382, "hub", 2.2, 1.65),
    ("성남 모란·야탑 환승허브", 127.1292, 37.4220, "hub", 2.0, 1.65),
    ("고양 일산 라페스타·웨스턴돔", 126.7695, 37.6585, "hub", 2.2, 1.65),
    ("고양 화정역 중심상권", 126.8325, 37.6345, "hub", 1.9, 1.45),
    ("고양 스타필드·삼송", 126.8950, 37.6468, "hub", 2.1, 1.65),
    ("안산 중앙역 중심상권", 126.8388, 37.3158, "hub", 2.1, 1.55),
    ("안산 원곡·안산역 환승권", 126.7895, 37.3270, "hub", 1.9, 1.35),
    ("평택역·소사벌 중심상권", 127.0858, 36.9908, "hub", 2.4, 1.50),
    ("평택 지제역 SRT 환승허브", 127.0702, 37.0188, "hub", 2.0, 1.45),
    ("구리역·돌다리 상권", 127.1436, 37.6033, "hub", 1.9, 1.50),
    ("군포 산본 로데오 중심상권", 126.9334, 37.3596, "hub", 1.9, 1.45),
    ("하남 스타필드", 127.2238, 37.5455, "hub", 2.2, 1.75),
    ("하남 미사역 중심상권", 127.1930, 37.5632, "hub", 1.7, 1.45),
    ("광명 철산 중심상권", 126.8680, 37.4760, "hub", 1.8, 1.55),
    ("광명 이케아·코스트코·롯데몰", 126.8825, 37.4235, "hub", 1.8, 1.55),
    ("동탄역 GTX·롯데백화점", 127.0965, 37.2005, "hub", 2.1, 1.80),
    ("용인 수지구청·죽전 신세계", 127.1078, 37.3248, "hub", 2.0, 1.55),
    ("용인 기흥역·AK상권", 127.1158, 37.2755, "hub", 1.9, 1.40),
    ("여주 프리미엄 아울렛", 127.6122, 37.2480, "hub", 3.0, 1.35),
    ("파주 프리미엄 아울렛·헤이리", 126.6975, 37.7720, "hub", 2.8, 1.35),
    ("시흥 프리미엄 아울렛·배곧중심", 126.7370, 37.3795, "hub", 2.2, 1.40),
    ("김포 구래·장기 중심상권", 126.6285, 37.6455, "hub", 2.2, 1.40),
    ("남양주 다산·별내 중심상권", 127.1490, 37.6265, "hub", 2.2, 1.45),
    ("파주 운정중앙역 GTX·야당상권", 126.7575, 37.7130, "hub", 2.2, 1.45),
    ("오산역·운암 상권", 127.0675, 37.1458, "hub", 2.0, 1.35),
    ("이천 터미널·중리 상권", 127.4425, 37.2795, "hub", 2.2, 1.25),
    ("경기광주역·경안 상권", 127.2535, 37.4105, "hub", 2.2, 1.30),

    # --- 대규모 통근 신도시·주거밀집 (res) ---
    ("화성 동탄2 호수공원·남동탄", 127.1045, 37.1695, "res", 3.2, 1.75),
    ("화성 동탄1 주거단지", 127.0740, 37.2035, "res", 2.8, 1.60),
    ("화성 병점·봉담 주거권", 126.9750, 37.2160, "res", 3.2, 1.40),
    ("용인 수지 상현·성복·풍덕천", 127.0810, 37.3170, "res", 3.2, 1.65),
    ("용인 동백·보라 주거권", 127.1480, 37.2750, "res", 3.0, 1.40),
    ("김포 한강신도시 주거권", 126.6780, 37.6410, "res", 3.8, 1.60),
    ("김포 풍무·사우·고촌 주거권", 126.7260, 37.6020, "res", 3.0, 1.50),
    ("파주 운정신도시 주거권", 126.7480, 37.7240, "res", 3.6, 1.60),
    ("남양주 다산·왕숙 주거권", 127.1620, 37.6240, "res", 3.4, 1.55),
    ("남양주 별내·진접 주거권", 127.1360, 37.6850, "res", 3.8, 1.45),
    ("남양주 화도·마석 주거권", 127.3050, 37.6520, "res", 3.2, 1.30),
    ("하남 미사강변 주거권", 127.1850, 37.5630, "res", 2.6, 1.55),
    ("하남·성남 위례신도시", 127.1440, 37.4750, "res", 2.6, 1.60),
    ("시흥 배곧·은계·장현 신도시", 126.7620, 37.4150, "res", 3.5, 1.45),
    ("양주 옥정·회천신도시", 127.0880, 37.8260, "res", 3.8, 1.45),
    ("광주 태전·오포 주거권", 127.2320, 37.3750, "res", 3.5, 1.35),
    ("오산 세교신도시", 127.0490, 37.1770, "res", 2.8, 1.35),
    ("성남 분당 주거권(이매·정자)", 127.1260, 37.3650, "res", 2.8, 1.55),
    ("수원 호매실·금곡 주거권", 126.9560, 37.2600, "res", 3.0, 1.50),
    ("수원 영통·망포 주거권", 127.0620, 37.2450, "res", 2.6, 1.50),
    ("의왕 백운밸리·포일·내손", 126.9930, 37.3770, "res", 2.8, 1.35),
    ("고양 일산 주거권(주엽·마두)", 126.7680, 37.6690, "res", 3.2, 1.55),
    ("고양 덕은·향동·창릉 주거권", 126.8780, 37.6060, "res", 2.8, 1.45),
    ("의정부 민락·고산신도시", 127.0980, 37.7420, "res", 3.2, 1.45),
    ("평택 고덕국제신도시 주거권", 127.0320, 37.0450, "res", 3.2, 1.45),

    # --- 주말 관광·레저·자연 (tour) ---
    ("용인 에버랜드·캐리비안베이", 127.2025, 37.2942, "tour", 3.4, 2.25),
    ("용인 한국민속촌", 127.1170, 37.2585, "tour", 2.2, 1.50),
    ("과천 서울대공원·서울랜드", 127.0168, 37.4342, "tour", 2.5, 1.95),
    ("가평 자라섬·남이섬·가평읍", 127.5150, 37.8150, "tour", 6.5, 2.10),
    ("가평 청평호·쁘띠프랑스", 127.4320, 37.7180, "tour", 6.0, 1.80),
    ("가평 아침고요수목원", 127.3520, 37.7450, "tour", 5.5, 1.65),
    ("양평 두물머리·세미원", 127.3180, 37.5340, "tour", 5.5, 1.95),
    ("양평 용문산 관광지", 127.5750, 37.5420, "tour", 6.5, 1.60),
    ("포천 산정호수", 127.3220, 38.0650, "tour", 6.5, 1.75),
    ("포천 허브아일랜드·아트밸리", 127.1620, 37.9650, "tour", 6.0, 1.50),
    ("안산 대부도·방아머리", 126.5820, 37.2450, "tour", 5.8, 1.95),
    ("화성 제부도·궁평항", 126.6450, 37.1630, "tour", 6.0, 1.70),
    ("광주 화담숲·곤지암리조트", 127.2930, 37.3380, "tour", 4.2, 1.70),
    ("연천 한탄강·재인폭포", 127.0880, 38.0810, "tour", 7.0, 1.40),
    ("김포 애기봉·대명항", 126.5550, 37.6800, "tour", 5.2, 1.40),
    ("남양주 팔당·북한강변", 127.2950, 37.5900, "tour", 4.5, 1.65),
    ("파주 임진각·마장호수", 126.7400, 37.8890, "tour", 6.0, 1.50),
    ("수원 화성행궁·광교호수공원", 127.0155, 37.2825, "tour", 2.0, 1.45),

    # --- 대학가 (univ) ---
    ("아주대·경기대", 127.0445, 37.2830, "univ", 1.8, 1.35),
    ("성균관대 자연과학캠퍼스", 126.9750, 37.2935, "univ", 1.8, 1.40),
    ("경희대 국제캠퍼스", 127.0815, 37.2430, "univ", 1.8, 1.30),
    ("단국대 죽전캠퍼스", 127.1270, 37.3215, "univ", 1.8, 1.30),
    ("한양대 ERICA", 126.8345, 37.2975, "univ", 2.0, 1.30),
    ("가천대·동서울대", 127.1285, 37.4508, "univ", 1.6, 1.30),
    ("한국항공대", 126.8650, 37.6005, "univ", 1.6, 1.15),
    ("명지대·용인대", 127.1870, 37.2240, "univ", 2.0, 1.20),
]

HIGHWAYS = [
    {
        "name": "경부고속도로",
        "code": "1",
        "pts": [
            [127.039, 37.460], [127.095, 37.402], [127.105, 37.345],
            [127.101, 37.280], [127.095, 37.215], [127.087, 37.152],
            [127.130, 37.065], [127.155, 36.960],
        ],
    },
    {
        "name": "수도권제1순환",
        "code": "100",
        "pts": [
            [127.095, 37.402], [127.168, 37.485], [127.190, 37.555],
            [127.145, 37.628], [127.058, 37.705], [126.942, 37.685],
            [126.838, 37.648], [126.765, 37.590], [126.752, 37.500],
            [126.808, 37.410], [126.925, 37.375], [127.030, 37.392],
            [127.095, 37.402],
        ],
    },
    {
        "name": "영동고속도로",
        "code": "50",
        "pts": [
            [126.770, 37.358], [126.865, 37.322], [126.995, 37.308],
            [127.101, 37.280], [127.220, 37.250], [127.395, 37.245],
            [127.550, 37.255], [127.685, 37.280],
        ],
    },
    {
        "name": "서해안고속도로",
        "code": "15",
        "pts": [
            [126.882, 37.425], [126.862, 37.355], [126.875, 37.245],
            [126.885, 37.130], [126.860, 37.020], [126.835, 36.940],
        ],
    },
    {
        "name": "서울양양고속도로",
        "code": "60",
        "pts": [
            [127.190, 37.555], [127.280, 37.610], [127.420, 37.670],
            [127.555, 37.710],
        ],
    },
    {
        "name": "자유로·평택파주",
        "code": "17",
        "pts": [
            [126.845, 37.588], [126.742, 37.665], [126.715, 37.765],
            [126.750, 37.845],
        ],
    },
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


def haversine_km(lon1, lat1, lon2, lat2):
    dx = (lon1 - lon2) * 111.32 * COS_LAT_REF
    dy = (lat1 - lat2) * 110.57
    return math.hypot(dx, dy)


def min_dist_to_highways_km(lon, lat):
    best = 999.0
    for hw in HIGHWAYS:
        pts = hw["pts"]
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            ax, ay = x1 * 111.32 * COS_LAT_REF, y1 * 110.57
            bx, by = x2 * 111.32 * COS_LAT_REF, y2 * 110.57
            px, py = lon * 111.32 * COS_LAT_REF, lat * 110.57
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d < best:
                best = d
    return best


def det_hash(s: str) -> float:
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 100000) / 100000.0


def synthesize_dong_traffic(adm_cd, sggnm, dongnm, lon, lat, area_km2):
    weights = defaultdict(float)
    nearest_anchor = None
    best_anchor_score = 0.0

    for name, alon, alat, atype, rad, w in ANCHORS:
        d = haversine_km(lon, lat, alon, alat)
        if d < rad * 2.8:
            infl = w * math.exp(-0.5 * (d / rad) ** 2)
            weights[atype] += infl
            if infl > best_anchor_score:
                best_anchor_score = infl
                nearest_anchor = name

    if any(k in dongnm for k in ["삼평", "백현", "이의", "영천"]):
        weights["biz"] += 1.4
    if any(k in dongnm for k in ["동탄", "미사", "운정", "다산", "별내", "배곧", "옥정", "위례", "세교", "호매실", "광교"]):
        weights["res"] += 1.1
    if any(k in dongnm for k in ["고덕", "부발", "원시", "정왕", "월롱", "포승", "농서", "반월", "향남"]):
        weights["ind"] += 1.2
    if any(k in dongnm for k in ["대부", "포곡", "청평", "설악", "양서", "서신", "화도", "도척", "북면", "상면"]):
        weights["tour"] += 1.2
    if dongnm.endswith("면"):
        weights["rural"] += 0.95
    elif dongnm.endswith("읍"):
        weights["rural"] += 0.35
        weights["res"] += 0.45
    else:
        weights["res"] += 0.55

    d_seoul = min(haversine_km(lon, lat, 126.978, 37.566), haversine_km(lon, lat, 127.028, 37.498))
    metro_factor = math.exp(-d_seoul / 38.0)

    d_hw = min_dist_to_highways_km(lon, lat)
    hw_w = 0.28 * math.exp(-0.5 * (d_hw / 2.0) ** 2)

    density_factor = 1.0 / (0.35 + (max(area_km2, 0.35) ** 0.34))

    total_w = sum(weights.values())
    if total_w < 1e-6:
        weights["rural"] = 1.0
        total_w = 1.0

    primary_type = max(weights.items(), key=lambda kv: kv[1])[0]

    r_seed = det_hash(adm_cd)
    r_jitter = 0.88 + 0.24 * r_seed
    anchor_boost = 1.0 + 0.85 * min(best_anchor_score, 2.5)
    base_scale = (2.6 + 13.5 * metro_factor) * density_factor * anchor_boost * r_jitter
    if dongnm.endswith("면"):
        base_scale *= 0.52
    elif dongnm.endswith("읍"):
        base_scale *= 0.84
    base_scale = max(0.85, min(42.0, base_scale))

    wd = []
    we = []
    phase_noise = (det_hash(adm_cd + "_p") - 0.5) * 0.08
    for h in range(24):
        v_wd = 0.0
        v_we = 0.0
        for t, wt in weights.items():
            share = wt / total_w
            v_wd += share * CURVES_WEEKDAY[t][h]
            v_we += share * CURVES_WEEKEND[t][h]
        v_wd = v_wd * (1.0 - hw_w * 0.45) + hw_w * HIGHWAY_CURVE_WD[h]
        v_we = v_we * (1.0 - hw_w * 0.45) + hw_w * HIGHWAY_CURVE_WE[h]

        h_wave = 1.0 + 0.055 * math.sin((h + r_seed * 12.0) * 0.72) + phase_noise * math.cos(h * 0.45)
        wd.append(round(max(0.12, base_scale * v_wd * h_wave), 2))
        we.append(round(max(0.12, base_scale * v_we * h_wave), 2))

    g5_base = {"biz": 74, "hub": 70, "univ": 72, "res": 64, "ind": 61, "tour": 54, "rural": 43}[primary_type]
    g5_pct = int(round(max(32, min(86, g5_base + 8 * metro_factor + (r_seed - 0.5) * 8))))

    mix_map = {
        "biz":   [28, 22, 38, 12],
        "ind":   [34, 24, 28, 14],
        "hub":   [38, 30, 14, 18],
        "res":   [52, 26, 12, 10],
        "tour":  [34, 28,  8, 30],
        "univ":  [48, 30, 14,  8],
        "rural": [40, 26, 10, 24],
    }
    mix = list(mix_map[primary_type])
    if hw_w > 0.14:
        shift = min(6, int(round(hw_w * 24)))
        mix[3] += shift
        mix[0] -= shift

    return {
        "type": primary_type,
        "typeLabel": TYPE_LABELS[primary_type],
        "anchor": nearest_anchor if best_anchor_score >= 0.40 else "",
        "g5": g5_pct,
        "mix": mix,
        "wd": wd,
        "we": we,
    }


def main():
    print("Loading raw GeoJSON...")
    with open(RAW_GEOJSON, encoding="utf-8") as f:
        raw = json.load(f)

    feats = raw["features"]
    gg_feats = [ft for ft in feats if ft["properties"].get("sidonm") == "경기도"]
    seoul_feats = [ft for ft in feats if ft["properties"].get("sidonm") == "서울특별시"]
    incheon_feats = [ft for ft in feats if ft["properties"].get("sidonm") == "인천광역시"]

    xs, ys = [], []
    for ft in gg_feats:
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
    print(f"Quantized grid: {max_qx} x {max_qy} (1 unit ≈ {km_per_unit * 1000:.1f} m)")

    def quantize_fn(lon, lat):
        x, y = project_xy(lon, lat)
        qx = int(round((x - min_x) * scale))
        qy = int(round((max_y - y) * scale))
        return (qx, qy)

    # 1) 경기도 602개 행정동 공유 아크 추출
    print("Extracting topology-preserving shared arcs...")
    delta_arcs, encoded_feats, raw_polys_by_feat, total_arc_pts = extract_shared_arcs(gg_feats, quantize_fn, simplify_tol=7.5)
    print(f"Shared arcs: {len(delta_arcs):,}, simplified points: {total_arc_pts:,}")

    # 2) 경기도 전역 1km 육각 기지국 셀 격자(11,000+ 셀) RLE 생성
    HEX_R = 38.0  # center-to-center ≈ 65.8 units ≈ 1.02 km
    print(f"Building 1km hexagonal telecom cell grid (r={HEX_R})...")
    hex_runs, total_hex_cells, covered_dongs = build_hex_grid_rle(raw_polys_by_feat, max_qx, max_qy, hex_r=HEX_R)
    print(f"Generated {total_hex_cells:,} 1km hex cells ({len(hex_runs)//4:,} RLE runs, covers {covered_dongs}/602 dongs directly)")

    # 3) 서울 / 인천 배경 외곽 실루엣 추출
    seoul_rings = dissolve_outer_rings(seoul_feats, quantize_fn, simplify_tol=18.0, min_area_q=5000.0)
    incheon_rings = dissolve_outer_rings(incheon_feats, quantize_fn, simplify_tol=22.0, min_area_q=8000.0)

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

    # 4) 47개 시군구 외곽 경계선 추출
    sgg_groups = defaultdict(list)
    for ft in gg_feats:
        sgg_groups[ft["properties"]["sggnm"]].append(ft)

    sgg_meta = []
    sgg_names_sorted = sorted(sgg_groups.keys())
    sgg_idx_map = {name: i for i, name in enumerate(sgg_names_sorted)}

    for sgg_name in sgg_names_sorted:
        g_list = sgg_groups[sgg_name]
        sgg_rings = dissolve_outer_rings(g_list, quantize_fn, simplify_tol=12.0, min_area_q=1500.0)
        best_r = max(sgg_rings, key=lambda r: abs(ring_signed_area(r))) if sgg_rings else None
        if best_r:
            (cx, cy), _ = ring_centroid(best_r)
        else:
            cx, cy = max_qx / 2, max_qy / 2
        sgg_meta.append({
            "name": sgg_name,
            "code": g_list[0]["properties"]["sgg"],
            "cx": int(round(cx)),
            "cy": int(round(cy)),
            "rings": encode_rings(sgg_rings),
        })

    # 5) 602개 행정동 속성 + 트래픽 생성
    print(f"Synthesizing mobile traffic for 602 행정동 using {len(ANCHORS)} sub-anchors...")
    dongs = []
    csv_rows = []
    for idx, ft in enumerate(gg_feats):
        props = ft["properties"]
        adm_cd = props.get("adm_cd2") or props.get("adm_cd")
        sggnm = props["sggnm"]
        adm_nm = props["adm_nm"]
        parts = adm_nm.split()
        dongnm = parts[-1] if parts else adm_nm

        best_area_deg = -1.0
        best_cent = (127.0, 37.5)
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
        traffic = synthesize_dong_traffic(adm_cd, sggnm, dongnm, clon, clat, total_area_km2)

        dongs.append({
            "id": idx,
            "cd": adm_cd,
            "s": sgg_idx_map[sggnm],
            "nm": dongnm,
            "cx": qcx,
            "cy": qcy,
            "lon": round(clon, 4),
            "lat": round(clat, 4),
            "km2": round(total_area_km2, 2),
            "tp": traffic["type"],
            "anc": traffic["anchor"],
            "g5": traffic["g5"],
            "mix": traffic["mix"],
            "p": encoded_feats[idx],
            "wd": traffic["wd"],
            "we": traffic["we"],
        })

        for day_key, arr in [("weekday", traffic["wd"]), ("weekend", traffic["we"])]:
            for h, val in enumerate(arr):
                csv_rows.append([adm_cd, sggnm, dongnm, day_key, h, val])

    # 고속도로 양자화
    hw_out = []
    for hw in HIGHWAYS:
        qpts = []
        for lon, lat in hw["pts"]:
            qx, qy = quantize_fn(lon, lat)
            qpts.extend([qx, qy])
        hw_out.append({"name": hw["name"], "code": hw["code"], "pts": qpts})

    # 115+ 초상세 앵커 양자화 (브라우저 1km/400m 마이크로셀 실시간 공간 모델링용)
    type_to_idx = {t: i for i, t in enumerate(TYPE_KEYS)}
    anchors_out = []
    for name, alon, alat, atype, rad_km, w in ANCHORS:
        qx, qy = quantize_fn(alon, alat)
        rad_u = int(round(rad_km / km_per_unit))
        anchors_out.append([qx, qy, type_to_idx[atype], rad_u, round(w, 2), name])

    landmarks = [
        {"name": "판교TV", "lon": 127.1086, "lat": 37.4018, "tp": "biz"},
        {"name": "삼성전자 수원", "lon": 127.0558, "lat": 37.2565, "tp": "ind"},
        {"name": "삼성전자 평택", "lon": 127.0528, "lat": 37.0428, "tp": "ind"},
        {"name": "SK하이닉스 이천", "lon": 127.4882, "lat": 37.2512, "tp": "ind"},
        {"name": "수원역", "lon": 127.0016, "lat": 37.2659, "tp": "hub"},
        {"name": "동탄역", "lon": 127.0965, "lat": 37.2005, "tp": "hub"},
        {"name": "킨텍스", "lon": 126.7458, "lat": 37.6691, "tp": "biz"},
        {"name": "에버랜드", "lon": 127.2025, "lat": 37.2942, "tp": "tour"},
        {"name": "가평·자라섬", "lon": 127.5100, "lat": 37.8150, "tp": "tour"},
        {"name": "양평 두물머리", "lon": 127.3180, "lat": 37.5340, "tp": "tour"},
        {"name": "스타필드 하남", "lon": 127.2238, "lat": 37.5455, "tp": "hub"},
        {"name": "반월·시화산단", "lon": 126.7660, "lat": 37.3195, "tp": "ind"},
        {"name": "파주 LG디스플레이", "lon": 126.7798, "lat": 37.7980, "tp": "ind"},
        {"name": "과천청사·대공원", "lon": 127.0030, "lat": 37.4300, "tp": "biz"},
        {"name": "대부도", "lon": 126.5820, "lat": 37.2450, "tp": "tour"},
        {"name": "의정부역", "lon": 127.0458, "lat": 37.7382, "tp": "hub"},
    ]
    lm_out = []
    for lm in landmarks:
        qx, qy = quantize_fn(lm["lon"], lm["lat"])
        lm_out.append({"name": lm["name"], "x": qx, "y": qy, "tp": lm["tp"]})

    seoul_cx, seoul_cy = quantize_fn(126.985, 37.560)
    incheon_cx, incheon_cy = quantize_fn(126.665, 37.475)
    seoul_center_q = list(quantize_fn(127.003, 37.532))

    bundle = {
        "meta": {
            "version": "2026-10-01",
            "source": "vuski/admdongkor (경기도 602 행정동 + 1km 육각 셀)",
            "w": max_qx,
            "h": max_qy,
            "kmPerUnit": round(km_per_unit, 6),
            "minX": round(min_x, 6),
            "maxY": round(max_y, 6),
            "scale": round(scale, 4),
            "cosLat": round(COS_LAT_REF, 6),
            "seoulLabel": [seoul_cx, seoul_cy],
            "incheonLabel": [incheon_cx, incheon_cy],
            "seoulCenter": seoul_center_q,
            "typeKeys": TYPE_KEYS,
            "typeLabels": TYPE_LABELS,
        },
        "arcs": delta_arcs,
        "sggs": sgg_meta,
        "dongs": dongs,
        "hexGrid": {
            "r": HEX_R,
            "count": total_hex_cells,
            "runs": hex_runs,
        },
        "model": {
            "anchors": anchors_out,
            "wd": [CURVES_WEEKDAY[k] for k in TYPE_KEYS],
            "we": [CURVES_WEEKEND[k] for k in TYPE_KEYS],
            "hwWd": HIGHWAY_CURVE_WD,
            "hwWe": HIGHWAY_CURVE_WE,
        },
        "context": {
            "seoul": encode_rings(seoul_rings),
            "incheon": encode_rings(incheon_rings),
        },
        "highways": hw_out,
        "landmarks": lm_out,
    }

    bundle_path = os.path.join(OUT_DIR, "gyeonggi_bundle.json")
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))

    csv_path = os.path.join(OUT_DIR, "gyeonggi_sample_traffic.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["adm_cd", "sggnm", "dongnm", "day_type", "hour", "traffic_gbps"])
        w.writerows(csv_rows)

    print(f"Wrote {bundle_path} ({os.path.getsize(bundle_path) / 1024:.1f} KB)")
    print(f"Wrote {csv_path} ({os.path.getsize(csv_path) / 1024:.1f} KB, {len(csv_rows):,} rows)")


if __name__ == "__main__":
    main()
