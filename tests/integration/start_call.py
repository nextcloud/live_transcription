#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Log in with a headless Chromium and start the call of a conversation in the Talk web UI.

The browser stays in the call until SIGINT/SIGTERM, then leaves it. "READY" is
printed once the call runs. A screenshot is written on failure and on exit.

Usage: start_call.py --url URL --token TOKEN --user U --password P [--screenshot FILE]
"""

import argparse
import asyncio
import logging
import signal
import sys

from playwright.async_api import async_playwright

LOGGER = logging.getLogger("start_call")
CALL_BUTTON = 'button[aria-label="Start call"], button[aria-label="Join call"]'
LEAVE_BUTTON = 'button[aria-label="Leave call"]'
TIMEOUT_MS = 90_000


async def start_call(page, url: str, token: str, user: str, password: str):
	await page.goto(f"{url}/index.php/login")
	await page.fill("#user", user)
	await page.fill("#password", password)
	await page.click("button[type=submit]")
	await page.wait_for_url("**/apps/**", timeout=TIMEOUT_MS)
	LOGGER.info("%s logged in", user)

	await page.goto(f"{url}/index.php/call/{token}")
	await page.locator(CALL_BUTTON).first.click(timeout=TIMEOUT_MS)
	LOGGER.info("call button clicked")

	# Talk asks to check the devices before the first call; its dialog has its own call button.
	dialog = page.get_by_role("dialog").filter(has_text="Check devices")
	leave = page.locator(LEAVE_BUTTON).first
	await dialog.or_(leave).first.wait_for(timeout=TIMEOUT_MS)
	if await dialog.is_visible():
		await dialog.locator(CALL_BUTTON).last.click()
		LOGGER.info("device check confirmed")
	await leave.wait_for(timeout=TIMEOUT_MS)
	LOGGER.info("%s is in the call", user)


async def run(args) -> int:
	stop = asyncio.Event()
	for sig in (signal.SIGINT, signal.SIGTERM):
		asyncio.get_running_loop().add_signal_handler(sig, stop.set)

	async with async_playwright() as playwright:
		browser = await playwright.chromium.launch(headless=True, args=[
			# grant the microphone and camera prompt, and feed synthetic devices
			"--use-fake-ui-for-media-stream",
			"--use-fake-device-for-media-stream",
			"--autoplay-policy=no-user-gesture-required",
		])
		# Talk flags "HeadlessChrome" as an unsupported browser and its warning covers the call button
		user_agent = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
			f"Chrome/{browser.version} Safari/537.36")
		page = await browser.new_page(viewport={"width": 1280, "height": 900}, user_agent=user_agent)
		page.on("console", lambda msg: msg.type == "error" and LOGGER.warning("browser console: %s", msg.text))
		try:
			await start_call(page, args.url, args.token, args.user, args.password)
		except Exception:
			LOGGER.exception("could not start the call")
			await page.screenshot(path=args.screenshot, full_page=True)
			return 1

		print("READY", flush=True)
		await stop.wait()
		await page.screenshot(path=args.screenshot, full_page=True)
		await page.locator(LEAVE_BUTTON).first.click(timeout=10_000)
		await page.wait_for_timeout(2000)
		LOGGER.info("%s left the call", args.user)
		await browser.close()
	return 0


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("--url", required=True, help="Nextcloud base URL")
	parser.add_argument("--token", required=True, help="conversation token")
	parser.add_argument("--user", required=True)
	parser.add_argument("--password", required=True)
	parser.add_argument("--screenshot", default="start_call.png")
	args = parser.parse_args()
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
		datefmt="%H:%M:%S")
	return asyncio.run(run(args))


if __name__ == "__main__":
	sys.exit(main())
