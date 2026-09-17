'use strict';

const $ = (id) => document.getElementById(id);
const form = $('job-form');
const listEl = $('job-list');
let config = null;

const MODE_HINTS = {
  merged: 'One file containing both video and audio (needs ffmpeg).',
  separate: 'Two independent downloads: <title>.video.<ext> and <title>.audio.<ext> — never merged.',
  audio: 'Best audio stream only. Quality preset does not apply.',
  video: 'Video stream only — the file will have no audio track.',
};

const AUDIO_DESC = {
  native: 'no re-encode',
  mp3: 'lossy',
  m4a: 'AAC, lossy',
  opus: 'lossy',
  flac: 'lossless',
  wav: 'uncompressed',
};

const COPY_NOTE = ' If the source already uses this codec, the stream is copied without re-encoding and the bitrate is not applied.';
const AUDIO_HINTS = {
  native: 'The audio stream is saved exactly as the site serves it (usually .webm or .m4a).',
  mp3: 'Re-encoded to MP3 with ffmpeg after downloading. Best = LAME VBR V0 (~245 kbps); fixed bitrates are CBR.' + COPY_NOTE,
  m4a: 'Encoded to AAC in an .m4a file — plays everywhere Apple does.' + COPY_NOTE,
  opus: 'Encoded to Opus — the smallest files for the quality. YouTube audio is usually Opus already.' + COPY_NOTE,
  flac: 'Lossless FLAC. It cannot restore quality the source never had, but nothing further is lost.',
  wav: 'Decoded to uncompressed 16-bit WAV with ffmpeg — about 10 MB per minute.',
};

const audioFormat = (key) => config?.audio_formats?.find((f) => f.key === key);

// --------------------------------------------------------------------- utils
function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

function fmtEta(s) {
  if (s === null || s === undefined) return null;
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h) return `${h}h${String(m % 60).padStart(2, '0')}m`;
  if (m) return `${m}m${String(s % 60).padStart(2, '0')}s`;
  return `${s}s`;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const slug = (s) => String(s).replace(/\s+/g, '-');

// -------------------------------------------------------------------- config
async function loadConfig() {
  config = await (await fetch('/api/config')).json();

  const chips = $('chips');
  chips.replaceChildren();
  chips.append(el('span', 'chip', `yt-dlp ${config.ytdlp_version || '?'}`));
  chips.append(el('span', `chip${config.ffmpeg ? '' : ' bad'}`, config.ffmpeg ? 'ffmpeg ok' : 'ffmpeg missing'));
  chips.append(el('span', 'chip', `max ${config.max_concurrent} concurrent`));

  if (!$('outdir').value) $('outdir').value = config.default_outdir;

  const canConvert = config.ffmpeg && config.ffprobe;
  $('audio-format').replaceChildren(...config.audio_formats.map((f) => {
    const o = el('option', null, AUDIO_DESC[f.key] ? `${f.label} (${AUDIO_DESC[f.key]})` : f.label);
    o.value = f.key;
    o.disabled = f.convert && !canConvert;
    return o;
  }));
  $('audio-quality').replaceChildren(...config.audio_qualities.map((q) => {
    const o = el('option', null, q.label);
    o.value = q.key;
    return o;
  }));

  if (!config.ffmpeg) {
    const merged = form.querySelector('input[value="merged"]');
    merged.disabled = true;
    if (merged.checked) form.querySelector('input[value="separate"]').checked = true;
  }

  $('footer-note').textContent =
    `Local only — bound to 127.0.0.1, no authentication. Output paths must sit under ${config.allowed_root}.`;
  syncMode();
}

function currentMode() {
  return form.querySelector('input[name=mode]:checked').value;
}

function syncMode() {
  const mode = currentMode();
  let hint = MODE_HINTS[mode];
  if (mode === 'merged' && !config?.ffmpeg) hint = 'ffmpeg was not found on PATH, so merged mode is unavailable.';
  $('mode-hint').textContent = hint;

  const hasAudioFile = mode === 'audio' || mode === 'separate';
  const fmt = audioFormat($('audio-format').value);
  $('audio-format').disabled = !hasAudioFile;
  $('audio-quality').disabled = !hasAudioFile || !fmt?.bitrate_ok;
  $('keep-original').disabled = !hasAudioFile || !fmt?.convert;
  const best = $('audio-quality').querySelector('option[value=best]');
  if (best) best.textContent = fmt?.best_label || 'Best';
  let audioHint = hasAudioFile ? (AUDIO_HINTS[fmt?.key] || '') : 'Audio format applies to Audio only and Separate modes.';
  if (hasAudioFile && config && !(config.ffmpeg && config.ffprobe)) audioHint += ' Conversion needs ffmpeg and ffprobe on PATH.';
  $('audio-hint').textContent = audioHint;
  $('quality').disabled = mode === 'audio';
}

