/**
 * 한국 모바일 트래픽 모니터 — Google Sheets 실시간 유동인구·ITS 수집기 & 히스토리 API
 *
 * 기능:
 *  1. 서울시 실시간 인구데이터 API (OA-21778 · SKT/KT 기지국 기반 5분 갱신) 수집
 *  2. 국가교통정보센터(ITS) 실시간 교통소통정보 API (전국 주요 권역) 수집
 *  3. Google 스프레드시트에 자동 누적:
 *     - [설정]: API 키 및 수집 옵션 관리
 *     - [장소목록]: 서울 주요 핫스팟 + 전국 ITS 권역 목록 (행 추가/ON·OFF 가능)
 *     - [최신현황]: 가장 최근 수집 결과 (혼잡도·인구순 정렬, 엑셀처럼 바로 조회·차트화)
 *     - [히스토리_YYYYMM]: 월별 시트에 시간순 영구 누적 (10M 셀 한도 방지)
 *  4. 웹 앱(doGet) 배포 시 앱에 바로 연결 가능한 JSON API 제공:
 *     - ?mode=latest                : 최신 스냅샷 + 저장된 날짜 목록
 *     - ?mode=dates                 : 히스토리 보유 날짜 목록
 *     - ?mode=history&date=YYYY-MM-DD : 특정 날짜의 전체 시간대 히스토리
 */

const SHEET_CONFIG = "설정";
const SHEET_POIS = "장소목록";
const SHEET_LATEST = "최신현황";
const HISTORY_PREFIX = "히스토리_";
const TZ = "Asia/Seoul";

