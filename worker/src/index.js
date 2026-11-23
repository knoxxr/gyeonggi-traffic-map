/**
 * 한국 모바일 트래픽 모니터 — Cloudflare Worker 하이브리드 실시간 프록시
 *
 * • 서울 121개 공식 핫스팟 실시간 유동인구 (서울 열린데이터광장 OA-21778, SKT·KT 기지국 기반)
 * • 전국 16개 시·도 44개 주요 권역 ITS 실시간 도로 혼잡도 (국가교통정보센터)
 * • Free Tier 50-subrequest 한도 자동 대응 (free_chunked: 2분마다 4샤드 순환 수집 = 8분 전체 갱신, $0/월)
 *   또는 Paid 플랜 full 모드 (5분마다 165곳 일괄 수집)
 * • KV 히스토리 저장 + 선택적 Google Sheets(gas/Code.gs doPost) 자동 전달 지원
 */

import { SEOUL_121_POIS, NATIONWIDE_42_ITS } from "./locations.js";

const CONGEST_RANK = { "붐빔": 4, "정체": 4, "약간 붐빔": 3, "서행": 3, "보통": 2, "여유": 1, "원활": 1 };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept",
};

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...CORS_HEADERS,
      ...extraHeaders,
    },
  });
}

function getKstStrings(date = new Date()) {
  const kst = new Date(date.getTime() + 9 * 3600 * 1000);
  const yyyy = kst.getUTCFullYear();
  const mm = String(kst.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(kst.getUTCDate()).padStart(2, "0");
  const hh = String(kst.getUTCHours()).padStart(2, "0");
  const mi = String(kst.getUTCMinutes()).padStart(2, "0");
  return {
    full: `${yyyy}-${mm}-${dd} ${hh}:${mi}`,
    date: `${yyyy}-${mm}-${dd}`,
    hm: `${hh}:${mi}`,
    minuteSlot: `${hh}:${String(Math.floor(kst.getUTCMinutes() / 5) * 5).padStart(2, "0")}`,
  };
}

function parseXmlTag(xml, tag) {
  const m = xml.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`));
  return m ? m[1].trim() : "";
}

async function fetchOneSeoulPoi(poi, apiKey, tsFallback) {
  const [code, category, name, lon, lat] = poi;
  // sample 키는 광화문·덕수궁 1곳만 지원
  if (apiKey.toLowerCase() === "sample" && name !== "광화문·덕수궁") return null;
  const url = `http://openapi.seoul.go.kr:8088/${encodeURIComponent(apiKey)}/json/citydata_ppltn/1/5/${encodeURIComponent(name)}`;
  try {
    const resp = await fetch(url, { cf: { cacheTtl: 60 } });
    if (!resp.ok) return null;
    const txt = (await resp.text()).trim();
    let row = null;
    if (txt.startsWith("{")) {
      const json = JSON.parse(txt);
      const list = json["SeoulRtd.citydata_ppltn"];
      row = Array.isArray(list) ? list[0] : (list && typeof list === "object" ? list : null);
    } else if (txt.startsWith("<")) {
      if (!txt.includes("AREA_NM")) return null;
      row = {
        AREA_NM: parseXmlTag(txt, "AREA_NM"),
        AREA_CD: parseXmlTag(txt, "AREA_CD"),
        AREA_CONGEST_LVL: parseXmlTag(txt, "AREA_CONGEST_LVL"),
        AREA_CONGEST_MSG: parseXmlTag(txt, "AREA_CONGEST_MSG"),
        AREA_PPLTN_MIN: parseXmlTag(txt, "AREA_PPLTN_MIN"),
        AREA_PPLTN_MAX: parseXmlTag(txt, "AREA_PPLTN_MAX"),
        PPLTN_TIME: parseXmlTag(txt, "PPLTN_TIME"),
      };
    }
    if (!row || !row.AREA_CONGEST_LVL) return null;
    const popMin = Number(row.AREA_PPLTN_MIN) || 0;
    const popMax = Number(row.AREA_PPLTN_MAX) || popMin;
    const pop = Math.round((popMin + popMax) / 2);
    const congest = String(row.AREA_CONGEST_LVL || "보통").trim();
    const boost = congest === "붐빔" ? 1.35 : congest === "약간 붐빔" ? 1.15 : congest === "여유" ? 0.85 : 1.0;
    const trafficGbps = Number(Math.max(1.2, (pop / 1400) * boost).toFixed(2));
    return {
      ts: String(row.PPLTN_TIME || tsFallback),
      source: "seoul_pop",
      category,
      code: String(row.AREA_CD || code),
      name: String(row.AREA_NM || name),
      lon,
      lat,
      pop_min: popMin,
      pop_max: popMax,
      pop,
      congest,
      speed: 0,
      traffic_gbps: trafficGbps,
      note: String(row.AREA_CONGEST_MSG || "").slice(0, 60),
    };
  } catch {
    return null;
  }
}

async function fetchOneItsHub(hub, apiKey, tsFallback) {
  const [code, sido, name, lon, lat, d, baseSpeed] = hub;
  const minX = (lon - d).toFixed(4);
  const maxX = (lon + d).toFixed(4);
  const minY = (lat - d * 0.85).toFixed(4);
  const maxY = (lat + d * 0.85).toFixed(4);
  const url = `https://openapi.its.go.kr:9443/trafficInfo?apiKey=${encodeURIComponent(apiKey)}&type=all&drcType=all&minX=${minX}&maxX=${maxX}&minY=${minY}&maxY=${maxY}&getType=json`;
  try {
    const resp = await fetch(url, { cf: { cacheTtl: 60 } });
    if (!resp.ok) return null;
    const json = await resp.json();
    const items = json?.body?.items;
    if (!Array.isArray(items) || items.length === 0) return null;
    let sumSpd = 0, cnt = 0;
    for (let i = 0; i < items.length; i++) {
      const sp = Number(items[i].speed);
      if (sp > 1 && sp < 160) { sumSpd += sp; cnt++; }
    }
    if (cnt === 0) return null;
    const avgSpd = Number((sumSpd / cnt).toFixed(1));
    const ratio = avgSpd / (baseSpeed || 58);
    const congest = ratio < 0.48 ? "정체" : ratio < 0.75 ? "서행" : "원활";
    const congestionIdx = Math.max(0.1, Math.min(1.6, 1.35 - ratio * 0.85));
    const trafficGbps = Number((12.0 * congestionIdx).toFixed(2));
    return {
      ts: tsFallback,
      source: "its",
      category: `전국 ITS · ${sido}`,
      code,
      name,
      lon,
      lat,
      pop_min: 0,
      pop_max: 0,
      pop: 0,
      congest,
      speed: avgSpd,
      traffic_gbps: trafficGbps,
      note: `링크 ${cnt}개 평균 ${avgSpd}km/h`,
    };
  } catch {
    return null;
  }
}

async function runPool(items, concurrency, fn) {
  const results = [];
  let idx = 0;
  async function worker() {
    while (idx < items.length) {
      const cur = idx++;
      const res = await fn(items[cur]);
      if (res) results.push(res);
    }
  }
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, () => worker());
  await Promise.all(workers);
  return results;
}

