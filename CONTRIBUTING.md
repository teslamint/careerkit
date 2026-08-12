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