// ============================================================================
// 1. 기본 장소 마스터 (서울 공식 실시간 인구 POI + 전국 16개 시·도 ITS 권역)
//    [사용여부, 구분, 코드, 장소명, 경도, 위도, 반경도(ITS bbox 절반), 기준속도(km/h)]
// ============================================================================
const DEFAULT_POIS = [
  ["Y", "seoul_pop", "SP001", "경복궁", 126.977, 37.5796, 0, 0],
  ["Y", "seoul_pop", "SP002", "광화문·덕수궁", 126.9768, 37.5698, 0, 0],
  ["Y", "seoul_pop", "SP003", "보신각", 126.9837, 37.5698, 0, 0],
  ["Y", "seoul_pop", "SP004", "서울 암사동 유적", 127.1303, 37.5602, 0, 0],
  ["Y", "seoul_pop", "SP005", "창덕궁·종묘", 126.991, 37.5794, 0, 0],
  ["Y", "seoul_pop", "SP006", "강남 MICE 관광특구", 127.0592, 37.5117, 0, 0],
  ["Y", "seoul_pop", "SP007", "동대문 관광특구", 127.0095, 37.5665, 0, 0],
  ["Y", "seoul_pop", "SP008", "명동 관광특구", 126.9849, 37.5636, 0, 0],
  ["Y", "seoul_pop", "SP009", "이태원 관광특구", 126.9945, 37.5345, 0, 0],
  ["Y", "seoul_pop", "SP010", "잠실 관광특구", 127.1002, 37.5133, 0, 0],
  ["Y", "seoul_pop", "SP011", "종로·청계 관광특구", 126.9896, 37.5704, 0, 0],
  ["Y", "seoul_pop", "SP012", "홍대 관광특구", 126.9237, 37.5559, 0, 0],
  ["Y", "seoul_pop", "SP013", "강서한강공원", 126.8172, 37.5867, 0, 0],
  ["Y", "seoul_pop", "SP014", "고척돔", 126.8671, 37.4982, 0, 0],
  ["Y", "seoul_pop", "SP015", "광나루한강공원", 127.1213, 37.5476, 0, 0],
  ["Y", "seoul_pop", "SP016", "광화문광장", 126.9769, 37.5724, 0, 0],
  ["Y", "seoul_pop", "SP017", "국립중앙박물관·용산가족공원", 126.9804, 37.5239, 0, 0],
  ["Y", "seoul_pop", "SP018", "난지한강공원", 126.8764, 37.5668, 0, 0],
  ["Y", "seoul_pop", "SP019", "남산공원", 126.9882, 37.5512, 0, 0],
  ["Y", "seoul_pop", "SP020", "노들섬", 126.9581, 37.5177, 0, 0],
  ["Y", "seoul_pop", "SP021", "뚝섬한강공원", 127.0699, 37.5293, 0, 0],
  ["Y", "seoul_pop", "SP022", "망원한강공원", 126.8946, 37.5554, 0, 0],
  ["Y", "seoul_pop", "SP023", "반포한강공원", 126.9959, 37.5105, 0, 0],
  ["Y", "seoul_pop", "SP024", "보라매공원", 126.9189, 37.4926, 0, 0],
  ["Y", "seoul_pop", "SP025", "북서울꿈의숲", 127.0413, 37.6205, 0, 0],
  ["Y", "seoul_pop", "SP026", "서대문독립공원", 126.9562, 37.5744, 0, 0],
  ["Y", "seoul_pop", "SP027", "서리풀공원·몽마르뜨공원", 127.0031, 37.4946, 0, 0],
  ["Y", "seoul_pop", "SP028", "서울대공원", 127.0155, 37.4275, 0, 0],
  ["Y", "seoul_pop", "SP029", "서울숲공원", 127.0374, 37.5444, 0, 0],
  ["Y", "seoul_pop", "SP030", "송현녹지광장", 126.9815, 37.5764, 0, 0],
  ["Y", "seoul_pop", "SP031", "아차산", 127.103, 37.5671, 0, 0],
  ["Y", "seoul_pop", "SP032", "안양천", 126.8794, 37.5197, 0, 0],
  ["Y", "seoul_pop", "SP033", "양화한강공원", 126.9023, 37.5382, 0, 0],
  ["Y", "seoul_pop", "SP034", "어린이대공원", 127.0819, 37.5482, 0, 0],
  ["Y", "seoul_pop", "SP035", "여의도한강공원", 126.9344, 37.5284, 0, 0],
  ["Y", "seoul_pop", "SP036", "여의서로", 126.9212, 37.5312, 0, 0],
  ["Y", "seoul_pop", "SP037", "올림픽공원", 127.1216, 37.5209, 0, 0],
  ["Y", "seoul_pop", "SP038", "월드컵공원", 126.8973, 37.5682, 0, 0],
  ["Y", "seoul_pop", "SP039", "응봉산", 127.0303, 37.5481, 0, 0],
  ["Y", "seoul_pop", "SP040", "이촌한강공원", 126.9704, 37.5174, 0, 0],
  ["Y", "seoul_pop", "SP041", "잠실종합운동장", 127.0737, 37.515, 0, 0],
  ["Y", "seoul_pop", "SP042", "잠실한강공원", 127.0825, 37.518, 0, 0],
  ["Y", "seoul_pop", "SP043", "잠원한강공원", 127.0119, 37.5196, 0, 0],
  ["Y", "seoul_pop", "SP044", "청계산", 127.0542, 37.4342, 0, 0],
  ["Y", "seoul_pop", "SP045", "홍제폭포", 126.9369, 37.5813, 0, 0],
  ["Y", "seoul_pop", "SP046", "가락시장", 127.1112, 37.4932, 0, 0],
  ["Y", "seoul_pop", "SP047", "가로수길", 127.0229, 37.5208, 0, 0],
  ["Y", "seoul_pop", "SP048", "광장(전통)시장", 126.9993, 37.5701, 0, 0],
  ["Y", "seoul_pop", "SP049", "김포공항", 126.8013, 37.5587, 0, 0],
  ["Y", "seoul_pop", "SP050", "남대문시장", 126.9776, 37.5592, 0, 0],
  ["Y", "seoul_pop", "SP051", "노량진", 126.9424, 37.5135, 0, 0],
  ["Y", "seoul_pop", "SP052", "덕수궁길·정동길", 126.974, 37.5663, 0, 0],
  ["Y", "seoul_pop", "SP053", "북창동 먹자골목", 126.9774, 37.5632, 0, 0],
  ["Y", "seoul_pop", "SP054", "북촌한옥마을", 126.9849, 37.5826, 0, 0],
  ["Y", "seoul_pop", "SP055", "서촌", 126.9708, 37.5795, 0, 0],
  ["Y", "seoul_pop", "SP056", "성수카페거리", 127.0558, 37.5445, 0, 0],
  ["Y", "seoul_pop", "SP057", "송리단길·호수단길", 127.1072, 37.5085, 0, 0],
  ["Y", "seoul_pop", "SP058", "신촌 스타광장", 126.9368, 37.5562, 0, 0],
  ["Y", "seoul_pop", "SP059", "압구정로데오거리", 127.0391, 37.5273, 0, 0],
  ["Y", "seoul_pop", "SP060", "여의도", 126.9245, 37.5219, 0, 0],
  ["Y", "seoul_pop", "SP061", "연남동", 126.925, 37.5625, 0, 0],
  ["Y", "seoul_pop", "SP062", "영등포 타임스퀘어", 126.9032, 37.5171, 0, 0],
  ["Y", "seoul_pop", "SP063", "용리단길", 126.9706, 37.5305, 0, 0],
  ["Y", "seoul_pop", "SP064", "이태원 앤틱가구거리", 126.9935, 37.5326, 0, 0],
  ["Y", "seoul_pop", "SP065", "익선동", 126.9897, 37.5743, 0, 0],
  ["Y", "seoul_pop", "SP066", "인사동", 126.9859, 37.5742, 0, 0],
  ["Y", "seoul_pop", "SP067", "잠실롯데타워·석촌호수", 127.1025, 37.5126, 0, 0],
  ["Y", "seoul_pop", "SP068", "창동 신경제 중심지", 127.0478, 37.6532, 0, 0],
  ["Y", "seoul_pop", "SP069", "청담동 명품거리", 127.0468, 37.5247, 0, 0],
  ["Y", "seoul_pop", "SP070", "청량리 제기동 일대 전통시장", 127.0413, 37.5806, 0, 0],
  ["Y", "seoul_pop", "SP071", "해방촌·경리단길", 126.9873, 37.5416, 0, 0],
  ["Y", "seoul_pop", "SP072", "DDP(동대문디자인플라자)", 127.0095, 37.5665, 0, 0],
  ["Y", "seoul_pop", "SP073", "DMC(디지털미디어시티)", 126.8902, 37.5792, 0, 0],
  ["Y", "seoul_pop", "SP074", "가산디지털단지역", 126.8825, 37.4815, 0, 0],
  ["Y", "seoul_pop", "SP075", "강남역", 127.0276, 37.4979, 0, 0],
  ["Y", "seoul_pop", "SP076", "건대입구역", 127.0695, 37.5404, 0, 0],
  ["Y", "seoul_pop", "SP077", "고덕역", 127.1542, 37.555, 0, 0],
  ["Y", "seoul_pop", "SP078", "고속터미널역", 127.0049, 37.5049, 0, 0],
  ["Y", "seoul_pop", "SP079", "교대역", 127.0141, 37.4934, 0, 0],
  ["Y", "seoul_pop", "SP080", "구로디지털단지역", 126.9015, 37.4853, 0, 0],
  ["Y", "seoul_pop", "SP081", "구로역", 126.8822, 37.5031, 0, 0],
  ["Y", "seoul_pop", "SP082", "군자역", 127.0795, 37.5572, 0, 0],
  ["Y", "seoul_pop", "SP083", "대림역", 126.8958, 37.4932, 0, 0],
  ["Y", "seoul_pop", "SP084", "동대문역", 127.0097, 37.5714, 0, 0],
  ["Y", "seoul_pop", "SP085", "뚝섬역", 127.0474, 37.5472, 0, 0],
  ["Y", "seoul_pop", "SP086", "미아사거리역", 127.03, 37.6132, 0, 0],
  ["Y", "seoul_pop", "SP087", "발산역", 126.8377, 37.5586, 0, 0],
  ["Y", "seoul_pop", "SP088", "사당역", 126.9816, 37.4765, 0, 0],
  ["Y", "seoul_pop", "SP089", "삼각지역", 126.9731, 37.5347, 0, 0],
  ["Y", "seoul_pop", "SP090", "서울대입구역", 126.9527, 37.4812, 0, 0],
  ["Y", "seoul_pop", "SP091", "서울식물원·마곡나루역", 126.8295, 37.5673, 0, 0],
  ["Y", "seoul_pop", "SP092", "서울역", 126.9707, 37.5547, 0, 0],
  ["Y", "seoul_pop", "SP093", "선릉역", 127.049, 37.5045, 0, 0],
  ["Y", "seoul_pop", "SP094", "성신여대입구역", 127.0165, 37.5927, 0, 0],
  ["Y", "seoul_pop", "SP095", "수유역", 127.0257, 37.638, 0, 0],
  ["Y", "seoul_pop", "SP096", "숭례문", 126.9753, 37.56, 0, 0],
  ["Y", "seoul_pop", "SP097", "시의회 앞", 126.9765, 37.5668, 0, 0],
  ["Y", "seoul_pop", "SP098", "신논현역·논현역", 127.0249, 37.5048, 0, 0],
  ["Y", "seoul_pop", "SP099", "신도림역", 126.8912, 37.5088, 0, 0],
  ["Y", "seoul_pop", "SP100", "신림역", 126.9297, 37.4842, 0, 0],
  ["Y", "seoul_pop", "SP101", "신정네거리역", 126.8529, 37.5201, 0, 0],
  ["Y", "seoul_pop", "SP102", "신촌·이대역", 126.9415, 37.5567, 0, 0],
  ["Y", "seoul_pop", "SP103", "쌍문역", 127.0347, 37.6486, 0, 0],
  ["Y", "seoul_pop", "SP104", "양재역", 127.0346, 37.4841, 0, 0],
  ["Y", "seoul_pop", "SP105", "역삼역", 127.0364, 37.5006, 0, 0],
  ["Y", "seoul_pop", "SP106", "연신내역", 126.9211, 37.619, 0, 0],
  ["Y", "seoul_pop", "SP107", "오목교역·목동운동장", 126.8752, 37.5245, 0, 0],
  ["Y", "seoul_pop", "SP108", "왕십리역", 127.0377, 37.5615, 0, 0],
  ["Y", "seoul_pop", "SP109", "용산역", 126.9648, 37.5299, 0, 0],
  ["Y", "seoul_pop", "SP110", "이태원역", 126.9946, 37.5345, 0, 0],
  ["Y", "seoul_pop", "SP111", "잠실새내역", 127.0862, 37.5117, 0, 0],
  ["Y", "seoul_pop", "SP112", "잠실역", 127.1001, 37.5133, 0, 0],
  ["Y", "seoul_pop", "SP113", "장지역", 127.1262, 37.4787, 0, 0],
  ["Y", "seoul_pop", "SP114", "장한평역", 127.0644, 37.5614, 0, 0],
  ["Y", "seoul_pop", "SP115", "천호역", 127.1238, 37.5387, 0, 0],
  ["Y", "seoul_pop", "SP116", "총신대입구(이수)역", 126.9817, 37.4867, 0, 0],
  ["Y", "seoul_pop", "SP117", "충정로역", 126.9637, 37.5599, 0, 0],
  ["Y", "seoul_pop", "SP118", "합정역", 126.9138, 37.5495, 0, 0],
  ["Y", "seoul_pop", "SP119", "혜화역", 127.0019, 37.5821, 0, 0],
  ["Y", "seoul_pop", "SP120", "홍대입구역(2호선)", 126.9238, 37.5572, 0, 0],
  ["Y", "seoul_pop", "SP121", "회기역", 127.0576, 37.5897, 0, 0],
  ["Y", "its", "ITS_ICN_SD", "인천 송도국제도시", 126.647, 37.386, 0.05, 58],
  ["Y", "its", "ITS_ICN_BP", "인천 부평·구월 도심", 126.716, 37.471, 0.05, 52],
  ["Y", "its", "ITS_ICN_AP", "인천국제공항·영종", 126.4407, 37.4602, 0.07, 75],
  ["Y", "its", "ITS_GG_PG", "경기 판교·분당", 127.11, 37.394, 0.05, 62],
  ["Y", "its", "ITS_GG_SW", "경기 수원·광교", 127.032, 37.274, 0.05, 55],
  ["Y", "its", "ITS_GG_DT", "경기 화성 동탄·기흥", 127.095, 37.205, 0.06, 65],
  ["Y", "its", "ITS_GG_IS", "경기 고양 일산·킨텍스", 126.765, 37.66, 0.06, 58],
  ["Y", "its", "ITS_GG_BC", "경기 부천·광명·안양", 126.86, 37.445, 0.06, 54],
  ["Y", "its", "ITS_GG_UJ", "경기 의정부·남양주", 127.06, 37.735, 0.06, 58],
  ["Y", "its", "ITS_GG_PT", "경기 평택·고덕", 127.06, 37.03, 0.07, 68],
  ["Y", "its", "ITS_GG_YI", "경기 용인·에버랜드", 127.2025, 37.2942, 0.06, 65],
  ["Y", "its", "ITS_GG_GP", "경기 가평·양평 나들이축", 127.44, 37.66, 0.09, 68],
  ["Y", "its", "ITS_BS_SM", "부산 서면·도심", 129.0596, 35.1578, 0.05, 50],
  ["Y", "its", "ITS_BS_HU", "부산 해운대·센텀", 129.1586, 35.1631, 0.05, 52],
  ["Y", "its", "ITS_BS_ST", "부산역·남포·영도", 129.036, 35.105, 0.05, 50],
  ["Y", "its", "ITS_BS_GS", "부산 김해공항·명지·녹산", 128.92, 35.125, 0.06, 62],
  ["Y", "its", "ITS_DG_DS", "대구 동성로·수성", 128.6014, 35.8714, 0.06, 54],
  ["Y", "its", "ITS_DG_DD", "대구 동대구·혁신도시", 128.66, 35.878, 0.06, 60],
  ["Y", "its", "ITS_DJ_DS", "대전 둔산·유성", 127.3845, 36.3504, 0.06, 56],
  ["Y", "its", "ITS_DJ_ST", "대전역·복합터미널", 127.4342, 36.3325, 0.05, 54],
  ["Y", "its", "ITS_GJ_SM", "광주 상무·충장", 126.8526, 35.1595, 0.06, 54],
  ["Y", "its", "ITS_GJ_SJ", "광주 송정·첨단산단", 126.82, 35.19, 0.06, 58],
  ["Y", "its", "ITS_US_SS", "울산 삼산·현대차권", 129.339, 35.5384, 0.06, 56],
  ["Y", "its", "ITS_US_UJ", "울산 KTX·언양", 129.138, 35.5515, 0.07, 68],
  ["Y", "its", "ITS_SJ_CT", "세종 정부청사·나성", 127.265, 36.495, 0.06, 62],
  ["Y", "its", "ITS_GW_CC", "강원 춘천 도심", 127.7298, 37.8813, 0.07, 62],
  ["Y", "its", "ITS_GW_GN", "강원 강릉·경포·속초축", 128.896, 37.764, 0.08, 65],
  ["Y", "its", "ITS_GW_WJ", "강원 원주 혁신·기업", 127.945, 37.342, 0.07, 68],
  ["Y", "its", "ITS_CB_CJ", "충북 청주·오송", 127.46, 36.6424, 0.07, 62],
  ["Y", "its", "ITS_CB_CH", "충북 충주·제천", 127.926, 36.991, 0.08, 68],
  ["Y", "its", "ITS_CN_CA", "충남 천안·아산", 127.1139, 36.8151, 0.07, 65],
  ["Y", "its", "ITS_CN_SS", "충남 당진·서산·내포", 126.63, 36.785, 0.08, 68],
  ["Y", "its", "ITS_JB_JJ", "전북 전주·혁신도시", 127.148, 35.8242, 0.07, 58],
  ["Y", "its", "ITS_JB_GS", "전북 군산·익산", 126.86, 35.955, 0.08, 64],
  ["Y", "its", "ITS_JN_YS", "전남 여수·순천·광양", 127.6622, 34.7604, 0.08, 65],
  ["Y", "its", "ITS_JN_MP", "전남 목포·무안·나주", 126.52, 34.92, 0.09, 64],
  ["Y", "its", "ITS_GB_PH", "경북 포항·영일대", 129.365, 36.019, 0.07, 62],
  ["Y", "its", "ITS_GB_GJ", "경북 경주 보문·황리단", 129.2247, 35.8428, 0.07, 64],
  ["Y", "its", "ITS_GB_GM", "경북 구미·안동", 128.3446, 36.1195, 0.08, 65],
  ["Y", "its", "ITS_GN_CW", "경남 창원·마산·김해", 128.6811, 35.228, 0.07, 58],
  ["Y", "its", "ITS_GN_JJ", "경남 진주 혁신도시", 128.1076, 35.18, 0.07, 64],
  ["Y", "its", "ITS_GN_GJ", "경남 거제·통영", 128.6211, 34.8806, 0.08, 62],
  ["Y", "its", "ITS_JJ_CT", "제주 제주시·공항", 126.51, 33.4996, 0.07, 52],
  ["Y", "its", "ITS_JJ_SG", "제주 서귀포·중문", 126.5, 33.2541, 0.08, 56],
];