function sortRecords(list) {
  return list.sort((a, b) => {
    const ra = CONGEST_RANK[a.congest] || 0;
    const rb = CONGEST_RANK[b.congest] || 0;
    if (rb !== ra) return rb - ra;
    return (b.pop || b.traffic_gbps * 1000) - (a.pop || a.traffic_gbps * 1000);
  });
}

function summarize(data) {
  let seoulCount = 0, itsCount = 0, crowded = 0, busy = 0;
  for (const r of data) {
    if (r.source === "seoul_pop") seoulCount++;
    else if (r.source === "its") itsCount++;
    if (r.congest === "붐빔" || r.congest === "정체") crowded++;
    else if (r.congest === "약간 붐빔" || r.congest === "서행") busy++;
  }
  return { total: data.length, seoul_count: seoulCount, its_count: itsCount, crowded, busy };
}

// 4-shard 분할 (각 샤드 <= 44 subrequests -> Cloudflare Free Tier 50 한도 완벽 준수!)
function getShardTasks(shardIdx) {
  if (shardIdx === 0) return { type: "seoul", items: SEOUL_121_POIS.slice(0, 41) };
  if (shardIdx === 1) return { type: "seoul", items: SEOUL_121_POIS.slice(41, 82) };
  if (shardIdx === 2) return { type: "seoul", items: SEOUL_121_POIS.slice(82) };
  return { type: "its", items: NATIONWIDE_42_ITS };
}

