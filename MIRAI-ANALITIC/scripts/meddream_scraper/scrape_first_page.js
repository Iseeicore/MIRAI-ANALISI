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

async function scrapeFirstPage() {
  const headless = process.env.HEADLESS !== 'false';
  const browser = await chromium.launch({ channel: 'chrome', headless });
  const page = await browser.newPage();

  await page.goto(MEDDREAM_URL, { waitUntil: 'networkidle' });
  await page.fill('#username', MEDDREAM_USER);
  await page.fill('#password', MEDDREAM_PASSWORD);
  await page.click('button.button-control-primary');

  await page.waitForSelector('.list-row:not(.header)', { timeout: 20000 });
  await page.waitForTimeout(1000);

  // MedDream renders the studies list as divs (.list-row), not an HTML table.
  // Field order matches the header's data-testid="header-item-<field>" order.
  const FIELDS = ['id', 'name', 'accessionNumber', 'modality', 'description', 'createDatetime', 'receiveDatetime', 'sourceae'];

  const records = await page.$$eval('.list-row:not(.header)', (rows, fields) => {
    return rows.map(row => {
      const checkbox = row.querySelector('input[type="checkbox"]');
      const items = Array.from(row.querySelectorAll(':scope > .item'));
      // items[0] = checkbox, items[1] = eye/action icon, items[2..] = data fields
      const dataItems = items.slice(2);
      const record = { studyInstanceUID: checkbox ? checkbox.value : null };
      fields.forEach((field, i) => {
        record[field] = dataItems[i] ? dataItems[i].innerText.trim() : '';
      });
      return record;
    });
  }, FIELDS);

  const outDir = path.join(__dirname, 'output');
  fs.mkdirSync(outDir, { recursive: true });
  const outFile = path.join(outDir, `meddream_page1_${Date.now()}.json`);
  fs.writeFileSync(outFile, JSON.stringify(records, null, 2));

  console.log(`Scraped ${records.length} records from page 1.`);
  console.log(`Saved to: ${outFile}`);

  if (!headless) {
    await page.waitForTimeout(5000);
  }
  await browser.close();
  return records;
}

scrapeFirstPage().catch(err => {
  console.error('Scrape failed:', err.message);
  process.exit(1);
});
