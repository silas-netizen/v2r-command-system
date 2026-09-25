# /autocompact 자동 압축 설정법

`/autocompact` 뒤에는 문장이 아니라 **auto** 또는 **토큰 수**만 넣을 수 있습니다.

| 입력 | 뜻 |
|---|---|
| `/autocompact auto` | 기본값. 문맥이 한도에 가까워지면 자동 압축 |
| `/autocompact 200k` | 20만 토큰에 도달하면 자동 압축 (100k~1M 사이) |
| `/autocompact 500000` | 숫자로 직접 지정도 가능 |

지금 세션은 이미 자동 압축이 켜져 있습니다(방금 자동 요약이 한 번 일어났습니다). 더 일찍 압축되게 하려면 `/autocompact 200k` 처럼 낮은 값을 넣으면 됩니다.

관련 규칙 파일: [feedback-reply-length.md](file:///C:/Users/user/.claude/projects/D--v2r----/memory/feedback-reply-length.md)