// ============================================================================
// 2. 스프레드시트 커스텀 메뉴
// ============================================================================
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("📡 트래픽·유동인구 수집기")
    .addItem("1. 시트 초기 설정 (탭·장소목록 생성)", "setupSheets")
    .addItem("2. 지금 1회 수집 실행", "collectAllNow")
    .addSeparator()
    .addItem("3. 자동 수집 시작 (15분마다 · 무료 안전)", "startTrigger15")
    .addItem("4. 자동 수집 시작 (10분마다 · 일 2만회 주의)", "startTrigger10")
    .addItem("5. 자동 수집 중지", "stopTriggers")
    .addSeparator()
    .addItem("6. API 키·응답 진단 테스트", "testEndpoints")
    .addToUi();
}

// ============================================================================
// 3. 시트 초기 설정
// ============================================================================
function setupSheets() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // 1) 설정 시트
  let cfg = ss.getSheetByName(SHEET_CONFIG);
  if (!cfg) {
    cfg = ss.insertSheet(SHEET_CONFIG, 0);
  }
  if (cfg.getLastRow() < 2) {
    const rows = [
      ["항목", "값", "설명"],
      ["SEOUL_API_KEY", "sample", "서울 열린데이터광장 인증키 (무료 발급: data.seoul.go.kr · sample은 광화문·덕수궁 1곳만 조회)"],
      ["ITS_API_KEY", "test", "국가교통정보센터 ITS 인증키 (무료 발급: its.go.kr · test는 데모용)"],
      ["수집_서울인구", "Y", "서울시 실시간 유동인구 수집 여부 (Y/N)"],
      ["수집_전국ITS", "Y", "전국 주요 권역 ITS 도로 혼잡도 수집 여부 (Y/N)"],
      ["허용_토큰", "", "(선택) 비워두면 공개 URL, 입력 시 웹앱 요청에 ?token=값 필요"],
      ["최근_수집시각", "-", "자동 기록"],
      ["최근_수집건수", "0", "자동 기록"],
    ];
    cfg.clear();
    cfg.getRange(1, 1, rows.length, 3).setValues(rows);
    cfg.getRange(1, 1, 1, 3).setFontWeight("bold").setBackground("#1e3a8a").setFontColor("#ffffff");
    cfg.setColumnWidth(1, 170);
    cfg.setColumnWidth(2, 300);
    cfg.setColumnWidth(3, 520);
    cfg.setFrozenRows(1);
  }

  // 2) 장소목록 시트
  let pois = ss.getSheetByName(SHEET_POIS);
  if (!pois) {
    pois = ss.insertSheet(SHEET_POIS, 1);
  }
  if (pois.getLastRow() < 2) {
    const header = [["사용(Y/N)", "구분", "코드", "장소명", "경도", "위도", "ITS_반경(도)", "ITS_기준속도"]];
    const body = header.concat(DEFAULT_POIS);
    pois.clear();
    pois.getRange(1, 1, body.length, 8).setValues(body);
    pois.getRange(1, 1, 1, 8).setFontWeight("bold").setBackground("#1e3a8a").setFontColor("#ffffff");
    pois.setFrozenRows(1);
    pois.autoResizeColumns(1, 8);
  }

  // 3) 최신현황 시트
  let latest = ss.getSheetByName(SHEET_LATEST);
  if (!latest) {
    latest = ss.insertSheet(SHEET_LATEST, 2);
  }
  if (latest.getLastRow() < 1) {
    const lHeader = [["수집시각", "구분", "코드", "장소명", "경도", "위도", "유동인구_최소", "유동인구_최대", "추정인구", "혼잡도", "평균속도(km/h)", "트래픽환산(Gbps)", "비고"]];
    latest.getRange(1, 1, 1, lHeader[0].length).setValues(lHeader);
    latest.getRange(1, 1, 1, lHeader[0].length).setFontWeight("bold").setBackground("#0f766e").setFontColor("#ffffff");
    latest.setFrozenRows(1);
  }

  SpreadsheetApp.getUi().alert(
    "초기 설정 완료!\n\n" +
    "1) [설정] 탭에 서울시 API 키와 ITS API 키를 입력하세요.\n" +
    "2) 메뉴에서 [2. 지금 1회 수집 실행]으로 테스트하세요.\n" +
    "3) 메뉴에서 [3. 자동 수집 시작]을 누르면 드라이브에 자동 누적됩니다."
  );
}