// ---------------------------------------------------------------- rendering
function renderTask(task) {
  const wrap = el('div', 'task');

  const head = el('div', 'task-head');
  head.append(el('span', 'task-name', `${task.label} · ${task.state}`));

  const bits = [];
  if (task.total) bits.push(`${fmtBytes(task.downloaded)} / ${fmtBytes(task.total)}`);
  else if (task.downloaded) bits.push(fmtBytes(task.downloaded));
  if (task.speed) bits.push(`${fmtBytes(task.speed)}/s`);
  const eta = fmtEta(task.eta);
  if (eta && task.state === 'downloading') bits.push(`ETA ${eta}`);
  if (task.percent !== null && task.percent !== undefined) bits.unshift(`${task.percent}%`);
  head.append(el('span', 'task-num', bits.join('  ·  ')));
  wrap.append(head);

  const track = el('div', 'track');
  const fill = el('div', `fill ${task.state}`);
  const pct = task.state === 'done' ? 100 : task.percent;
  if (pct === null || pct === undefined) {
    if (['downloading', 'merging', 'converting'].includes(task.state)) fill.classList.add('indeterminate');
  } else {
    fill.style.width = `${pct}%`;
  }
  track.append(fill);
  wrap.append(track);

  if (task.files?.length > 1) {
    task.files.forEach((f) => wrap.append(el('div', 'file', `${f.role}: ${f.path}`)));
  } else if (task.filepath) {
    wrap.append(el('div', 'file', task.filepath));
  }
  (task.notes || []).forEach((n) => wrap.append(el('p', 'note', n)));
  if (task.error) wrap.append(el('p', 'error', task.error));
  return wrap;
}

function renderJob(job) {
  const card = el('div', 'job');
  card.dataset.id = job.id;

  const head = el('div', 'job-head');
  head.append(el('div', 'job-title', job.title || job.url));
  head.append(el('span', `state ${slug(job.state)}`, job.state));
  card.append(head);

  const q = job.mode === 'audio' ? 'best audio' : job.quality === 'best' ? 'best' : `${job.quality}p`;
  const af = job.audio_format && job.audio_format !== 'native' ? ` · ${job.audio_label}` : '';
  card.append(el('p', 'job-sub', `${job.mode_label} · ${q}${af} · ${job.outdir}`));
  if (job.title) card.append(el('p', 'job-sub', job.url));

  job.tasks.forEach((t) => card.append(renderTask(t)));

  if (job.error) card.append(el('p', 'error', job.error));

  const foot = el('div', 'job-foot');
  const active = ['queued', 'probing formats', 'downloading', 'merging', 'converting'].includes(job.state);
  if (active) {
    const btn = el('button', 'small', 'Cancel');
    btn.type = 'button';
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      await fetch(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
    });
    foot.append(btn);
  }
  const again = el('button', 'small ghost', 'Use these settings');
  again.type = 'button';
  again.addEventListener('click', () => {
    $('url').value = job.url;
    form.querySelector(`input[value="${job.mode}"]`).checked = true;
    $('quality').value = job.quality;
    $('audio-format').value = job.audio_format || 'native';
    $('audio-quality').value = job.audio_quality || 'best';
    $('keep-original').checked = !!job.keep_original;
    $('outdir').value = job.outdir;
    syncMode();
    $('url').focus();
  });
  foot.append(again);
  card.append(foot);

  return card;
}

function render(state) {
  const jobs = state.jobs || [];
  $('empty').hidden = jobs.length > 0;
  $('job-count').textContent = jobs.length ? `(${jobs.length})` : '';
  const frag = document.createDocumentFragment();
  jobs.forEach((j) => frag.append(renderJob(j)));
  listEl.replaceChildren(frag);
}

// ------------------------------------------------------------------ updates
function connect() {
  const src = new EventSource('/api/events');
  src.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); } catch (_) { /* ignore */ }
  };
  src.onerror = () => {
    src.close();
    setTimeout(connect, 2000);
    poll();
  };
}

async function poll() {
  try {
    render(await (await fetch('/api/jobs')).json());
  } catch (_) { /* server down; SSE reconnect will recover */ }
}

// --------------------------------------------------------------------- form
form.addEventListener('change', (e) => {
  if (['mode', 'audio_format'].includes(e.target.name)) syncMode();
});

$('reset-path').addEventListener('click', () => {
  $('outdir').value = config?.default_outdir || '';
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const errEl = $('form-error');
  errEl.hidden = true;
  const btn = $('submit');
  btn.disabled = true;
  $('form-hint').textContent = 'Submitting…';

  const body = {
    url: $('url').value.trim(),
    mode: currentMode(),
    quality: $('quality').value,
    audio_format: $('audio-format').value,
    audio_quality: $('audio-quality').value,
    keep_original: !$('keep-original').disabled && $('keep-original').checked,
    outdir: $('outdir').value.trim(),
  };

  try {
    const res = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'Could not start the job.';
      errEl.hidden = false;
    } else {
      $('url').value = '';
      poll();
    }
  } catch (err) {
    errEl.textContent = String(err);
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    $('form-hint').textContent = '';
  }
});

loadConfig().then(poll).then(connect);
