"""도메인 계층: I/O 없는 순수 계산.

이 패키지는 fastapi, redis, matplotlib, dask를 import하지 않는다.
의존 방향은 api → services → domain 한쪽으로만 흐른다 (docs/improvement-plan.md 3.1).
"""