function readConfig_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const cfgSheet = ss.getSheetByName(SHEET_CONFIG);
  const map = {};
  if (!cfgSheet) return map;
  const vals = cfgSheet.getDataRange().getValues();
  for (let i = 1; i < vals.length; i++) {
    const k = String(vals[i][0] || "").trim();
    if (k) map[k] = String(vals[i][1] == null ? "" : vals[i][1]).trim();
  }
  return map;
}

function updateConfigValue_(key, value) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const cfgSheet = ss.getSheetByName(SHEET_CONFIG);
  if (!cfgSheet) return;
  const vals = cfgSheet.getDataRange().getValues();
  for (let i = 1; i < vals.length; i++) {
    if (String(vals[i][0]).trim() === key) {
      cfgSheet.getRange(i + 1, 2).setValue(value);
      return;
    }
  }
}

function loadActivePois_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SHEET_POIS);
  if (!sh || sh.getLastRow() < 2) return [];
  const rows = sh.getRange(2, 1, sh.getLastRow() - 1, 8).getValues();
  const list = [];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (String(r[0]).trim().toUpperCase() !== "Y") continue;
    list.push({
      source: String(r[1]).trim(),
      code: String(r[2]).trim(),
      name: String(r[3]).trim(),
      lon: Number(r[4]),
      lat: Number(r[5]),
      radiusDeg: Number(r[6]) || 0.06,
      baseSpeed: Number(r[7]) || 58,
    });
  }
  return list;
}

