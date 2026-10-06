"""Headless Chromium lifecycle (Playwright): one browser per process, one context per job."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from app.converter.errors import ServiceUnavailableError

logger = logging.getLogger("prelo.converter")

LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",
    "--font-render-hinting=none",
    # Every legitimate request is answered by Playwright request interception, so the
    # browser itself never needs the network: send anything else (WebSockets,
    # preconnects, WebRTC) to a closed local port, loopback included.
    "--proxy-server=127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
_CLOSE_TIMEOUT = 10.0
# The "disconnected" event of a dying browser can arrive just after the error it caused.
_DISCONNECT_GRACE = 0.25


class BrowserCrashedError(Exception):
    """The browser died while a job was using it (the job may be retried)."""


class RendererCrashedError(Exception):
    """A page's renderer process died (e.g. out of memory) while the browser stayed up."""


class BrowserManager:
    """Owns the Playwright driver and the Chromium instance.

    The browser is launched by :meth:`start` and transparently relaunched (under a
    lock) when it is found disconnected. Jobs get an isolated context from
    :meth:`context`, which is always closed afterwards.

    ``sandbox`` is ``"auto"`` (use Chromium's sandbox when the container allows it,
    otherwise run without it and log a warning), ``"true"`` (required) or ``"false"``.
    """

    def __init__(self, launch_args: tuple[str, ...] = LAUNCH_ARGS, sandbox: str = "auto") -> None:
        self._launch_args = launch_args
        self._sandbox = sandbox
        self._sandboxed = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._version: str | None = None

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def sandboxed(self) -> bool:
        """Whether the running browser uses Chromium's sandbox."""
        return self._sandboxed

    def is_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> None:
        await self.ensure()

    async def stop(self) -> None:
        async with self._lock:
            await self._close_browser()
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    async def ensure(self) -> Browser:
        """Return a connected browser, (re)launching it when needed."""
        browser = self._browser
        if browser is not None and browser.is_connected():
            return browser
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self._close_browser()
            try:
                self._browser = await self._launch()
            except Exception as first_error:
                # The driver itself may be broken: restart it once and retry.
                logger.warning("Chromium launch failed (%s); restarting the driver", first_error)
                await self._restart_driver()
                try:
                    self._browser = await self._launch()
                except Exception as exc:
                    logger.error("Chromium could not be launched: %s", exc)
                    raise ServiceUnavailableError() from exc
            self._version = self._browser.version
            logger.info(
                "Chromium %s started (sandbox %s)",
                self._version,
                "on" if self._sandboxed else "off",
            )
            return self._browser

    @contextlib.asynccontextmanager
    async def context(self, **options: Any) -> AsyncIterator[BrowserContext]:
        """A fresh, isolated browser context, closed on exit (also on cancellation)."""
        browser = await self.ensure()
        try:
            context = await browser.new_context(**options)
        except PlaywrightError as exc:
            await self.raise_if_crashed(exc)
            raise
        try:
            yield context
        finally:
            await close_quietly(context)

    async def raise_if_crashed(self, error: Exception) -> None:
        """Turn a Playwright error caused by a dead browser into :class:`BrowserCrashedError`."""
        if self.is_connected() and "closed" in str(error).lower():
            await asyncio.sleep(_DISCONNECT_GRACE)
        if not self.is_connected():
            raise BrowserCrashedError(str(error)) from error

    async def _launch(self) -> Browser:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        args = list(self._launch_args)
        if self._sandbox != "false":
            try:
                browser = await self._playwright.chromium.launch(args=args, chromium_sandbox=True)
            except Exception as exc:
                if self._sandbox == "true":
                    raise
                logger.warning(
                    "Chromium sandbox unavailable (%s); running without it. Allow user "
                    "namespaces (see the seccomp profile in the repository) or set "
                    "PRELO_CHROMIUM_SANDBOX=false to silence this warning",
                    str(exc).strip().splitlines()[0][:200] if str(exc).strip() else exc,
                )
                self._sandbox = "false"  # do not retry on every relaunch
            else:
                self._sandboxed = True
                return browser
        self._sandboxed = False
        return await self._playwright.chromium.launch(args=args, chromium_sandbox=False)

    async def _restart_driver(self) -> None:
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._playwright = None

    async def _close_browser(self) -> None:
        if self._browser is not None:
            await close_quietly(self._browser)
            self._browser = None


async def close_quietly(target: Browser | BrowserContext) -> None:
    """Close a browser or context, ignoring errors and bounding the wait."""
    try:
        async with asyncio.timeout(_CLOSE_TIMEOUT):
            await target.close()
    except (PlaywrightError, TimeoutError):
        pass
    except Exception:  # pragma: no cover - defensive: never mask the original error
        logger.debug("Ignoring error while closing a browser resource", exc_info=True)
