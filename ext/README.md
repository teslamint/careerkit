# CareerKit Browser Extension

채용 사이트에서 공고를 수집하고 스크리닝 결과를 인라인으로 표시하는 Chrome 확장 프로그램.

## 설치

### 1. Native Messaging Host 등록

```bash
cd ext/native-host
uv run python install.py
```

### 2. Chrome에 확장 로드

1. Chrome에서 `chrome://extensions` 접속
2. "개발자 모드" 활성화 (우상단 토글)
3. "압축해제된 확장 프로그램을 로드합니다" 클릭
4. `ext/` 디렉터리 선택

### 3. 확장 ID 설정

1. 로드 후 확장의 ID를 복사 (chrome://extensions에서 확인)
2. `ext/native-host/.generated/com.careerkit.host.json`을 열어 `allowed_origins`의 `EXTENSION_ID_HERE`를 실제 ID로 교체
3. install.py를 다시 실행하거나 파일을 직접 수정

## 사용법

- **수집**: 채용 사이트(Wanted, Remember, GroupBy, Saramin) 공고 페이지 방문 → 우하단 "수집" 버튼 클릭
- **결과 확인**: 판정 배지(🟢추천/🟡보류/🔴비추천) 자동 표시
- **상세 보기**: 배지 클릭 또는 확장 아이콘 클릭 → 사이드바에서 스크리닝 상세 확인

## 트러블슈팅

### Native host 연결 불가
- `uv run python ext/native-host/install.py`를 재실행
- Chrome을 재시작
- `chrome://extensions`에서 확장의 "서비스 워커" 링크를 클릭해 콘솔 로그 확인

### 수동 삭제
```bash
rm ~/Library/Application\ Support/Google/Chrome/NativeMessagingHosts/com.careerkit.host.json
rm -rf ext/native-host/.generated/
```

## 지원 플랫폼 (v1)

| 플랫폼 | URL 패턴 |
|--------|---------|
| Wanted | wanted.co.kr/wd/* |
| Remember | rememberapp.co.kr/job/* |
| GroupBy | groupby.kr/positions/* |
| Saramin | saramin.co.kr (rec_idx=*) |