// ============================================================================
// 4. 실시간 데이터 수집 (서울 121곳 유동인구 + 전국 ITS 도로혼잡)
// ============================================================================
function collectAllNow() {
  const res = collectAll();
  try {
    SpreadsheetApp.getUi().alert(
      `수집 완료 (${res.timestamp})\n` +
      `• 서울 유동인구: ${res.seoulCount}곳\n` +
      `• 전국 ITS 교통: ${res.itsCount}곳\n` +
      `• [최신현황] 및 [${res.historySheet}] 탭에 저장되었습니다.`
    );
  } catch (_) {}
}

function collectAll() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) {
    return { timestamp: "", seoulCount: 0, itsCount: 0, historySheet: "" };
  }
  try {
    const cfg = readConfig_();
    const pois = loadActivePois_();
    const now = new Date();
    const tsFull = Utilities.formatDate(now, TZ, "yyyy-MM-dd HH:mm");
    const dateStr = Utilities.formatDate(now, TZ, "yyyy-MM-dd");
    const hmStr = Utilities.formatDate(now, TZ, "HH:mm");
    const ymStr = Utilities.formatDate(now, TZ, "yyyyMM");

    const results = [];
    let seoulCount = 0;
    let itsCount = 0;

    // 1) 서울시 실시간 인구데이터 수집 (25개씩 병렬 배치 호출)
    if ((cfg["수집_서울인구"] || "Y").toUpperCase() === "Y" && cfg["SEOUL_API_KEY"]) {
      const key = cfg["SEOUL_API_KEY"];
      let seoulPois = pois.filter((p) => p.source === "seoul_pop");
      // sample 키는 광화문·덕수궁(POI009) 1곳만 지원
      if (key.toLowerCase() === "sample") {
        seoulPois = seoulPois.filter((p) => p.name === "광화문·덕수궁");
      }
      const fetched = fetchSeoulPopBatch_(seoulPois, key, tsFull);
      seoulCount = fetched.length;
      for (let i = 0; i < fetched.length; i++) results.push(fetched[i]);
    }

    // 2) 전국 ITS 실시간 교통소통정보 수집 (16개씩 병렬 배치 호출)
    if ((cfg["수집_전국ITS"] || "Y").toUpperCase() === "Y" && cfg["ITS_API_KEY"]) {
      const key = cfg["ITS_API_KEY"];
      const itsPois = pois.filter((p) => p.source === "its");
      const fetched = fetchItsBatch_(itsPois, key, tsFull);
      itsCount = fetched.length;
      for (let i = 0; i < fetched.length; i++) results.push(fetched[i]);
    }

    // 3) 정렬 (서울 혼잡도·인구 상위 -> 전국 정체 상위)
    const congestRank = { "붐빔": 4, "정체": 4, "약간 붐빔": 3, "서행": 3, "보통": 2, "여유": 1, "원활": 1 };
    results.sort((a, b) => {
      const ra = congestRank[a.congest] || 0;
      const rb = congestRank[b.congest] || 0;
      if (rb !== ra) return rb - ra;
      return (b.pop || b.traffic_gbps * 1000) - (a.pop || a.traffic_gbps * 1000);
    });

    // 4) [최신현황] 시트 갱신
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const latestSheet = ss.getSheetByName(SHEET_LATEST) || ss.insertSheet(SHEET_LATEST);
    const lHeader = [["수집시각", "구분", "코드", "장소명", "경도", "위도", "유동인구_최소", "유동인구_최대", "추정인구", "혼잡도", "평균속도(km/h)", "트래픽환산(Gbps)", "비고"]];
    const lRows = results.map((r) => [
      r.ts, r.source, r.code, r.name, r.lon, r.lat,
      r.pop_min || "", r.pop_max || "", r.pop || "",
      r.congest, r.speed || "", r.traffic_gbps, r.note || ""
    ]);
    latestSheet.clearContents();
    const outLatest = lHeader.concat(lRows);
    latestSheet.getRange(1, 1, outLatest.length, lHeader[0].length).setValues(outLatest);

    // 5) 월별 히스토리 시트 [히스토리_YYYYMM]에 누적 추가
    const histName = HISTORY_PREFIX + ymStr;
    let histSheet = ss.getSheetByName(histName);
    if (!histSheet) {
      histSheet = ss.insertSheet(histName);
      const hHeader = [["수집시각", "날짜", "시분", "구분", "코드", "장소명", "경도", "위도", "추정인구", "혼잡도", "평균속도", "트래픽환산(Gbps)"]];
      histSheet.getRange(1, 1, 1, hHeader[0].length).setValues(hHeader);
      histSheet.getRange(1, 1, 1, hHeader[0].length).setFontWeight("bold").setBackground("#334155").setFontColor("#ffffff");
      histSheet.setFrozenRows(1);
    }
    if (results.length > 0) {
      const hRows = results.map((r) => [
        tsFull, dateStr, hmStr, r.source, r.code, r.name, r.lon, r.lat,
        r.pop || 0, r.congest, r.speed || 0, r.traffic_gbps
      ]);
      const startRow = histSheet.getLastRow() + 1;
      histSheet.getRange(startRow, 1, hRows.length, hRows[0].length).setValues(hRows);
    }

    updateConfigValue_("최근_수집시각", tsFull);
    updateConfigValue_("최근_수집건수", `${results.length} (서울 ${seoulCount} + ITS ${itsCount})`);

    // 캐시 무효화
    const cache = CacheService.getScriptCache();
    cache.remove("latest_payload");
    cache.remove("dates_payload");

    return { timestamp: tsFull, seoulCount, itsCount, historySheet: histName };
  } finally {
    lock.releaseLock();
  }
}

