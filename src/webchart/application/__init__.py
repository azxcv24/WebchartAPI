"""유스케이스 계층: 요청 하나를 plan → fetch → project → paint → encode 로 처리한다.

domain(순수 로직)과 infrastructure(저장소·그리기)를 조합하며, HTTP/WS 같은 전송 방식은
모른다 (계획 문서 3.1, 3.2).
"""
