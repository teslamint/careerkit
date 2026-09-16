# Contributing

이 프로젝트에 기여해 주셔서 감사합니다!

> **저장소 경계**: 코드 개발은 공개 `careerkit` 저장소에서 진행하며 이슈와 Pull
> Request 모두 받습니다. 이력서 원본, 채용 데이터, 설계·계획·회고 문서는 별도의
> 비공개 워크스페이스 저장소에 두고 여기에는 커밋하지 않습니다. 라이선스는 MIT입니다.

## 기여 방법

### 버그 리포트

1. [Issues](../../issues)에서 기존 이슈 확인
2. 새 이슈 생성 시 다음 정보 포함:
   - 재현 단계
   - 예상 동작
   - 실제 동작
   - 환경 정보 (OS, Python 버전 등)

### 기능 제안

1. [Issues](../../issues)에 Feature Request 생성
2. 사용 사례와 기대 효과 설명

### Pull Request

1. Fork 후 feature branch 생성
2. 변경사항 커밋 (Conventional Commits 형식)
3. 테스트 실행: `UV_CACHE_DIR=.uv-cache uv run career-resume build example all`
4. Pull Request 생성

## 커밋 메시지

Conventional Commits 형식을 따릅니다:

```
<type>(<scope>): <description>

[optional body]
```

### Types

- `feat`: 새 기능
- `fix`: 버그 수정
- `docs`: 문서 변경
- `style`: 코드 스타일 (포맷팅 등)
- `refactor`: 리팩토링
- `test`: 테스트 추가/수정
- `chore`: 빌드/도구 변경

### 예시

```
feat(builder): add --example flag for demo builds
fix(variant): correct tag parsing for nested blocks
docs(readme): update installation instructions
```

## 코드 스타일

### Python

- PEP 8 준수
- Type hints 사용 권장
- Docstring 포함

### Markdown

- 제목에 `#` 사용
- 리스트 들여쓰기 2칸
- 코드 블록에 언어 명시

## 로컬 개발

```bash
# 예제 데이터로 테스트
UV_CACHE_DIR=.uv-cache uv run career-resume build example all

# 빌드 출력 확인
ls example/build/resume-example*

# 설치형 CLI로 예제 빌드
UV_CACHE_DIR=.uv-cache uv run career-resume build example all
```

## 질문

질문이 있으시면 Issues에 남겨주세요.

## 게시 비식별 가드 (publish de-identification guard)

이 저장소에는 코퍼스 유래 식별 데이터(회사명, 레코드 키, 공고 URL, 개인 이메일 등)가
공개 게시 경로로 흘러들지 못게 하는 결정론적 게이트가 있다. 세 계층(구조 규칙, 형태소
고유명사, 용어 목록)이 모두 차단한다.

### 설치 (1회)

```sh
make install-hooks
```

- `core.hooksPath`를 `.githooks/`로 설정한다. `commit-msg`, `pre-commit`,
  `pre-push`가 이후 모든 커밋·푸시에 실행된다.
- 개발자 셸에 `CAREER_WORKSPACE`가 있으면 훅이 세 계층을 모두 실행한다. 없는 환경
  (CI, throwaway 저장소)에서는 `GUARD_LAYERS=1,2`를 내보낸 개발자나 러너가 계층을
  축소한다. 훅은 절대 레이어 3을 조용히 생략하지 않는다 — 빈 워크스페이스에선
  exit 2로 실패해 원인을 이름으로 말한다.
- `post-merge`, `post-rewrite`, `post-checkout` 훅이 레이어 2의 어휘 캐시를
  차단 경로 밖에서 갱신한다.
- 선택 분석기(`uv sync --extra analyzer`)를 설치하면 형태소 계층도 활성화된다.
  설치하지 않으면 스캔 시 stderr에 "analyzer inactive" 한 줄이 뜨고 나머지 계층이
  종료 코드를 결정한다.

### 훅이 잡는 것 / 못 잡는 것

- 잡는 것: 커밋 메시지, 스테이지된 추가 줄과 **추가 경로**, cherry-pick·am·rebase·
  태그 본문(pre-push).
- 못 잡는 것(git이 못 보는 것): **PR·이슈 본문, 리뷰 댓글, 릴리스 노트**. 이 항목은
  사람이 게시 전에 검사해야 한다. 절차:

```sh
# 본문을 파일로 저장한 뒤 게이트로 스캔하고 붙여넣는다
gh issue view 123 --json body -q .body > /tmp/body.md
uv run python -m careerkit.publish_guard --message /tmp/body.md
```

### 분석기 비활성 라인

기본 설치에는 분석기가 없다. 클린 스캔이 exit 0이면서 stderr에 정확히 한 줄의
"inactive" 메시지를 내는 것이 정상이다. `--layers 1,2`는 CI 전용: 개발자 셸에서
이 플래그 없이 `CAREER_WORKSPACE`가 빠지면 레이어 3이 조용히 꺼지지 않고 exit 2로
실패한다.
