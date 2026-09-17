#!/usr/bin/env node
// Headless-browser UI checks: drives the real page in Chromium over the DevTools
// protocol (no npm dependencies; needs Node 22+ and chromium on PATH).
//
//   node app/tests/ui_check.mjs            # all checks
//   node app/tests/ui_check.mjs -k bitrate # checks whose name contains "bitrate"
//   node app/tests/ui_check.mjs --shot     # also save a screenshot per check

import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER = join(HERE, '..', 'server.py');
const args = process.argv.slice(2);
const filters = args.flatMap((a, i) => (a === '-k' ? [args[i + 1]] : []));
const shots = args.includes('--shot');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Helpers injected into the page before each check.
const PAGE_HELPERS = `
  window.t = {
    $: (id) => document.getElementById(id),
    mode(m) { const r = document.querySelector('input[name=mode][value="' + m + '"]'); r.checked = true; r.dispatchEvent(new Event('change', { bubbles: true })); },
    set(id, v) { const e = document.getElementById(id); if (e.type === 'checkbox') e.checked = v; else e.value = v; e.dispatchEvent(new Event('change', { bubbles: true })); },
    opts(id) { return [...document.getElementById(id).options].map((o) => o.value); },
    async until(fn, ms = 8000) { const end = Date.now() + ms; while (Date.now() < end) { try { const v = await fn(); if (v) return v; } catch (_) {} await new Promise((r) => setTimeout(r, 100)); } throw new Error('timed out waiting: ' + fn); },
    async post(body) { const r = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); return r.json(); },
    eq(a, b, what) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(what + ': ' + JSON.stringify(a) + ' !== ' + JSON.stringify(b)); },
    ok(v, what) { if (!v) throw new Error(what); },
  };
`;

// Each check is an async function body run in the page. Throw to fail.
const CHECKS = [
  ['baseline: config loaded', `
    t.ok(t.$('outdir').value, 'default outdir filled');
    t.ok(document.querySelectorAll('#chips .chip').length >= 2, 'chips rendered');
  `],
  ['bitrate: formats come from config', `
    const cfg = await (await fetch('/api/config')).json();
    t.eq(t.opts('audio-format'), cfg.audio_formats.map((f) => f.key), 'format options');
    t.eq(t.opts('audio-quality'), ['best', '320', '192', '128'], 'bitrate options');
  `],
  ['bitrate: enable/disable by mode and format', `
    t.mode('merged');   t.ok(t.$('audio-format').disabled && t.$('audio-quality').disabled, 'merged disables audio');
    t.mode('video');    t.ok(t.$('audio-format').disabled, 'video disables audio');
    t.mode('audio');    t.set('audio-format', 'mp3');
    t.ok(!t.$('audio-quality').disabled, 'mp3 enables bitrate');
    t.eq(t.$('audio-quality').options[0].textContent, 'Best (VBR ~245 kbps)', 'best label');
    t.set('audio-format', 'wav');    t.ok(t.$('audio-quality').disabled, 'wav disables bitrate');
    t.set('audio-format', 'native'); t.ok(t.$('audio-quality').disabled, 'original disables bitrate');
    t.mode('separate'); t.set('audio-format', 'mp3'); t.ok(!t.$('audio-quality').disabled, 'separate+mp3 enables bitrate');
  `],
  ['bitrate: job card label and "Use these settings"', `
    const job = await t.post({ url: 'https://example.invalid/x', mode: 'audio', audio_format: 'mp3', audio_quality: '192', outdir: t.$('outdir').value });
    const card = await t.until(() => document.querySelector('.job[data-id="' + job.id + '"]'));
    t.ok(card.textContent.includes('MP3 192k'), 'card shows MP3 192k');
    t.mode('merged'); t.set('audio-format', 'native'); t.set('audio-quality', 'best');
    [...card.querySelectorAll('button')].find((b) => b.textContent === 'Use these settings').click();
    t.eq([document.querySelector('input[name=mode]:checked').value, t.$('audio-format').value, t.$('audio-quality').value],
         ['audio', 'mp3', '192'], 'restored settings');
    t.ok(!t.$('audio-quality').disabled, 'bitrate enabled after restore');
  `],
  ['formats: six options, lossless disables bitrate, best label follows format', `
    t.eq(t.opts('audio-format'), ['native', 'mp3', 'm4a', 'opus', 'flac', 'wav'], 'format list');
    t.mode('audio');
    for (const [f, off] of [['mp3', false], ['m4a', false], ['opus', false], ['flac', true], ['wav', true]]) {
      t.set('audio-format', f);
      t.eq(t.$('audio-quality').disabled, off, f + ' bitrate disabled');
    }
    t.set('audio-format', 'opus'); t.eq(t.$('audio-quality').options[0].textContent, 'Best (160 kbps)', 'opus best label');
    t.set('audio-format', 'm4a');  t.eq(t.$('audio-quality').options[0].textContent, 'Best (256 kbps AAC)', 'm4a best label');
    t.set('audio-format', 'flac'); t.ok(t.$('audio-hint').textContent.includes('Lossless'), 'flac hint');
  `],
  ['formats: copy note shows on the job card (live download)', `
    const job = await t.post({ url: 'https://www.youtube.com/watch?v=jNQXAC9IVRw', mode: 'audio', audio_format: 'opus', audio_quality: '128', outdir: t.$('outdir').value });
    const note = await t.until(() => document.querySelector('.job[data-id="' + job.id + '"] .note'), 60000);
    t.ok(note.textContent.includes('bitrate not applied'), 'note text: ' + note.textContent);
  `],
  ['keep: checkbox enabled only when converting, restored by "Use these settings"', `
    t.mode('merged'); t.ok(t.$('keep-original').disabled, 'merged disables keep');
    t.mode('audio'); t.set('audio-format', 'native'); t.ok(t.$('keep-original').disabled, 'original disables keep');
    t.set('audio-format', 'flac'); t.ok(!t.$('keep-original').disabled, 'flac enables keep');
    t.mode('separate'); t.ok(!t.$('keep-original').disabled, 'separate+flac enables keep');
    const job = await t.post({ url: 'https://example.invalid/y', mode: 'audio', audio_format: 'mp3', keep_original: true, outdir: t.$('outdir').value });
    const card = await t.until(() => document.querySelector('.job[data-id="' + job.id + '"]'));
    t.ok(card.textContent.includes('+ original'), 'card label shows + original');
    t.set('keep-original', false); t.mode('merged');
    [...card.querySelectorAll('button')].find((b) => b.textContent === 'Use these settings').click();
    t.ok(t.$('keep-original').checked && !t.$('keep-original').disabled, 'keep restored');
  `],
  ['keep: card lists original and converted files (live download)', `
    const job = await t.post({ url: 'https://www.youtube.com/watch?v=jNQXAC9IVRw', mode: 'audio', audio_format: 'mp3', keep_original: true, outdir: t.$('outdir').value });
    const files = await t.until(() => { const f = document.querySelectorAll('.job[data-id="' + job.id + '"] .file'); return f.length === 2 && f; }, 90000);
    t.ok(files[0].textContent.startsWith('original: ') && files[0].textContent.endsWith('.webm'), 'original row: ' + files[0].textContent);
    t.ok(files[1].textContent.startsWith('converted: ') && files[1].textContent.endsWith('.mp3'), 'converted row: ' + files[1].textContent);
  `],
];

