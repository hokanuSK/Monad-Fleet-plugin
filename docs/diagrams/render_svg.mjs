import puppeteer from 'puppeteer';
import fs from 'fs';

const svgPath = process.argv[2];
const outPath = process.argv[3];
const scale = parseFloat(process.argv[4] || '3');

const svg = fs.readFileSync(svgPath, 'utf8');
const browser = await puppeteer.launch({
  headless: 'shell',
  executablePath: '/Users/admin/.cache/puppeteer/chrome-headless-shell/mac_arm-131.0.6778.204/chrome-headless-shell-mac-arm64/chrome-headless-shell',
});
const page = await browser.newPage();
await page.setContent(`<!doctype html><html><head><meta charset="utf-8">
<style>*{margin:0;padding:0}body{background:#fff}</style></head>
<body>${svg}</body></html>`, {waitUntil:'networkidle0'});
const el = await page.$('svg');
const box = await el.boundingBox();
await page.setViewport({width: Math.ceil(box.width), height: Math.ceil(box.height), deviceScaleFactor: scale});
await el.screenshot({path: outPath, omitBackground: false});
await browser.close();
console.log('OK', outPath, box.width+'x'+box.height, 'scale', scale);