function fetchSeoulPopBatch_(pois, apiKey, tsFallback) {
  const out = [];
  const BATCH = 25;
  for (let i = 0; i < pois.length; i += BATCH) {
    const chunk = pois.slice(i, i + BATCH);
    const reqs = chunk.map((p) => ({
      url: `http://openapi.seoul.go.kr:8088/${encodeURIComponent(apiKey)}/json/citydata_ppltn/1/5/${encodeURIComponent(p.name)}`,
      muteHttpExceptions: true,
      followRedirects: true,
    }));
    let resps = [];
    try {
      resps = UrlFetchApp.fetchAll(reqs);
    } catch (e) {
      continue;
    }
    for (let j = 0; j < resps.length; j++) {
      const p = chunk[j];
      try {
        if (resps[j].getResponseCode() !== 200) continue;
        const txt = resps[j].getContentText("UTF-8");
        const parsed = parseSeoulPopResponse_(txt);
        if (!parsed) continue;
        const popMin = Number(parsed.AREA_PPLTN_MIN) || 0;
        const popMax = Number(parsed.AREA_PPLTN_MAX) || popMin;
        const popEst = Math.round((popMin + popMax) / 2);
        const congest = String(parsed.AREA_CONGEST_LVL || "보통").trim();
        // 유동인구 -> 모바일 트래픽 환산 (1인당 피크 평균 활동 기반 환산 지수, 지도 스케일 정합)
        const congestBoost = congest === "붐빔" ? 1.35 : congest === "약간 붐빔" ? 1.15 : congest === "여유" ? 0.85 : 1.0;
        const trafficGbps = Number(Math.max(1.2, (popEst / 1400) * congestBoost).toFixed(2));
        out.push({
          ts: String(parsed.PPLTN_TIME || tsFallback),
          source: "seoul_pop",
          code: p.code,
          name: String(parsed.AREA_NM || p.name),
          lon: p.lon,
          lat: p.lat,
          pop_min: popMin,
          pop_max: popMax,
          pop: popEst,
          congest: congest,
          speed: 0,
          traffic_gbps: trafficGbps,
          note: String(parsed.AREA_CONGEST_MSG || "").slice(0, 60),
        });
      } catch (_) {}
    }
  }
  return out;
}

function parseSeoulPopResponse_(txt) {
  if (!txt) return null;
  const trimmed = txt.trim();
  if (trimmed.startsWith("{")) {
    const json = JSON.parse(trimmed);
    const list = json["SeoulRtd.citydata_ppltn"];
    if (Array.isArray(list) && list.length > 0) return list[0];
    if (list && typeof list === "object") return list;
    return null;
  }
  if (trimmed.startsWith("<")) {
    const doc = XmlService.parse(trimmed);
    const root = doc.getRootElement();
    const node = root.getChild("SeoulRtd.citydata_ppltn");
    if (!node) return null;
    const get = (tag) => {
      const c = node.getChild(tag);
      return c ? c.getText() : "";
    };
    return {
      AREA_NM: get("AREA_NM"),
      AREA_CONGEST_LVL: get("AREA_CONGEST_LVL"),
      AREA_CONGEST_MSG: get("AREA_CONGEST_MSG"),
      AREA_PPLTN_MIN: get("AREA_PPLTN_MIN"),
      AREA_PPLTN_MAX: get("AREA_PPLTN_MAX"),
      PPLTN_TIME: get("PPLTN_TIME"),
    };
  }
  return null;
}

function fetchItsBatch_(pois, apiKey, tsFallback) {
  const out = [];
  const BATCH = 16;
  for (let i = 0; i < pois.length; i += BATCH) {
    const chunk = pois.slice(i, i + BATCH);
    const reqs = chunk.map((p) => {
      const d = p.radiusDeg || 0.06;
      const minX = (p.lon - d).toFixed(4);
      const maxX = (p.lon + d).toFixed(4);
      const minY = (p.lat - d * 0.85).toFixed(4);
      const maxY = (p.lat + d * 0.85).toFixed(4);
      return {
        url: `https://openapi.its.go.kr:9443/trafficInfo?apiKey=${encodeURIComponent(apiKey)}&type=all&drcType=all&minX=${minX}&maxX=${maxX}&minY=${minY}&maxY=${maxY}&getType=json`,
        muteHttpExceptions: true,
        followRedirects: true,
      };
    });
    let resps = [];
    try {
      resps = UrlFetchApp.fetchAll(reqs);
    } catch (e) {
      continue;
    }
    for (let j = 0; j < resps.length; j++) {
      const p = chunk[j];
      try {
        if (resps[j].getResponseCode() !== 200) continue;
        const json = JSON.parse(resps[j].getContentText("UTF-8"));
        const items = (json && json.body && json.body.items) ? json.body.items : [];
        if (!Array.isArray(items) || items.length === 0) continue;

        let sumSpd = 0, cnt = 0;
        for (let k = 0; k < items.length; k++) {
          const sp = Number(items[k].speed);
          if (sp > 1 && sp < 160) {
            sumSpd += sp;
            cnt++;
          }
        }
        if (cnt === 0) continue;
        const avgSpd = Number((sumSpd / cnt).toFixed(1));
        const ratio = avgSpd / (p.baseSpeed || 58);
        // 속도가 낮을수록 정체(유입 밀집) 높음
        const congest = ratio < 0.48 ? "정체" : ratio < 0.75 ? "서행" : "원활";
        const congestionIdx = Math.max(0.1, Math.min(1.6, 1.35 - ratio * 0.85));
        const trafficGbps = Number((12.0 * congestionIdx).toFixed(2));

        out.push({
          ts: tsFallback,
          source: "its",
          code: p.code,
          name: p.name,
          lon: p.lon,
          lat: p.lat,
          pop_min: 0,
          pop_max: 0,
          pop: 0,
          congest: congest,
          speed: avgSpd,
          traffic_gbps: trafficGbps,
          note: `링크 ${cnt}개 평균 ${avgSpd}km/h`,
        });
      } catch (_) {}
    }
  }
  return out;
}

