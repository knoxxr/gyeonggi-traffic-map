# Cloudflare Worker 하이브리드 실시간 프록시 (서울 121곳 + 전국 ITS 44권역)

이 Worker를 배포하면 **서울시 실시간 유동인구 121곳 전체(SKT·KT 기지국 기반)**와 **전국 16개 시·도 44개 주요 권역 ITS 도로 혼잡도**를 자동으로 수집·캐싱하고, CORS가 허용된 단일 JSON API로 **한국 모바일 트래픽 모니터**에 공급합니다.

---

## 핵심 장점
- **무료($0/월) 완전 지원 (`free_chunked`)**: Cloudflare 무료 플랜의 호출당 50 subrequest 한도를 넘지 않도록 165곳을 4개 샤드(41·41·39·44곳)로 나눠 2분마다 순환 수집 → **8분마다 전국 165곳 전체 갱신**.
- **유료 플랜 5분 일괄 수집 (`full`)**: `wrangler.toml`에서 `COLLECT_MODE = "full"`, `crons = ["*/5 * * * *"]`로 바꾸면 5분마다 165곳 전체를 3초 내 일괄 수집.
- **KV 90일 히스토리 내장**: `TRAFFIC_KV`를 연결하면 앱의 **`Drive 히스토리`** 탭에서 날짜별 24시간 재생이 그대로 동작합니다.
- **동시 접속 한도 완벽 보호 (3-Tier 캐시 + Single-Flight + ETag 304)**:
  - 업스트림 API(서울·ITS·Radar)는 **Cron 트리거에서만 호출**되므로 접속자가 10명이든 10만 명이든 공공 API 호출 횟수는 0으로 고정됩니다.
  - 요청은 **L1 Isolate RAM 캐시(60초) → L2 Cloudflare Edge Cache(120초, 과거 히스토리 6시간) → L3 KV** 순으로 처리되고, 동시 미스는 Single-Flight로 1회만 읽어 KV 무료 한도(일 10만 읽기)도 안전합니다.
- **Google Drive 스프레드시트 자동 연동(선택)**: `GAS_INGEST_URL` 시크릿에 `gas/Code.gs` 웹 앱 URL을 넣어두면, Worker가 165곳을 모을 때마다 Google 스프레드시트로 1회 POST 전달 → **Apps Script 무료 호출 한도(일 2만회)를 0회만 쓰면서 내 Google Drive에 5~8분 간격으로 영구 누적**됩니다.

---

## 3분 배포 방법

### 1) 설치 & 로그인
```bash
cd worker
npm install
npx wrangler login
```

### 2) KV 네임스페이스 생성 (전체 121+44곳 캐싱 & 히스토리용)
```bash
npx wrangler kv namespace create TRAFFIC_KV
```
출력된 `id`를 `worker/wrangler.toml` 하단의 `[[kv_namespaces]]` 주석을 풀어 입력합니다.

### 3) 무료 API 키 시크릿 등록
```bash
# 서울 열린데이터광장 인증키 (https://data.seoul.go.kr/together/mypage/actkeyMain.do)
npx wrangler secret put SEOUL_API_KEY

# 국가교통정보센터 ITS 인증키 (https://www.its.go.kr/opendata/opendataList)
npx wrangler secret put ITS_API_KEY

# (선택) Cloudflare Radar 대한민국 실시간 모바일·NetFlow 트래픽 지수 (무료)
# Cloudflare 대시보드 > My Profile > API Tokens > Create Custom Token (Account > Radar > Read)
npx wrangler secret put CF_RADAR_TOKEN

# (선택) Google Drive 스프레드시트에도 동시에 영구 적재하고 싶다면:
npx wrangler secret put GAS_INGEST_URL
```

### 4) 배포
```bash
npm run deploy
```

배포 후 출력되는 주소(예: `https://korea-traffic-hybrid-proxy.<계정>.workers.dev`)를 확인합니다.
- 상태 확인: `https://<워커주소>/?mode=health`
- 최신 데이터: `https://<워커주소>/?mode=latest`
- 히스토리 날짜: `https://<워커주소>/?mode=dates`
- 특정 날짜 재생: `https://<워커주소>/?mode=history&date=2026-11-23`

### 5) 지도 앱 연결
**한국 모바일 트래픽 모니터** 상단 **`실시간 API · 데이터`**(모바일 **`API·CSV`**) 클릭:
- **실시간**: `실시간 API 폴링` 탭에 워커 URL 붙여넣기 → **`실시간 API 폴링 시작`**
- **히스토리**: `Drive 히스토리` 탭에 동일한 워커 URL(또는 GAS URL) 붙여넣기 → **`저장된 날짜 조회`** → 날짜 선택 후 재생!
