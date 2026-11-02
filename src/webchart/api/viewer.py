"""레퍼런스 웹 뷰어 (계획 문서 5장, 3.3): /viewer.

정적 파일 두 개(viewer.html, webchart.js)를 내준다. 데이터 API를 호출할 때 뷰어가
사용자가 입력한 API 키를 붙이므로, 이 경로 자체는 인증하지 않는다.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

router = APIRouter(tags=["viewer"], include_in_schema=False)


@router.get("/viewer")
async def viewer_redirect() -> RedirectResponse:
    # 상대 경로 import("./webchart.js")가 /viewer/ 아래를 가리키도록 끝에 /를 붙인다
    return RedirectResponse("/viewer/")


@router.get("/viewer/")
async def viewer_page() -> FileResponse:
    return FileResponse(WEB_DIR / "viewer.html", media_type="text/html")


@router.get("/viewer/webchart.js")
async def viewer_script() -> FileResponse:
    return FileResponse(WEB_DIR / "webchart.js", media_type="text/javascript")
