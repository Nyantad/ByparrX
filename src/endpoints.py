import base64
import time
import warnings
from asyncio import wait_for
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from playwright_captcha import CaptchaType

from src.consts import CHALLENGE_TITLES
from src.models import (
    HealthcheckResponse,
    LinkRequest,
    LinkResponse,
    Solution,
)
from src.utils import CamoufoxDepClass, TimeoutTimer, browser_manager, get_camoufox, logger

warnings.filterwarnings("ignore", category=SyntaxWarning)


router = APIRouter()

CamoufoxDep = Annotated[CamoufoxDepClass, Depends(get_camoufox)]


@router.get("/", include_in_schema=False)
def read_root():
    """Redirect to /docs."""
    logger.debug("Redirecting to /docs")
    return RedirectResponse(url="/docs", status_code=301)


@router.get("/health")
async def health_check(sb: CamoufoxDep):
    """Health check endpoint."""
    health_check_request = await read_item(
        LinkRequest.model_construct(url="https://google.com"),
        sb,
    )

    if health_check_request.solution.status != HTTPStatus.OK:
        raise HTTPException(
            status_code=500,
            detail="Health check failed",
        )

    return HealthcheckResponse(user_agent=health_check_request.solution.user_agent)


@router.post("/v1")
async def read_item(request: LinkRequest, dep: CamoufoxDep) -> LinkResponse:
    """Handle POST requests."""
    start_time = int(time.time() * 1000)

    # Fast path: returnOnlyCookies — use cached cookies + user agent, skip navigation
    if request.return_only_cookies:
        existing_cookies = await browser_manager.get_cookies()
        if existing_cookies:
            ua = browser_manager.cached_user_agent or await dep.page.evaluate("navigator.userAgent")
            logger.debug("returnOnlyCookies: returning %d cached cookies", len(existing_cookies))
            return LinkResponse(
                message="Challenge not detected!",
                solution=Solution(
                    user_agent=ua,
                    url=request.url,
                    status=HTTPStatus.OK,
                    cookies=existing_cookies,
                    headers={},
                    response="",
                ),
                start_timestamp=start_time,
            )

    timer = TimeoutTimer(duration=request.max_timeout)

    request.url = request.url.replace('"', "").strip()
    challenge_detected = False
    page_request = None
    is_html = True

    # Navigate with one retry on transient failure
    for attempt in range(2):
        try:
            page_request = await dep.page.goto(
                request.url, timeout=timer.remaining() * 1000
            )
            break
        except TimeoutError:
            if attempt == 0 and timer.remaining() > 5:
                logger.warning("Navigation timed out, retrying once...")
                continue
            raise
        except Exception as e:
            if attempt == 0 and timer.remaining() > 5:
                logger.warning("Navigation failed (%s), retrying once...", e)
                continue
            raise

    status = page_request.status if page_request else HTTPStatus.OK

    # Check content type — skip DOM/network waits for non-HTML responses (images, etc.)
    content_type = (
        page_request.headers.get("content-type", "") if page_request else ""
    )
    is_html = "text/html" in content_type or not content_type

    try:
        if is_html:
            await dep.page.wait_for_load_state(
                state="domcontentloaded", timeout=timer.remaining() * 1000
            )
            try:
                await dep.page.wait_for_load_state(
                    "networkidle", timeout=min(timer.remaining(), 10) * 1000
                )
            except TimeoutError:
                pass  # networkidle is best-effort, don't fail on it

            challenge_detected = await dep.page.title() in CHALLENGE_TITLES
            if challenge_detected:
                logger.info("Challenge detected, attempting to solve...")
                # Solve the captcha
                await wait_for(
                    dep.solver.solve_captcha(  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                        captcha_container=dep.page,
                        captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                        wait_checkbox_attempts=1,
                        wait_checkbox_delay=0.5,
                    ),
                    timeout=timer.remaining(),
                )
                status = HTTPStatus.OK
                logger.debug("Challenge solved successfully.")
    except TimeoutError as e:
        logger.error("Timed out while solving the challenge")
        raise HTTPException(
            status_code=408,
            detail="Timed out while solving the challenge",
        ) from e

    cookies = await dep.context.cookies()

    message = "Challenge solved!" if challenge_detected else "Challenge not detected!"

    # Use cached user agent when available (avoids JS eval on every request)
    ua = browser_manager.cached_user_agent or await dep.page.evaluate("navigator.userAgent")

    # For binary (non-HTML) responses, capture raw bytes and base64-encode them
    # so the caller can reconstruct the binary (e.g. image) on its side.
    if not is_html and page_request:
        try:
            body = await page_request.body()
            response_content = base64.b64encode(body).decode("ascii")
            logger.debug("Captured binary response (%d bytes, base64-encoded)", len(body))
        except Exception:
            response_content = await dep.page.content()
    else:
        response_content = await dep.page.content()

    return LinkResponse(
        message=message,
        solution=Solution(
            user_agent=ua,
            url=dep.page.url,
            status=status,
            cookies=cookies,
            headers=page_request.headers if page_request else {},
            response=response_content,
        ),
        start_timestamp=start_time,
    )
