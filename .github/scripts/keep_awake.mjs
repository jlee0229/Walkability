// Visits the Streamlit Community Cloud app with a real headless browser so its
// WebSocket handshake fires (a plain HTTP ping never wakes a sleeping app —
// see the "gone to sleep" screen, which is gated behind JS). Clicks the wake
// button if present, then lingers long enough for the visit to register.
import { chromium } from "playwright";

const url = process.env.APP_URL;
if (!url) {
  console.error("APP_URL env var is required");
  process.exit(1);
}

const WAKE_BUTTON_NAME = /get this app back up/i;
const NAV_TIMEOUT_MS = 60_000;
const WAKE_SETTLE_MS = 120_000;
const VISIT_SETTLE_MS = 15_000;

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  console.log(`Visiting ${url}`);
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT_MS });

  const wakeButton = page.getByRole("button", { name: WAKE_BUTTON_NAME });
  const isAsleep = await wakeButton.isVisible({ timeout: 10_000 }).catch(() => false);

  if (isAsleep) {
    console.log("App is asleep — clicking wake button and waiting for it to boot");
    await wakeButton.click();
    await page.waitForTimeout(WAKE_SETTLE_MS);
  } else {
    console.log("No sleep screen detected — app is already awake");
  }

  // Keep the tab open briefly so the WebSocket session counts as a real visit.
  await page.waitForTimeout(VISIT_SETTLE_MS);

  await browser.close();
  console.log("Done.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