// ============================================================================
// 5. 자동 수집 트리거 관리
// ============================================================================
function startTrigger15() {
  stopTriggers(true);
  ScriptApp.newTrigger("collectAll").timeBased().everyMinutes(15).create();
  SpreadsheetApp.getUi().alert("15분마다 자동 수집 트리거가 등록되었습니다.\n(서울+전국 ITS 조합 기준 일 ~10,000회 호출 · 무료 한도 안전)");
}

function startTrigger10() {
  stopTriggers(true);
  ScriptApp.newTrigger("collectAll").timeBased().everyMinutes(10).create();
  SpreadsheetApp.getUi().alert("10분마다 자동 수집 트리거가 등록되었습니다.\n(무료 Gmail 일 20,000회 한도를 초과하지 않도록 활성 장소를 130곳 이하로 유지하세요)");
}

function stopTriggers(silent) {
  const triggers = ScriptApp.getProjectTriggers();
  let removed = 0;
  for (let i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "collectAll") {
      ScriptApp.deleteTrigger(triggers[i]);
      removed++;
    }
  }
  if (!silent) {
    SpreadsheetApp.getUi().alert(`자동 수집 트리거 ${removed}개를 중지했습니다.`);
  }
}

function testEndpoints() {
  const cfg = readConfig_();
  const lines = [];
  try {
    const sKey = cfg["SEOUL_API_KEY"] || "sample";
    const r1 = UrlFetchApp.fetch(`http://openapi.seoul.go.kr:8088/${encodeURIComponent(sKey)}/json/citydata_ppltn/1/5/${encodeURIComponent("광화문·덕수궁")}`, { muteHttpExceptions: true });
    const parsed = parseSeoulPopResponse_(r1.getContentText("UTF-8"));
    lines.push(parsed ? `✅ 서울 인구 API 정상 (${parsed.AREA_NM}: ${parsed.AREA_CONGEST_LVL}, ${parsed.AREA_PPLTN_MIN}~${parsed.AREA_PPLTN_MAX}명)` : `❌ 서울 API 응답 파싱 실패 (HTTP ${r1.getResponseCode()})`);
  } catch (e) {
    lines.push(`❌ 서울 API 오류: ${e.message}`);
  }
  try {
    const iKey = cfg["ITS_API_KEY"] || "test";
    const r2 = UrlFetchApp.fetch(`https://openapi.its.go.kr:9443/trafficInfo?apiKey=${encodeURIComponent(iKey)}&type=all&drcType=all&minX=127.05&maxX=127.15&minY=37.35&maxY=37.45&getType=json`, { muteHttpExceptions: true });
    const j2 = JSON.parse(r2.getContentText("UTF-8"));
    const cnt = (j2 && j2.body && Array.isArray(j2.body.items)) ? j2.body.items.length : 0;
    lines.push(cnt > 0 ? `✅ 전국 ITS API 정상 (판교 권역 링크 ${cnt}건 수신)` : `⚠️ ITS API 응답 비어있음 (HTTP ${r2.getResponseCode()} · 키 확인 필요)`);
  } catch (e) {
    lines.push(`❌ ITS API 오류: ${e.message}`);
  }
  SpreadsheetApp.getUi().alert(lines.join("\n\n"));
}

// ============================================================================
// 6. 웹 앱 JSON API (실시간 최신 + Google Drive 히스토리 조회)
// ============================================================================
function doGet(e) {
  const params = (e && e.parameter) ? e.parameter : {};
  const cfg = readConfig_();
  const reqToken = String(cfg["허용_토큰"] || "").trim();
  if (reqToken && String(params.token || "").trim() !== reqToken) {
    return jsonOut_({ error: "Unauthorized: 유효한 token 파라미터가 필요합니다." });
  }

  const mode = String(params.mode || "latest").toLowerCase();
  try {
    if (mode === "dates") return jsonOut_(buildDatesPayload_());
    if (mode === "history") {
      const dateStr = String(params.date || "").trim();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
        return jsonOut_({ error: "date 파라미터는 YYYY-MM-DD 형식이어야 합니다." });
      }
      return jsonOut_(buildHistoryPayload_(dateStr));
    }
    return jsonOut_(buildLatestPayload_());
  } catch (err) {
    return jsonOut_({ error: String(err && err.message ? err.message : err) });
  }
}

function buildLatestPayload_() {
  const cache = CacheService.getScriptCache();
  const hit = cache.get("latest_payload");
  if (hit) return JSON.parse(hit);

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SHEET_LATEST);
  const data = [];
  let updatedAt = "";
  if (sh && sh.getLastRow() >= 2) {
    const rows = sh.getRange(2, 1, sh.getLastRow() - 1, 13).getValues();
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (!updatedAt && r[0]) updatedAt = formatCellDate_(r[0]);
      const lon = Number(r[4]), lat = Number(r[5]);
      if (isNaN(lon) || isNaN(lat)) continue;
      data.push({
        ts: formatCellDate_(r[0]),
        source: String(r[1] || ""),
        code: String(r[2] || ""),
        name: String(r[3] || ""),
        lon: lon,
        lat: lat,
        pop_min: Number(r[6]) || 0,
        pop_max: Number(r[7]) || 0,
        pop: Number(r[8]) || 0,
        congest: String(r[9] || ""),
        speed: Number(r[10]) || 0,
        traffic_gbps: Number(r[11]) || 0,
        note: String(r[12] || ""),
      });
    }
  }
  const datesObj = buildDatesPayload_();
  const payload = {
    mode: "latest",
    updated_at: updatedAt,
    count: data.length,
    dates: datesObj.dates.map((d) => d.date),
    data: data,
  };
  try {
    const str = JSON.stringify(payload);
    if (str.length < 95000) cache.put("latest_payload", str, 180);
  } catch (_) {}
  return payload;
}

