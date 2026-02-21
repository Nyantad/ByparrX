import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, NamedTuple, cast

from camoufox import AsyncCamoufox
from fastapi import Header
from playwright.async_api import Browser, BrowserContext, Page
from playwright_captcha import (
    ClickSolver,
    FrameworkType,
)
from pydantic import BaseModel, Field

from src.consts import (
    ADDON_PATH,
    LOG_LEVEL,
    MAX_ATTEMPTS,
    PROXY_PASSWORD,
    PROXY_SERVER,
    PROXY_USERNAME,
)

solver_logger = logging.getLogger("playwright_captcha")
solver_logger.handlers.clear()
if LOG_LEVEL == logging.DEBUG:
    solver_logger.addHandler(logging.StreamHandler())
    solver_logger.setLevel(LOG_LEVEL)
else:
    solver_logger.handlers.append(logging.NullHandler())

logger = logging.getLogger("uvicorn.error")
logger.setLevel(LOG_LEVEL)
if len(logger.handlers) == 0:
    logger.addHandler(logging.StreamHandler())


class TimeoutTimer(BaseModel):
    duration: int  # in seconds
    start_time: float = Field(default_factory=time.perf_counter)

    def remaining(self) -> float:
        """Get remaining time in seconds."""
        return max(0, self.duration - (time.perf_counter() - self.start_time))


class CamoufoxDepClass(NamedTuple):
    page: Page
    solver: ClickSolver
    context: BrowserContext


class BrowserManager:
    """Keeps a persistent browser context alive so cookies (e.g. cf_clearance) survive across requests."""

    def __init__(self) -> None:
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._camoufox_cm: AsyncCamoufox | None = None
        self._init_lock = asyncio.Lock()
        self._proxy_config: dict | None = None
        self._page_semaphore = asyncio.Semaphore(4)  # max 4 concurrent pages
        self._user_agent: str | None = None  # cached after first page

    async def _start(self, proxy_config: dict | None = None) -> None:
        self._proxy_config = proxy_config
        self._camoufox_cm = AsyncCamoufox(
            main_world_eval=True,
            addons=[ADDON_PATH],
            geoip=True,
            proxy=proxy_config,
            locale="en-US",
            headless=True,
            humanize=True,
            i_know_what_im_doing=True,
            config={"forceScopeAccess": True},
            disable_coop=True,
        )
        browser_raw = await self._camoufox_cm.__aenter__()
        self._browser = cast("Browser", browser_raw)
        self._context = await self._browser.new_context()
        self._user_agent = None  # reset on new browser

    async def _ensure_browser(self, proxy_config: dict | None = None) -> None:
        # Restart if proxy config changed
        if self._browser is not None and self._proxy_config != proxy_config:
            logger.debug("Proxy config changed, restarting browser")
            await self.shutdown()

        if self._browser is not None:
            # Verify the browser is still alive
            try:
                self._browser.contexts  # noqa: B018
            except Exception:
                logger.warning("Browser context lost, restarting")
                await self.shutdown()

        if self._browser is None:
            await self._start(proxy_config)

    async def warmup(self, proxy_config: dict | None = None) -> None:
        """Pre-launch browser during app startup so first request is fast."""
        async with self._init_lock:
            await self._ensure_browser(proxy_config)
        # Capture user agent early
        if self._user_agent is None and self._context is not None:
            page = await self._context.new_page()
            try:
                self._user_agent = await page.evaluate("navigator.userAgent")
                logger.info("Browser warmed up, UA: %s", self._user_agent[:60])
            finally:
                await page.close()

    @property
    def cached_user_agent(self) -> str | None:
        return self._user_agent

    async def get_cookies(self) -> list:
        """Return cookies from the persistent context without creating a page."""
        if self._context is None:
            return []
        return await self._context.cookies()

    @asynccontextmanager
    async def get_page(self, proxy_config: dict | None = None):
        """Yield a new page in the persistent context. The page is closed after use."""
        async with self._init_lock:
            await self._ensure_browser(proxy_config)

        assert self._context is not None  # noqa: S101
        async with self._page_semaphore:
            page = await self._context.new_page()
            try:
                yield page, self._context
            finally:
                # Cache user agent from first page if not yet cached
                if self._user_agent is None:
                    try:
                        self._user_agent = await page.evaluate("navigator.userAgent")
                    except Exception:
                        pass
                try:
                    await page.close()
                except Exception:
                    pass

    async def shutdown(self) -> None:
        """Close the browser and clean up."""
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._camoufox_cm:
                await self._camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._camoufox_cm = None


browser_manager = BrowserManager()


async def get_camoufox(
    x_proxy_server: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Server",
            description="Override proxy server for this request in protocol://host:port format.",
        ),
    ] = None,
    x_proxy_username: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Username",
        ),
    ] = None,
    x_proxy_password: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Password",
        ),
    ] = None,
) -> AsyncGenerator[CamoufoxDepClass]:
    """Get Camoufox instance with a persistent browser context for cookie reuse."""
    proxy_config = None

    if x_proxy_server:
        proxy_config = {
            "server": x_proxy_server,
            "username": x_proxy_username,
            "password": x_proxy_password,
        }
    elif PROXY_SERVER:
        proxy_config = {
            "server": PROXY_SERVER,
            "username": PROXY_USERNAME,
            "password": PROXY_PASSWORD,
        }

    async with browser_manager.get_page(proxy_config) as (page, context):
        async with ClickSolver(
            framework=FrameworkType.CAMOUFOX,
            page=page,
            max_attempts=MAX_ATTEMPTS,
            attempt_delay=1,
        ) as solver:
            yield CamoufoxDepClass(page, solver, context)
