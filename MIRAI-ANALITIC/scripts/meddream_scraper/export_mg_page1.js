require('dotenv').config();
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const MEDDREAM_URL = process.env.MEDDREAM_URL || 'http://10.10.3.188/';
const MEDDREAM_USER = process.env.MEDDREAM_USER;
const MEDDREAM_PASSWORD = process.env.MEDDREAM_PASSWORD;

if (!MEDDREAM_USER || !MEDDREAM_PASSWORD) {
  console.error('Missing MEDDREAM_USER / MEDDREAM_PASSWORD in .env (see .env.example).');
  process.exit(1);
}

async function setModalityToMGOnly(page) {
  const caret = page.locator('[data-testid="modalities-buttons-group"] button').last();
  await caret.click();
  await page.waitForTimeout(500);

  const panel = page.locator('.included-modalities.button-group');
  const count = await panel.locator('button').count();
  for (let i = 0; i < count; i++) {
    const btn = panel.locator('button').nth(i);
    const text = (await btn.textContent()).trim();
    const cls = (await btn.getAttribute('class')) || '';
    const isActive = cls.includes('active');
    if (text === 'MG' && !isActive) {
      await btn.click();
      await page.waitForTimeout(200);
    } else if (text !== 'MG' && isActive) {
      await btn.click();
      await page.waitForTimeout(200);
    }
  }

  await caret.click(); // close the dropdown
  await page.waitForTimeout(500);
}

async function selectAllRowsOnPage(page) {
  const checkboxes = page.locator('.list-row:not(.header) input[type="checkbox"]');
  const count = await checkboxes.count();
  for (let i = 0; i < count; i++) {
    const cb = checkboxes.nth(i);
    if (!(await cb.isChecked())) {
      await cb.check();
    }
  }
  return count;
}

async function run() {
  const headless = process.env.HEADLESS !== 'false';
  const browser = await chromium.launch({ channel: 'chrome', headless });
  const page = await browser.newPage();

  try {
  await page.goto(MEDDREAM_URL, { waitUntil: 'networkidle' });
  await page.fill('#username', MEDDREAM_USER);
  await page.fill('#password', MEDDREAM_PASSWORD);
  await page.click('button.button-control-primary');
  await page.waitForSelector('.list-row:not(.header)', { timeout: 20000 });
  await page.waitForTimeout(1000);

  console.log('Setting modality filter to MG only...');
  await setModalityToMGOnly(page);
  await page.waitForSelector('.list-row:not(.header)', { timeout: 20000 });
  await page.waitForTimeout(1000);

  const selectedCount = await selectAllRowsOnPage(page);
  console.log(`Selected ${selectedCount} MG studies on page 1.`);

  await page.locator('button[title="Export"]').first().click();
  await page.waitForTimeout(800);
  await page.click('#burn');

  console.log('Export job started on the MedDream server. Waiting for the ZIP to be ready (can take several minutes for 20 real studies)...');
  const downloadBtn = page.locator('button:has-text("Download")').first();

  const outDir = path.join(__dirname, 'output');
  fs.mkdirSync(outDir, { recursive: true });

  const maxWaitMs = 15 * 60 * 1000; // 15 minutes
  const pollMs = 10000;
  let waited = 0;
  let ready = await downloadBtn.isVisible().catch(() => false);
  while (!ready && waited < maxWaitMs) {
    await page.waitForTimeout(pollMs);
    waited += pollMs;
    ready = await downloadBtn.isVisible().catch(() => false);
    console.log(`  ...still waiting (${Math.round(waited / 1000)}s elapsed, ready=${ready})`);
  }

  if (!ready) {
    await page.screenshot({ path: path.join(outDir, 'export_timeout_debug.png') });
    throw new Error(`Download button never appeared after ${maxWaitMs / 1000}s. Screenshot saved to output/export_timeout_debug.png`);
  }

  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 120000 }),
    downloadBtn.click(),
  ]);
  const savePath = path.join(outDir, `meddream_mg_page1_${Date.now()}.zip`);
  await download.saveAs(savePath);

  console.log(`Saved ZIP: ${savePath} (${fs.statSync(savePath).size} bytes)`);

  if (!headless) {
    await page.waitForTimeout(3000);
  }
  return savePath;
  } finally {
    await browser.close();
  }
}

run().catch(err => {
  console.error('Export failed:', err.message);
  process.exitCode = 1;
});
