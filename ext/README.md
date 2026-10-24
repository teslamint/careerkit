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

- **수집**: 채용 사이트(Wanted, Remember, GroupBy, Saramin, 직행) 공고 페이지 방문 → 우하단 "수집" 버튼 클릭
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
| 직행 | zighang.com/recruitment/* (공고 UUID) |

직행은 robots.txt로 자동 접근을 막는다. 그래서 careerkit은 직행 서버에 요청을 보내지 않는다.
배지의 "수집"은 사용자가 연 공고 페이지의 스크립트에서 공고 데이터를 읽어 native host로 보낸다.

- 사이트 안에서 다른 공고로 이동한 뒤 "새로고침 후 다시 시도"가 뜨면 페이지를 새로고침한다.
- 원본 공고가 지원 플랫폼(Wanted, Remember, GroupBy, Saramin)에 있으면 원본 URL로 수집한다.
  기록은 원본 URL에 남으므로 직행 페이지를 다시 열면 배지는 "수집" 버튼을 보인다. 누르면 원본 기록의 판정을 보여 준다.
- 사이드 패널의 "수집"은 페이지를 읽을 수 없으므로 직행 공고에는 배지의 "수집"을 쓴다. 이미 있는 기록의 재스크리닝은 사이드 패널에서 된다.
- 확장의 직행 호스트 권한은 사이드 패널이 탭 URL을 읽기 위한 것이다. 호스트 권한만으로는 요청이 나가지 않는다. 확장 코드에는 `fetch` 호출이 없고 native host는 직행에 요청하지 않는다.
- 본문이 이미지뿐인 공고는 직행의 요약 텍스트로 수집하고 출처를 "직행 (사이트 요약)"으로 표시한다.

자세한 결정 배경은 `docs/adr/0001-zighang-extension-only-collection.md`에 있다.