// ---------------------------------------------------------------------------
function freePort() {
  return new Promise((res) => {
    const s = createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
  });
}

async function waitHttp(url, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { const r = await fetch(url); if (r.ok) return r; } catch (_) { /* not up yet */ }
    await sleep(150);
  }
  throw new Error(`not reachable: ${url}`);
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [];
    ws.onmessage = (m) => { const d = JSON.parse(m.data);
      if (d.id && this.pending.has(d.id)) { this.pending.get(d.id)(d); this.pending.delete(d.id); } else this.events.push(d); };
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, (d) => (d.error ? rej(new Error(d.error.message)) : res(d.result))));
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
}

async function main() {
  const chosen = CHECKS.filter(([n]) => !filters.length || filters.some((f) => n.includes(f)));
  const tmp = mkdtempSync(join(tmpdir(), 'yt-gui-ui-'));
  const port = await freePort();
  const server = spawn('python3', [SERVER, '--port', port, '--root', tmp, '--outdir', join(tmp, 'dl'),
    ...(process.env.UI_SERVER_ARGS ? process.env.UI_SERVER_ARGS.split(' ') : [])], { stdio: 'ignore' });
  const profile = join(tmp, 'profile');
  const chrome = spawn('chromium', ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--window-size=1200,1400', 'about:blank'], { stdio: 'ignore' });

  let failed = 0;
  try {
    const app = `http://127.0.0.1:${port}/`;
    await waitHttp(app + 'api/config');
    const portFile = join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 100 && !existsSync(portFile); i++) await sleep(100);
    const devPort = readFileSync(portFile, 'utf8').split('\n')[0];
    const targets = await (await waitHttp(`http://127.0.0.1:${devPort}/json/list`)).json();
    const page = targets.find((t) => t.type === 'page');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((r) => { ws.onopen = r; });
    const cdp = new CDP(ws);
    await cdp.send('Runtime.enable');

    for (const [name, body] of chosen) {
      await cdp.send('Page.navigate', { url: app });
      await sleep(300);
      let err = null;
      try {
        await cdp.eval(`(async () => { ${PAGE_HELPERS}
          await t.until(() => document.getElementById('outdir').value);
          ${body} })()`);
      } catch (e) { err = e.message.split('\n')[0]; }
      if (shots) {
        const { data } = await cdp.send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(join(tmp, `${name.replace(/\W+/g, '_')}.png`), Buffer.from(data, 'base64'));
      }
      console.log(`${err ? 'FAIL' : 'PASS'}  ${name}${err ? `\n        ${err}` : ''}`);
      failed += err ? 1 : 0;
    }
    ws.close();
  } finally {
    chrome.kill();
    server.kill();
  }
  console.log(`\n${chosen.length - failed}/${chosen.length} passed`);
  if (shots || failed) console.log(`artifacts: ${tmp}`);
  else { await sleep(300); rmSync(tmp, { recursive: true, force: true }); }
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