function buildDatesPayload_() {
  const cache = CacheService.getScriptCache();
  const hit = cache.get("dates_payload");
  if (hit) return JSON.parse(hit);

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheets = ss.getSheets();
  const dateMap = {};

  for (let s = 0; s < sheets.length; s++) {
    const sh = sheets[s];
    const name = sh.getName();
    if (!name.startsWith(HISTORY_PREFIX) || sh.getLastRow() < 2) continue;
    // 날짜(col 2), 시분(col 3)만 빠르게 읽기
    const cols = sh.getRange(2, 2, sh.getLastRow() - 1, 2).getValues();
    for (let i = 0; i < cols.length; i++) {
      const d = formatDateOnly_(cols[i][0]);
      const hm = formatHmOnly_(cols[i][1]);
      if (!d) continue;
      if (!dateMap[d]) dateMap[d] = { date: d, times: {}, rows: 0 };
      dateMap[d].rows++;
      if (hm) dateMap[d].times[hm] = true;
    }
  }

  const dates = Object.keys(dateMap)
    .sort()
    .reverse()
    .map((d) => ({
      date: d,
      snapshots: Object.keys(dateMap[d].times).length,
      rows: dateMap[d].rows,
    }));

  const payload = { mode: "dates", dates: dates };
  try {
    cache.put("dates_payload", JSON.stringify(payload), 180);
  } catch (_) {}
  return payload;
}

function buildHistoryPayload_(dateStr) {
  const ym = dateStr.replace(/-/g, "").slice(0, 6);
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(HISTORY_PREFIX + ym);
  if (!sh || sh.getLastRow() < 2) {
    return { mode: "history", date: dateStr, times: [], snapshots: {} };
  }

  const rows = sh.getRange(2, 1, sh.getLastRow() - 1, 12).getValues();
  const snapshots = {};
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const d = formatDateOnly_(r[1]);
    if (d !== dateStr) continue;
    const hm = formatHmOnly_(r[2]);
    if (!hm) continue;
    if (!snapshots[hm]) snapshots[hm] = [];
    snapshots[hm].push({
      source: String(r[3] || ""),
      code: String(r[4] || ""),
      name: String(r[5] || ""),
      lon: Number(r[6]),
      lat: Number(r[7]),
      pop: Number(r[8]) || 0,
      congest: String(r[9] || ""),
      speed: Number(r[10]) || 0,
      traffic_gbps: Number(r[11]) || 0,
    });
  }

  const times = Object.keys(snapshots).sort();
  return { mode: "history", date: dateStr, count: times.length, times: times, snapshots: snapshots };
}

function formatCellDate_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, TZ, "yyyy-MM-dd HH:mm");
  return String(v || "").trim();
}
function formatDateOnly_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, TZ, "yyyy-MM-dd");
  const s = String(v || "").trim();
  const m = s.match(/(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : "";
}
function formatHmOnly_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, TZ, "HH:mm");
  const s = String(v || "").trim();
  const m = s.match(/(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

function jsonOut_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ============================================================================
// 7. Cloudflare Worker -> Google Sheets 자동 적재 Webhook (doPost)
//    Worker가 5~8분마다 121+44곳을 수집한 뒤 1회 POST로 시트에 영구 누적
// ============================================================================
function doPost(e) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) {
    return jsonOut_({ ok: false, error: "busy" });
  }
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) ? e.postData.contents : "{}");
    const cfg = readConfig_();
    const reqToken = String(cfg["허용_토큰"] || "").trim();
    if (reqToken && String(body.token || "").trim() !== reqToken) {
      return jsonOut_({ ok: false, error: "Unauthorized" });
    }
    const results = Array.isArray(body.data) ? body.data : [];
    if (results.length === 0) return jsonOut_({ ok: false, error: "empty data" });

    const now = new Date();
    const tsFull = String(body.timestamp || Utilities.formatDate(now, TZ, "yyyy-MM-dd HH:mm"));
    const dateStr = String(body.date || Utilities.formatDate(now, TZ, "yyyy-MM-dd"));
    const hmStr = String(body.hm || Utilities.formatDate(now, TZ, "HH:mm"));
    const ymStr = dateStr.replace(/-/g, "").slice(0, 6);

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const latestSheet = ss.getSheetByName(SHEET_LATEST) || ss.insertSheet(SHEET_LATEST);
    const lHeader = [["수집시각", "구분", "코드", "장소명", "경도", "위도", "유동인구_최소", "유동인구_최대", "추정인구", "혼잡도", "평균속도(km/h)", "트래픽환산(Gbps)", "비고"]];
    const lRows = results.map((r) => [
      r.ts || tsFull, r.source || "", r.code || "", r.name || "", Number(r.lon) || 0, Number(r.lat) || 0,
      Number(r.pop_min) || "", Number(r.pop_max) || "", Number(r.pop) || "",
      String(r.congest || ""), Number(r.speed) || "", Number(r.traffic_gbps) || 0, String(r.note || "")
    ]);
    latestSheet.clearContents();
    const outLatest = lHeader.concat(lRows);
    latestSheet.getRange(1, 1, outLatest.length, lHeader[0].length).setValues(outLatest);

    const histName = HISTORY_PREFIX + ymStr;
    let histSheet = ss.getSheetByName(histName);
    if (!histSheet) {
      histSheet = ss.insertSheet(histName);
      const hHeader = [["수집시각", "날짜", "시분", "구분", "코드", "장소명", "경도", "위도", "추정인구", "혼잡도", "평균속도", "트래픽환산(Gbps)"]];
      histSheet.getRange(1, 1, 1, hHeader[0].length).setValues(hHeader);
      histSheet.getRange(1, 1, 1, hHeader[0].length).setFontWeight("bold").setBackground("#334155").setFontColor("#ffffff");
      histSheet.setFrozenRows(1);
    }
    const hRows = results.map((r) => [
      tsFull, dateStr, hmStr, r.source || "", r.code || "", r.name || "", Number(r.lon) || 0, Number(r.lat) || 0,
      Number(r.pop) || 0, String(r.congest || ""), Number(r.speed) || 0, Number(r.traffic_gbps) || 0
    ]);
    histSheet.getRange(histSheet.getLastRow() + 1, 1, hRows.length, hRows[0].length).setValues(hRows);

    const seoulCnt = results.filter((r) => r.source === "seoul_pop").length;
    const itsCnt = results.filter((r) => r.source === "its").length;
    updateConfigValue_("최근_수집시각", tsFull + " (Worker)");
    updateConfigValue_("최근_수집건수", `${results.length} (서울 ${seoulCnt} + ITS ${itsCnt})`);

    const cache = CacheService.getScriptCache();
    cache.remove("latest_payload");
    cache.remove("dates_payload");
    return jsonOut_({ ok: true, saved: results.length, historySheet: histName });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err && err.message ? err.message : err) });
  } finally {
    lock.releaseLock();
  }
}
