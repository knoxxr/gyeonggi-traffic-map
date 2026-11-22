#!/usr/bin/env python3
"""
경기도 602개 행정동(읍·면·동) 경계 데이터 전처리 및 시간대별 모바일 통신 트래픽 모델 생성기.

원본 행정경계: vuski/admdongkor (2026.10.01 기준, 대한민국 행정동 경계)
출력:
  - data/gyeonggi_bundle.json : 웹 시각화 앱 내장용 경량 토폴로지(공유 아크) + 시공간 트래픽 데이터
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

COS_LAT_REF = math.cos(math.radians(37.55))  # 경기도 중심 위도 보정


def project_xy(lon: float, lat: float):
    """경기도 국소 등거리 원통 투영 (종횡비 왜곡 제거)."""
    return (lon * COS_LAT_REF, lat)


def iter_polygon_rings(geom):
    """GeoJSON Geometry -> list of polygons, each polygon is list of rings [(lon, lat), ...]."""
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
    """폴리곤 무게중심 (lon, lat)."""
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
    """Ramer-Douglas-Peucker 단순화 (끝점 고정으로 인접 폴리곤 토폴로지 100% 보존)."""
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
    out = [pts[i] for i in range(len(pts)) if keep[i]]
    return out


def dissolve_outer_rings(features, quantize_fn, simplify_tol=12.0, min_area_q=250.0):
    """
    다수의 폴리곤 집합에서 내부 공유 엣지를 상쇄하여 외곽 경계 링 추출.
    (시·군·구 통합 경계, 서울/인천 배경 실루엣 추출에 사용)
    """
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

    # 남은 외곽 엣지들을 링으로 연결
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
                # 링 단순화 (2분할 RDP로 폐곡선 안정화)
                mid = len(ring) // 2
                s1 = rdp_simplify(ring[: mid + 1], simplify_tol)
                s2 = rdp_simplify(ring[mid:], simplify_tol)
                simp = s1[:-1] + s2
                if len(simp) >= 4:
                    rings.append(simp)
    return rings


def extract_shared_arcs(gg_features, quantize_fn, simplify_tol=8.5):
    """
    경기도 602개 행정동의 모든 폴리곤 링을 토폴로지 보존 공유 아크(TopoJSON 스타일)로 분해·단순화.
    인접 동 간 경계선이 단일 아크로 공유되므로 틈(sliver)이나 어긋남이 0%.
    """
    # 1) 모든 링을 양자화 좌표로 변환하고 정점 차수(인접 고유 이웃 수) 계산
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
                # 아주 작은 무인도/노이즈 파편 제거 (주 폴리곤 외곽은 유지)
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

    # 분기점(junction): 이웃이 2개가 아닌 모든 정점
    junctions = {v for v, nbrs in vertex_neighbors.items() if len(nbrs) != 2}

    arc_map = {}  # canonical_tuple -> (arc_id, reversed_bool)
    arcs_list = []

    def register_arc(pts):
        # 끝점이 같은 폐쇄 링인 경우 2개로 분할해 단순화 후 등록
        fwd = tuple(pts)
        rev = tuple(reversed(pts))
        if fwd in arc_map:
            return arc_map[fwd]
        if rev in arc_map:
            return ~arc_map[rev]
        # 단순화
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
                n = len(qring) - 1  # 마지막 점은 첫 점과 동일
                j_indices = [i for i in range(n) if qring[i] in junctions]
                if not j_indices:
                    # 순수 고립 링 (섬 등)
                    aid = register_arc(qring)
                    enc_rings.append([aid])
                else:
                    # 분기점 기준으로 링 회전 후 아크 분할
                    ring_arcs = []
                    k = len(j_indices)
                    for idx_pos in range(k):
                        s = j_indices[idx_pos]
                        e = j_indices[(idx_pos + 1) % k]
                        if s < e:
                            seg = qring[s : e + 1]
                        else:
                            seg = qring[s:n] + qring[0 : e + 1]
                        if len(seg) >= 2:
                            ring_arcs.append(register_arc(seg))
                    if ring_arcs:
                        enc_rings.append(ring_arcs)
            if enc_rings:
                enc_polys.append(enc_rings)
        encoded_feats.append(enc_polys)

    # 아크를 델타 인코딩 평면 배열로 압축 [x0, y0, dx1, dy1, dx2, dy2, ...]
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

    return delta_arcs, encoded_feats, total_pts


# ---------------------------------------------------------------------------
# 2. 경기도 실제 도시구조 기반 24시간 모바일 통신 트래픽 시공간 모델
# ---------------------------------------------------------------------------

# (이름, 경도, 위도, 유형, 영향반경_km, 가중치)
ANCHORS = [
    # --- 업무·테크 허브 (biz) ---
    ("판교테크노밸리", 127.1086, 37.4018, "biz", 3.6, 2.35),
    ("분당 정자·수내·서현 업무지구", 127.1140, 37.3760, "biz", 3.2, 1.55),
    ("광교테크노밸리·경기도청", 127.0465, 37.2896, "biz", 3.2, 1.45),
    ("과천 지식정보타운·정부청사", 126.9896, 37.4264, "biz", 3.0, 1.45),
    ("안양 평촌·인덕원 IT밸리", 126.9685, 37.3945, "biz", 3.0, 1.35),
    ("광명 KTX·GIDC 업무지구", 126.8848, 37.4163, "biz", 2.8, 1.30),
    ("고양 킨텍스·방송영상밸리", 126.7458, 37.6691, "biz", 3.4, 1.30),
    ("동탄테크노밸리", 127.0992, 37.2116, "biz", 3.3, 1.45),
    ("하남 미사 지식산업센터", 127.1930, 37.5612, "biz", 2.6, 1.15),
    ("용인 플랫폼시티·기흥", 127.1095, 37.3042, "biz", 2.8, 1.05),
    ("구리·다산 테크노밸리", 127.1520, 37.6195, "biz", 2.6, 0.95),
    ("수원 시청·인계 업무지구", 127.0286, 37.2636, "biz", 2.6, 1.10),
    ("의정부 경기북부청사", 127.0725, 37.7460, "biz", 2.5, 0.90),

    # --- 첨단 산업단지·반도체 클러스터 (ind) ---
    ("삼성전자 수원 디지털시티", 127.0558, 37.2565, "ind", 3.2, 1.95),
    ("삼성전자 화성·기흥 반도체", 127.0772, 37.2248, "ind", 3.8, 2.05),
    ("삼성전자 평택 고덕 캠퍼스", 127.0528, 37.0428, "ind", 4.5, 1.90),
    ("SK하이닉스 이천 캠퍼스", 127.4882, 37.2512, "ind", 4.8, 1.75),
    ("반월·시화 국가산업단지", 126.7660, 37.3195, "ind", 4.8, 1.65),
    ("파주 LG디스플레이·월롱산단", 126.7798, 37.7980, "ind", 4.5, 1.45),
    ("용인 원삼·남사 반도체클러스터", 127.2500, 37.1635, "ind", 4.8, 1.25),
    ("평택 포승 국가산단·평택항", 126.8350, 36.9685, "ind", 4.5, 1.20),
    ("화성 향남·발안 산업단지", 126.9150, 37.1250, "ind", 4.0, 1.10),
    ("김포 양촌·학운 산업단지", 126.6250, 37.6150, "ind", 3.8, 1.05),
    ("안성 일반산업단지", 127.2350, 37.0150, "ind", 4.0, 0.95),

    # --- 상업·환승·번화가 (hub) ---
    ("수원역 환승·로데오", 127.0016, 37.2659, "hub", 2.8, 1.85),
    ("부천역·중동 중심상권", 126.7760, 37.4930, "hub", 3.0, 1.65),
    ("안양 범계·안양역 상권", 126.9420, 37.3920, "hub", 2.8, 1.50),
    ("의정부역 중심상권", 127.0458, 37.7382, "hub", 2.8, 1.45),
    ("성남 모란·야탑 환승허브", 127.1292, 37.4220, "hub", 2.6, 1.45),
    ("고양 일산 라페스타·화정", 126.7695, 37.6585, "hub", 3.2, 1.45),
    ("안산 중앙역 중심상권", 126.8388, 37.3158, "hub", 2.8, 1.35),
    ("평택역·소사벌 상권", 127.0858, 36.9908, "hub", 3.0, 1.30),
    ("구리역·돌다리 상권", 127.1436, 37.6033, "hub", 2.4, 1.30),
    ("군포 산본 중심상권", 126.9334, 37.3596, "hub", 2.4, 1.25),
    ("하남 스타필드", 127.2238, 37.5455, "hub", 2.8, 1.50),
    ("고양 스타필드·삼송", 126.8950, 37.6468, "hub", 2.8, 1.40),
    ("광명 철산 중심상권", 126.8680, 37.4760, "hub", 2.4, 1.35),
    ("여주 프리미엄 아울렛", 127.6122, 37.2480, "hub", 3.8, 1.15),
    ("파주 프리미엄 아울렛", 126.6975, 37.7720, "hub", 3.5, 1.15),
    ("동탄역 GTX·롯데백화점", 127.0965, 37.2005, "hub", 2.8, 1.55),

    # --- 대규모 통근 신도시·주거밀집 (res) ---
    ("화성 동탄2신도시", 127.1100, 37.1930, "res", 4.2, 1.65),
    ("용인 수지 주거권", 127.0950, 37.3220, "res", 3.8, 1.55),
    ("김포 한강신도시", 126.6780, 37.6410, "res", 4.5, 1.50),
    ("파주 운정신도시", 126.7560, 37.7215, "res", 4.2, 1.50),
    ("남양주 다산·별내신도시", 127.1520, 37.6300, "res", 4.5, 1.50),
    ("하남 미사·위례신도시", 127.1720, 37.5200, "res", 4.0, 1.45),
    ("시흥 배곧·은계신도시", 126.7270, 37.3750, "res", 4.0, 1.35),
    ("양주 옥정·회천신도시", 127.0900, 37.8230, "res", 4.5, 1.30),
    ("광주 태전·오포 주거권", 127.2320, 37.3750, "res", 4.2, 1.25),
    ("오산 세교신도시", 127.0490, 37.1770, "res", 3.5, 1.25),
    ("성남 분당 주거권", 127.1260, 37.3650, "res", 3.5, 1.45),
    ("수원 영통·호매실 주거권", 126.9600, 37.2600, "res", 3.8, 1.35),
    ("의왕 백운밸리·포일", 126.9930, 37.3770, "res", 3.2, 1.20),
    ("고양 일산·덕양 주거권", 126.8200, 37.6400, "res", 4.5, 1.45),

    # --- 주말 관광·레저·자연 (tour) ---
    ("용인 에버랜드·포곡", 127.2025, 37.2942, "tour", 4.5, 1.95),
    ("과천 서울대공원·서울랜드", 127.0168, 37.4342, "tour", 3.2, 1.75),
    ("가평 자라섬·남이섬·청평", 127.4950, 37.8050, "tour", 10.5, 1.85),
    ("가평 아침고요수목원", 127.3520, 37.7450, "tour", 7.5, 1.45),
    ("양평 두물머리·양서", 127.3180, 37.5340, "tour", 7.5, 1.70),
    ("양평 용문산 관광지", 127.5750, 37.5420, "tour", 8.5, 1.45),
    ("포천 산정호수·허브아일랜드", 127.3050, 38.0400, "tour", 10.0, 1.55),
    ("안산 대부도", 126.5820, 37.2450, "tour", 7.0, 1.75),
    ("화성 제부도·궁평항", 126.6450, 37.1630, "tour", 7.5, 1.55),
    ("광주 화담숲·곤지암", 127.2930, 37.3380, "tour", 5.5, 1.45),
    ("연천 한탄강·재인폭포", 127.0880, 38.0810, "tour", 9.0, 1.25),
    ("김포 애기봉·대명항", 126.5550, 37.6800, "tour", 6.5, 1.25),
    ("남양주 팔당·북한강변", 127.2950, 37.5900, "tour", 6.0, 1.45),

    # --- 대학가 (univ) ---
    ("아주대·경기대", 127.0445, 37.2830, "univ", 2.2, 1.20),
    ("성균관대 자연과학캠퍼스", 126.9750, 37.2935, "univ", 2.2, 1.25),
    ("경희대 국제캠퍼스", 127.0815, 37.2430, "univ", 2.2, 1.15),
    ("단국대 죽전캠퍼스", 127.1270, 37.3215, "univ", 2.2, 1.15),
    ("한양대 ERICA", 126.8345, 37.2975, "univ", 2.4, 1.15),
    ("가천대·복정", 127.1285, 37.4508, "univ", 2.0, 1.15),
]

# 주요 고속도로/광역교통 축 (지도 오버레이 + 통근/주말 이동 트래픽 모델링 겸용)
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

# 24시간 기본 프로파일 (0시~23시, 평균 약 1.0 기준 정규화)
# 각 도시 기능 유형별 평일 / 주말 시간대 곡선
CURVES_WEEKDAY = {
    # 업무·테크 허브: 07~10 급증, 10~17 고점 유지, 18~20 급감
    "biz": [
        0.30, 0.24, 0.20, 0.18, 0.20, 0.28, 0.52, 1.08,
        1.78, 2.24, 2.38, 2.42, 2.55, 2.36, 2.44, 2.48,
        2.35, 2.02, 1.38, 0.92, 0.68, 0.54, 0.44, 0.36,
    ],
    # 산업단지·반도체: 이른 출근(06~08), 교대근무(15, 23), 야간 베이스라인 높음
    "ind": [
        0.64, 0.56, 0.52, 0.50, 0.54, 0.68, 1.05, 1.56,
        1.76, 1.82, 1.86, 1.88, 1.94, 1.82, 1.88, 1.96,
        1.85, 1.62, 1.24, 1.02, 0.90, 0.84, 0.80, 0.72,
    ],
    # 상업·환승 허브: 출근 환승(08), 점심(12), 저녁 외식·유흥 피크(18~21)
    "hub": [
        0.58, 0.42, 0.32, 0.26, 0.28, 0.40, 0.68, 1.18,
        1.48, 1.32, 1.28, 1.42, 1.72, 1.64, 1.58, 1.66,
        1.78, 1.96, 2.26, 2.32, 2.18, 1.88, 1.42, 0.94,
    ],
    # 대규모 통근 신도시·주거: 출근 이탈(08~10 급감), 주간 저점, 퇴근 후 급증(18~23)
    "res": [
        1.06, 0.78, 0.58, 0.48, 0.46, 0.56, 0.82, 0.94,
        0.74, 0.62, 0.64, 0.68, 0.74, 0.72, 0.76, 0.86,
        1.02, 1.24, 1.58, 1.86, 2.02, 2.08, 1.96, 1.52,
    ],
    # 관광·레저 (평일: 상대적으로 잔잔, 낮 시간 소폭 상승)
    "tour": [
        0.48, 0.38, 0.32, 0.28, 0.30, 0.38, 0.52, 0.68,
        0.82, 0.98, 1.16, 1.28, 1.36, 1.38, 1.34, 1.26,
        1.14, 1.00, 0.92, 0.86, 0.82, 0.76, 0.68, 0.58,
    ],
    # 대학가: 10~20 활발 + 심야 스트리밍 테일
    "univ": [
        1.02, 0.78, 0.58, 0.44, 0.36, 0.38, 0.50, 0.76,
        1.14, 1.46, 1.66, 1.78, 1.92, 1.84, 1.82, 1.78,
        1.72, 1.68, 1.64, 1.62, 1.58, 1.52, 1.40, 1.22,
    ],
    # 농촌·외곽 일반 (rural)
    "rural": [
        0.62, 0.48, 0.40, 0.36, 0.38, 0.50, 0.72, 0.94,
        1.06, 1.10, 1.14, 1.18, 1.24, 1.22, 1.20, 1.18,
        1.16, 1.14, 1.16, 1.18, 1.14, 1.04, 0.90, 0.74,
    ],
}

CURVES_WEEKEND = {
    # 업무·테크 허브: 주말 대폭 감소
    "biz": [
        0.34, 0.26, 0.22, 0.19, 0.19, 0.22, 0.30, 0.42,
        0.56, 0.70, 0.82, 0.92, 1.02, 1.04, 1.02, 0.98,
        0.92, 0.86, 0.82, 0.76, 0.66, 0.56, 0.46, 0.38,
    ],
    # 산업단지: 주말 교대 최소 가동
    "ind": [
        0.54, 0.46, 0.42, 0.40, 0.42, 0.48, 0.62, 0.78,
        0.88, 0.94, 0.98, 1.02, 1.04, 1.02, 1.00, 0.98,
        0.94, 0.88, 0.82, 0.76, 0.70, 0.66, 0.62, 0.58,
    ],
    # 상업·복합몰 허브: 주말 오후~저녁 폭발
    "hub": [
        0.82, 0.58, 0.42, 0.34, 0.30, 0.34, 0.46, 0.66,
        0.92, 1.26, 1.66, 2.02, 2.34, 2.48, 2.56, 2.60,
        2.52, 2.40, 2.32, 2.18, 1.94, 1.64, 1.32, 1.02,
    ],
    # 주거·신도시: 주말 아침 늦잠, 낮에도 높고 저녁 안정적
    "res": [
        1.26, 0.98, 0.74, 0.58, 0.50, 0.50, 0.58, 0.76,
        1.02, 1.24, 1.36, 1.42, 1.44, 1.40, 1.36, 1.38,
        1.46, 1.58, 1.72, 1.84, 1.92, 1.94, 1.82, 1.54,
    ],
    # 관광·레저: 주말 09~15 폭발적 급증!
    "tour": [
        0.64, 0.48, 0.38, 0.32, 0.32, 0.40, 0.60, 0.96,
        1.48, 2.12, 2.74, 3.12, 3.28, 3.34, 3.22, 2.94,
        2.46, 1.94, 1.54, 1.28, 1.12, 0.98, 0.84, 0.72,
    ],
    # 대학가: 주말 오후 완만
    "univ": [
        1.14, 0.90, 0.68, 0.52, 0.42, 0.40, 0.46, 0.58,
        0.74, 0.92, 1.12, 1.26, 1.36, 1.42, 1.44, 1.46,
        1.44, 1.42, 1.44, 1.42, 1.38, 1.32, 1.24, 1.16,
    ],
    # 외곽 일반: 주말 나들이 통과·거주 소폭 증가
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

TYPE_LABELS = {
    "biz": "업무·테크",
    "ind": "산업·반도체",
    "hub": "상업·환승",
    "res": "주거·신도시",
    "tour": "관광·휴양",
    "univ": "대학가",
    "rural": "외곽·복합",
}


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
            # segment distance in km
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
    """결정적 [0, 1) 난수 (재현성 보장)."""
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 100000) / 100000.0


def synthesize_dong_traffic(adm_cd, sggnm, dongnm, lon, lat, area_km2):
    """
    경기도 각 행정동의 실제 좌표, 면적(도심 밀집도 프록시), 60+ 핵심 거점 및 고속도로 거리 기반
    24시간 평일/주말 모바일 통신 트래픽(Gbps) + 5G 비율 + 서비스 구성 생성.
    """
    weights = defaultdict(float)
    nearest_anchor = None
    best_anchor_score = 0.0

    for name, alon, alat, atype, rad, w in ANCHORS:
        d = haversine_km(lon, lat, alon, alat)
        if d < rad * 2.6:
            infl = w * math.exp(-0.5 * (d / rad) ** 2)
            weights[atype] += infl
            if infl > best_anchor_score:
                best_anchor_score = infl
                nearest_anchor = name

    # 행정동 명칭 휴리스틱 보강 (정확한 지리적 정합성 강화)
    full = f"{sggnm} {dongnm}"
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
        # 동 지역은 기본 도시 주거/생활 베이스 부여
        weights["res"] += 0.55

    # 서울 도심(시청/강남) 근접성으로 수도권 핵심 밀도 보정
    d_seoul = min(haversine_km(lon, lat, 126.978, 37.566), haversine_km(lon, lat, 127.028, 37.498))
    metro_factor = math.exp(-d_seoul / 38.0)

    # 고속도로 인접 이동 트래픽 가중치
    d_hw = min_dist_to_highways_km(lon, lat)
    hw_w = 0.28 * math.exp(-0.5 * (d_hw / 2.2) ** 2)

    # 면적이 작을수록 고밀도 도심 행정동 (예: 0.5~3 km2 vs 면 단위 30~100 km2)
    density_factor = 1.0 / (0.35 + (max(area_km2, 0.35) ** 0.34))

    total_w = sum(weights.values())
    if total_w < 1e-6:
        weights["rural"] = 1.0
        total_w = 1.0

    # 대표 유형 결정
    primary_type = max(weights.items(), key=lambda kv: kv[1])[0]

    # 기준 트래픽 규모 (Gbps 단위, 행정동당 일평균 ~1.2 Gbps(외곽 면) ~ 38 Gbps(판교/수원역/동탄))
    r_seed = det_hash(adm_cd)
    r_jitter = 0.88 + 0.24 * r_seed
    anchor_boost = 1.0 + 0.85 * min(best_anchor_score, 2.5)
    base_scale = (2.6 + 13.5 * metro_factor) * density_factor * anchor_boost * r_jitter
    if dongnm.endswith("면"):
        base_scale *= 0.52
    elif dongnm.endswith("읍"):
        base_scale *= 0.84
    base_scale = max(0.85, min(42.0, base_scale))

    # 24시간 평일 / 주말 트래픽 배열 합성
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
        # 고속도로 통행 트래픽 가산
        v_wd = v_wd * (1.0 - hw_w * 0.45) + hw_w * HIGHWAY_CURVE_WD[h]
        v_we = v_we * (1.0 - hw_w * 0.45) + hw_w * HIGHWAY_CURVE_WE[h]

        # 미세한 동별 시간 변동성(자연스러운 비균질성)
        h_wave = 1.0 + 0.055 * math.sin((h + r_seed * 12.0) * 0.72) + phase_noise * math.cos(h * 0.45)
        wd.append(round(max(0.12, base_scale * v_wd * h_wave), 2))
        we.append(round(max(0.12, base_scale * v_we * h_wave), 2))

    # 5G 비율 (%)
    g5_base = {"biz": 74, "hub": 70, "univ": 72, "res": 64, "ind": 61, "tour": 54, "rural": 43}[primary_type]
    g5_pct = int(round(max(32, min(86, g5_base + 8 * metro_factor + (r_seed - 0.5) * 8))))

    # 서비스 유형 구성 [동영상/스트리밍, SNS·메신저, 업무·클라우드, 내비·모빌리티] 합=100
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
        "anchor": nearest_anchor if best_anchor_score >= 0.42 else "",
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

    print(f"Gyeonggi-do features: {len(gg_feats)}, Seoul: {len(seoul_feats)}, Incheon: {len(incheon_feats)}")

    # 경기도 투영 좌표 범위 계산
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
    print(f"Quantized grid: {max_qx} x {max_qy} (1 unit ≈ {111.0 * max(span_x, span_y) / 10000.0 * 1000:.1f} m)")

    def quantize_fn(lon, lat):
        x, y = project_xy(lon, lat)
        qx = int(round((x - min_x) * scale))
        qy = int(round((max_y - y) * scale))  # 화면 Y축(북쪽이 0)
        return (qx, qy)

    # 1) 경기도 602개 행정동 공유 아크 추출
    print("Extracting topology-preserving shared arcs...")
    delta_arcs, encoded_feats, total_arc_pts = extract_shared_arcs(gg_feats, quantize_fn, simplify_tol=7.5)
    print(f"Shared arcs: {len(delta_arcs):,}, total simplified points: {total_arc_pts:,}")

    # 2) 서울 / 인천 배경 외곽 실루엣 추출
    print("Dissolving Seoul & Incheon context outlines...")
    seoul_rings = dissolve_outer_rings(seoul_feats, quantize_fn, simplify_tol=18.0, min_area_q=5000.0)
    incheon_rings = dissolve_outer_rings(incheon_feats, quantize_fn, simplify_tol=22.0, min_area_q=8000.0)
    # 델타 인코딩 헬퍼
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

    # 3) 47개 시군구 외곽 경계선 추출 (시군구 경계 강조 및 시군구 뷰용)
    print("Dissolving 47 시·군·구 boundaries...")
    sgg_groups = defaultdict(list)
    for ft in gg_feats:
        sgg_groups[ft["properties"]["sggnm"]].append(ft)

    sgg_meta = []
    sgg_names_sorted = sorted(sgg_groups.keys())
    sgg_idx_map = {name: i for i, name in enumerate(sgg_names_sorted)}

    for sgg_name in sgg_names_sorted:
        g_list = sgg_groups[sgg_name]
        sgg_rings = dissolve_outer_rings(g_list, quantize_fn, simplify_tol=12.0, min_area_q=1500.0)
        # 시군구 대표 중심점 (가장 큰 링 무게중심)
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

    # 4) 602개 행정동 속성 + 트래픽 생성
    print("Synthesizing geographically grounded mobile traffic for 602 행정동...")
    dongs = []
    csv_rows = []
    for idx, ft in enumerate(gg_feats):
        props = ft["properties"]
        adm_cd = props.get("adm_cd2") or props.get("adm_cd")
        sggnm = props["sggnm"]
        adm_nm = props["adm_nm"]
        # adm_nm 예: "경기도 수원시장안구 파장동" -> dongnm = "파장동"
        parts = adm_nm.split()
        dongnm = parts[-1] if parts else adm_nm

        # 가장 큰 폴리곤 외곽에서 실제 경위도 중심점 및 면적(km^2) 계산
        best_area_deg = -1.0
        best_cent = (127.0, 37.5)
        total_area_km2 = 0.0
        for poly in iter_polygon_rings(ft["geometry"]):
            outer = poly[0]
            cent, a_deg = ring_centroid(outer)
            # 1 deg lat ≈ 110.57 km, 1 deg lon ≈ 111.32 * cos(lat) km
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

    # 주요 랜드마크 핀 (지도 오버레이용)
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

    # 서울·인천 라벨 좌표
    seoul_cx, seoul_cy = quantize_fn(126.985, 37.560)
    incheon_cx, incheon_cy = quantize_fn(126.665, 37.475)

    bundle = {
        "meta": {
            "version": "2026-10-01",
            "source": "vuski/admdongkor (경기도 602개 행정동)",
            "w": max_qx,
            "h": max_qy,
            "seoulLabel": [seoul_cx, seoul_cy],
            "incheonLabel": [incheon_cx, incheon_cy],
            "typeLabels": TYPE_LABELS,
        },
        "arcs": delta_arcs,
        "sggs": sgg_meta,
        "dongs": dongs,
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

    # 간단한 검증 통계 출력
    wd_totals = [sum(d["wd"][h] for d in dongs) for h in range(24)]
    we_totals = [sum(d["we"][h] for d in dongs) for h in range(24)]
    print(f"Weekday total Gbps: min={min(wd_totals):.1f} (@{wd_totals.index(min(wd_totals))}h), max={max(wd_totals):.1f} (@{wd_totals.index(max(wd_totals))}h)")
    print(f"Weekend total Gbps: min={min(we_totals):.1f} (@{we_totals.index(min(we_totals))}h), max={max(we_totals):.1f} (@{we_totals.index(max(we_totals))}h)")
    type_counts = Counter(d["tp"] for d in dongs)
    print("Dong archetypes:", dict(type_counts))


if __name__ == "__main__":
    main()