async function pushToGasSheet(env, kst, data) {
  const gasUrl = (env.GAS_INGEST_URL || "").trim();
  if (!gasUrl || data.length === 0) return false;
  try {
    const resp = await fetch(gasUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: (env.GAS_INGEST_TOKEN || env.API_TOKEN || "").trim(),
        timestamp: kst.full,
        date: kst.date,
        hm: kst.hm,
        data,
      }),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

// ============================================================================
// Cloudflare Radar API — 대한민국(KR) 실시간 모바일/인터넷 트래픽 지수 (3 subrequests)
// Token 권한: Account > Radar > Read (무료)
// ============================================================================
function analyzeRadarSeries(seriesObj) {
  const vals = (seriesObj?.values || []).map((v) => Number(v)).filter((v) => !isNaN(v));
  const ts = seriesObj?.timestamps || [];
  if (vals.length === 0) return null;
  const last = vals[vals.length - 1];
  const prev = vals.length >= 2 ? vals[vals.length - 2] : last;
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
  const idx = Number((last * 100).toFixed(1));
  const deltaPct = prev > 0.0001 ? Number((((last - prev) / prev) * 100).toFixed(1)) : 0;
  const vsAvgPct = avg > 0.0001 ? Number((((last - avg) / avg) * 100).toFixed(1)) : 0;
  // 24~32개 포인트로 다운샘플링한 스파크라인 (0~100 정수)
  const step = Math.max(1, Math.floor(vals.length / 28));
  const spark = [];
  for (let i = 0; i < vals.length; i += step) spark.push(Math.round(vals[i] * 100));
  if (spark[spark.length - 1] !== Math.round(last * 100)) spark.push(Math.round(last * 100));
  return { idx, deltaPct, vsAvgPct, avgRatio: avg > 0.0001 ? last / avg : 1, lastTs: ts[ts.length - 1] || "", spark };
}

async function fetchCloudflareRadarKr(env, kst) {
  const token = (env.CF_RADAR_TOKEN || "").trim();
  if (!token) return null;
  const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
  const base = "https://api.cloudflare.com/client/v4/radar";
  try {
    const [rMob, rDev, rNet] = await Promise.all([
      fetch(`${base}/http/timeseries?location=KR&dateRange=1d&aggInterval=15m&deviceType=MOBILE&name=mobile`, { headers, cf: { cacheTtl: 300 } }),
      fetch(`${base}/http/summary/DEVICE_TYPE?location=KR&dateRange=1d`, { headers, cf: { cacheTtl: 300 } }),
      fetch(`${base}/netflows/timeseries?location=KR&dateRange=1d&aggInterval=15m&name=kr`, { headers, cf: { cacheTtl: 300 } }),
    ]);

    let mobStats = null, netStats = null, mobShare = 0, deskShare = 0;
    if (rMob.ok) {
      const j = await rMob.json();
      mobStats = analyzeRadarSeries(j?.result?.mobile || j?.result?.serie_0);
    }
    if (rNet.ok) {
      const j = await rNet.json();
      netStats = analyzeRadarSeries(j?.result?.kr || j?.result?.serie_0);
    }
    if (rDev.ok) {
      const j = await rDev.json();
      const s = j?.result?.summary_0 || {};
      const m = Number(s.mobile ?? s.MOBILE ?? 0);
      const d = Number(s.desktop ?? s.DESKTOP ?? 0);
      const tot = m + d;
      if (tot > 0) {
        mobShare = Number(((m / tot) * 100).toFixed(1));
        deskShare = Number((100 - mobShare).toFixed(1));
      }
    }
    if (!mobStats && !netStats && mobShare === 0) return null;

    const ratio = mobStats ? mobStats.avgRatio : (netStats ? netStats.avgRatio : 1);
    const scaleFactor = Number(Math.max(0.78, Math.min(1.32, 0.8 + ratio * 0.2)).toFixed(3));
    return {
      provider: "Cloudflare Radar",
      location: "KR",
      updated_at: kst.full,
      mobile_index: mobStats ? mobStats.idx : 0,
      mobile_delta_pct: mobStats ? mobStats.deltaPct : 0,
      mobile_vs_avg_pct: mobStats ? mobStats.vsAvgPct : 0,
      netflow_index: netStats ? netStats.idx : 0,
      netflow_delta_pct: netStats ? netStats.deltaPct : 0,
      mobile_share_pct: mobShare,
      desktop_share_pct: deskShare,
      scale_factor: scaleFactor,
      sparkline: mobStats?.spark || netStats?.spark || [],
    };
  } catch {
    return null;
  }
}

async function saveSnapshotToKv(kv, kst, mergedArray, radarObj) {
  const sorted = sortRecords(mergedArray);
  const summary = summarize(sorted);

  // 1) 날짜 목록 갱신
  const datesRaw = (await kv.get("dates_index", "json")) || {};
  const dayKey = `history:${kst.date}`;
  const dayData = (await kv.get(dayKey, "json")) || { mode: "history", date: kst.date, times: [], snapshots: {} };

  // 스냅샷을 컴팩트하게 저장 (KV value 25MB 한도, 하루 180스냅샷 * 165곳 ≈ 2.2MB)
  const compact = sorted.map((r) => ({
    source: r.source,
    category: r.category,
    code: r.code,
    name: r.name,
    lon: r.lon,
    lat: r.lat,
    pop: r.pop || 0,
    congest: r.congest,
    speed: r.speed || 0,
    traffic_gbps: r.traffic_gbps,
  }));

  if (!dayData.snapshots[kst.hm]) dayData.times.push(kst.hm);
  dayData.times.sort();
  dayData.snapshots[kst.hm] = compact;
  await kv.put(dayKey, JSON.stringify(dayData), { expirationTtl: 60 * 60 * 24 * 90 }); // 90일 보관

  datesRaw[kst.date] = {
    date: kst.date,
    snapshots: dayData.times.length,
    rows: Object.values(dayData.snapshots).reduce((a, b) => a + b.length, 0),
  };
  await kv.put("dates_index", JSON.stringify(datesRaw));

  const datesList = Object.keys(datesRaw).sort().reverse();
  const latestPayload = {
    mode: "latest",
    updated_at: kst.full,
    summary,
    radar: radarObj || null,
    count: sorted.length,
    dates: datesList,
    data: sorted,
  };
  await kv.put("latest_payload", JSON.stringify(latestPayload));
  return latestPayload;
}

async function collectScheduled(env, forcedMode) {
  const kst = getKstStrings();
  const seoulKey = (env.SEOUL_API_KEY || "sample").trim();
  const itsKey = (env.ITS_API_KEY || "").trim();
  const mode = forcedMode || (env.COLLECT_MODE || "free_chunked").toLowerCase();
  const kv = env.TRAFFIC_KV;

  if (mode === "full") {
    // Paid 플랜 또는 수동 전체 수집: 서울 121 + 전국 ITS 44 + Radar KR 일괄 수집
    const [seoulData, itsData, radar] = await Promise.all([
      seoulKey ? runPool(SEOUL_121_POIS, 20, (p) => fetchOneSeoulPoi(p, seoulKey, kst.full)) : [],
      itsKey ? runPool(NATIONWIDE_42_ITS, 16, (h) => fetchOneItsHub(h, itsKey, kst.full)) : [],
      fetchCloudflareRadarKr(env, kst),
    ]);
    const all = sortRecords([...seoulData, ...itsData]);
    if (kv) {
      if (radar) await kv.put("radar_latest", JSON.stringify(radar), { expirationTtl: 1800 });
      await saveSnapshotToKv(kv, kst, all, radar);
    }
    const gasPushed = await pushToGasSheet(env, kst, all);
    return { mode: "full", updated_at: kst.full, ...summarize(all), radar_ok: Boolean(radar), gas_pushed: gasPushed };
  }

  // Free Tier 안전 샤드 순환 수집 (매 호출 <= 44 subrequests!)
  let shardIdx = 0;
  let mergedMap = {};
  let radar = null;
  if (kv) {
    shardIdx = Number((await kv.get("shard_cursor")) || 0) % 4;
    mergedMap = (await kv.get("latest_map", "json")) || {};
    radar = await kv.get("radar_latest", "json");
  }

  const task = getShardTasks(shardIdx);
  let fetched = [];
  if (task.type === "seoul" && seoulKey) {
    // Shard 0에서만 Cloudflare Radar 3건 동시 호출 (41 + 3 = 44 <= 50)
    if (shardIdx === 0 && env.CF_RADAR_TOKEN) {
      const [sRes, rRes] = await Promise.all([
        runPool(task.items, 12, (p) => fetchOneSeoulPoi(p, seoulKey, kst.full)),
        fetchCloudflareRadarKr(env, kst),
      ]);
      fetched = sRes;
      if (rRes) {
        radar = rRes;
        if (kv) await kv.put("radar_latest", JSON.stringify(radar), { expirationTtl: 1800 });
      }
    } else {
      fetched = await runPool(task.items, 12, (p) => fetchOneSeoulPoi(p, seoulKey, kst.full));
    }
  } else if (task.type === "its" && itsKey) {
    fetched = await runPool(task.items, 12, (h) => fetchOneItsHub(h, itsKey, kst.full));
  }

  for (const r of fetched) mergedMap[`${r.source}:${r.name}`] = r;

  const nextShard = (shardIdx + 1) % 4;
  const mergedList = Object.values(mergedMap);
  let gasPushed = false;

  if (kv) {
    await kv.put("latest_map", JSON.stringify(mergedMap));
    await kv.put("shard_cursor", String(nextShard));
    if (shardIdx === 3 || !(await kv.get("latest_payload"))) {
      await saveSnapshotToKv(kv, kst, mergedList, radar);
      gasPushed = await pushToGasSheet(env, kst, sortRecords(mergedList));
    } else {
      const existing = (await kv.get("latest_payload", "json")) || { dates: [] };
      const sorted = sortRecords(mergedList);
      await kv.put("latest_payload", JSON.stringify({
        mode: "latest",
        updated_at: kst.full,
        summary: summarize(sorted),
        radar: radar || existing.radar || null,
        count: sorted.length,
        dates: existing.dates || [],
        data: sorted,
      }));
    }
  }

  return {
    mode: "free_chunked",
    shard: shardIdx,
    fetched: fetched.length,
    merged_total: mergedList.length,
    radar_ok: Boolean(radar),
    updated_at: kst.full,
    gas_pushed: gasPushed,
  };
}

// KV 미연결 시 즉석 하이브리드 수집 (Free Tier 50 한도 내: 핵심 서울 26곳 + 전국 15권역 + Radar 3 = 44호출)
async function collectOnDemandNoKv(env) {
  const kst = getKstStrings();
  const seoulKey = (env.SEOUL_API_KEY || "sample").trim();
  const itsKey = (env.ITS_API_KEY || "").trim();
  const coreSeoulNames = new Set([
    "강남역", "홍대 관광특구", "잠실역", "명동 관광특구", "여의도", "성수카페거리",
    "광화문·덕수궁", "고속터미널역", "서울역", "건대입구역", "이태원 관광특구",
    "강남 MICE 관광특구", "동대문 관광특구", "종로·청계 관광특구", "신촌·이대역",
    "사당역", "구로디지털단지역", "가산디지털단지역", "영등포 타임스퀘어", "용산역",
    "가로수길", "압구정로데오거리", "연남동", "여의도한강공원", "잠실롯데타워·석촌호수", "선릉역",
  ]);
  const coreSeoul = SEOUL_121_POIS.filter((p) => coreSeoulNames.has(p[2]));
  const coreIts = NATIONWIDE_42_ITS.filter((_, i) => i % 2 === 0).slice(0, 15);
  const [sData, iData, radar] = await Promise.all([
    seoulKey ? runPool(coreSeoul, 10, (p) => fetchOneSeoulPoi(p, seoulKey, kst.full)) : [],
    itsKey ? runPool(coreIts, 10, (h) => fetchOneItsHub(h, itsKey, kst.full)) : [],
    fetchCloudflareRadarKr(env, kst),
  ]);
  const sorted = sortRecords([...sData, ...iData]);
  return {
    mode: "latest",
    note: "KV 미연결 즉석 모드 (핵심 41곳 + Radar KR). 전체 121+44곳 5분 자동수집은 KV 바인딩을 연결하세요.",
    updated_at: kst.full,
    summary: summarize(sorted),
    radar: radar || null,
    count: sorted.length,
    dates: [],
    data: sorted,
  };
}

function isAuthorized(request, env, url) {
  const reqToken = (env.API_TOKEN || "").trim();
  if (!reqToken) return true;
  const qToken = (url.searchParams.get("token") || "").trim();
  if (qToken === reqToken) return true;
  const auth = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "").trim();
  return auth === reqToken;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
    const mode = (url.searchParams.get("mode") || path || "latest").toLowerCase();

    if (mode === "health") {
      const kv = env.TRAFFIC_KV;
      const latest = kv ? await kv.get("latest_payload", "json") : null;
      return jsonResponse({
        ok: true,
        collect_mode: env.COLLECT_MODE || "free_chunked",
        kv_bound: Boolean(kv),
        seoul_key_set: Boolean(env.SEOUL_API_KEY && env.SEOUL_API_KEY !== "sample"),
        its_key_set: Boolean(env.ITS_API_KEY),
        cf_radar_set: Boolean(env.CF_RADAR_TOKEN),
        gas_bridge_set: Boolean(env.GAS_INGEST_URL),
        catalog: { seoul_pois: SEOUL_121_POIS.length, nationwide_its: NATIONWIDE_42_ITS.length },
        last_updated: latest?.updated_at || null,
        cached_count: latest?.count || 0,
        radar_cached: Boolean(latest?.radar),
      });
    }

    if (!isAuthorized(request, env, url)) {
      return jsonResponse({ error: "Unauthorized: 유효한 token이 필요합니다." }, 401);
    }

    const kv = env.TRAFFIC_KV;

    if (mode === "radar") {
      const cached = kv ? await kv.get("radar_latest", "json") : null;
      if (cached) return jsonResponse({ mode: "radar", radar: cached });
      const fresh = await fetchCloudflareRadarKr(env, getKstStrings());
      if (fresh && kv) await kv.put("radar_latest", JSON.stringify(fresh), { expirationTtl: 1800 });
      return jsonResponse({ mode: "radar", radar: fresh });
    }

    if (mode === "dates") {
      if (!kv) return jsonResponse({ mode: "dates", dates: [], note: "KV 바인딩이 필요합니다." });
      const raw = (await kv.get("dates_index", "json")) || {};
      const dates = Object.values(raw).sort((a, b) => b.date.localeCompare(a.date));
      return jsonResponse({ mode: "dates", dates });
    }

    if (mode === "history") {
      const dateStr = (url.searchParams.get("date") || "").trim();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
        return jsonResponse({ error: "date 파라미터는 YYYY-MM-DD 형식이어야 합니다." }, 400);
      }
      if (!kv) return jsonResponse({ mode: "history", date: dateStr, times: [], snapshots: {} });
      const data = await kv.get(`history:${dateStr}`, "json");
      return jsonResponse(data || { mode: "history", date: dateStr, times: [], snapshots: {} });
    }

    if (mode === "collect") {
      const forced = url.searchParams.get("full") === "1" ? "full" : undefined;
      const res = await collectScheduled(env, forced);
      return jsonResponse({ ok: true, ...res });
    }

    // Default: mode === "latest"
    if (kv) {
      const cached = await kv.get("latest_payload", "json");
      if (cached && Array.isArray(cached.data) && cached.data.length > 0) {
        return jsonResponse(cached, 200, { "Cache-Control": "public, max-age=30" });
      }
      // 아직 Cron 실행 전이면 1회 샤드 수집 후 반환
      await collectScheduled(env);
      const fresh = await kv.get("latest_payload", "json");
      if (fresh) return jsonResponse(fresh);
    }

    // KV 미연결 시 Edge Cache(5분) 활용 즉석 모드
    const cache = caches.default;
    const cacheKey = new Request(`${url.origin}/__ondemand_latest_v1`);
    const edgeHit = await cache.match(cacheKey);
    if (edgeHit) return edgeHit;

    const onDemand = await collectOnDemandNoKv(env);
    const resp = jsonResponse(onDemand, 200, { "Cache-Control": "public, max-age=300" });
    ctx.waitUntil(cache.put(cacheKey, resp.clone()));
    return resp;
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(collectScheduled(env));
  },
};
