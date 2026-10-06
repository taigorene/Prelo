// Prelo web UI: file selection, conversion options and calls to the REST API.
// Plain ES2020 module without dependencies. All URLs are relative so the page also works when the
// app is served behind a path prefix.

const ENDPOINTS = Object.freeze({
  info: 'api/v1/info',
  convert: (format) => `api/v1/convert/${encodeURIComponent(format)}`,
});

const STORAGE_KEY = 'prelo:preferences';
const FALLBACK_EXTENSIONS = ['.md', '.markdown', '.mdown', '.mkd', '.mkdn', '.txt', '.zip'];
const FALLBACK_TIMEOUT_SECONDS = 120;
// Extra time on top of the server-side conversion timeout (upload, queueing, download).
const CLIENT_TIMEOUT_MARGIN_SECONDS = 60;
// The server sends at most 50 warnings plus a line saying how many were left out.
const MAX_WARNINGS_SHOWN = 60;

const FORMATS = Object.freeze({
  pdf: { button: 'Converter para PDF', extension: '.pdf', mediaType: 'application/pdf' },
  docx: {
    button: 'Converter para Word',
    extension: '.docx',
    mediaType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  },
});

const OPTION_SELECTS = ['page_size', 'orientation', 'margin', 'mermaid_theme'];
const OPTION_CHECKBOXES = ['toc', 'page_numbers'];

const MESSAGES = Object.freeze({
  converting: 'Convertendo…',
  success: 'Pronto! Seu arquivo foi baixado.',
  network:
    'Não foi possível enviar o arquivo ao servidor. Verifique sua conexão e tente novamente. ' +
    'Se você alterou o arquivo depois de escolhê-lo, selecione-o de novo.',
  clientTimeout: 'A conversão está demorando mais que o esperado. Tente novamente em instantes.',
  unexpected: 'Ocorreu um erro inesperado. Recarregue a página e tente novamente.',
  emptyFile: 'O arquivo está vazio. Escolha um arquivo Markdown com conteúdo.',
  multipleFiles:
    'Envie um arquivo por vez. Para incluir imagens, compacte o Markdown e as imagens em um .zip.',
  directory: 'Pastas não são aceitas. Compacte a pasta em um arquivo .zip e envie novamente.',
});

const STATUS_MESSAGES = Object.freeze({
  400: 'O arquivo enviado não pôde ser lido como Markdown.',
  404: 'O serviço de conversão não foi encontrado neste endereço.',
  413: 'O arquivo é maior que o limite aceito pelo servidor.',
  415: 'Tipo de arquivo não suportado. Envie um Markdown (.md), texto (.txt) ou .zip.',
  422: 'Algumas opções enviadas são inválidas. Recarregue a página e tente novamente.',
  429: 'Muitas conversões em sequência. Aguarde um pouco e tente novamente.',
  500: 'Erro interno ao converter o documento.',
  502: 'O serviço de conversão está indisponível no momento. Tente novamente em instantes.',
  503: 'O serviço de conversão está indisponível no momento. Tente novamente em instantes.',
  504: 'A conversão excedeu o tempo limite.',
});

/** Error whose message is safe and meaningful to show to the user. */
class UserFacingError extends Error {}

const byId = (id) => document.getElementById(id);

const ui = {
  body: document.body,
  form: byId('converter'),
  controls: byId('controls'),
  fileInput: byId('file-input'),
  dropzone: byId('dropzone'),
  sizeLimit: byId('size-limit'),
  chip: byId('file-chip'),
  fileBadge: byId('file-badge'),
  fileName: byId('file-name'),
  fileMeta: byId('file-meta'),
  fileRemove: byId('file-remove'),
  fileError: byId('file-error'),
  fileErrorText: byId('file-error-text'),
  optionsSummary: byId('options-summary'),
  convert: byId('convert'),
  convertLabel: byId('convert-label'),
  result: byId('result'),
  resultName: byId('result-name'),
  resultMeta: byId('result-meta'),
  warnings: byId('warnings'),
  warningsCount: byId('warnings-count'),
  warningsList: byId('warnings-list'),
  downloadAgain: byId('download-again'),
  error: byId('error'),
  errorMessage: byId('error-message'),
  announcer: byId('announcer'),
  version: byId('version'),
  overlay: byId('drop-overlay'),
};

const state = {
  file: null,
  busy: false,
  maxBytes: null, // unknown until /api/v1/info answers; the server enforces it anyway
  maxUploadMb: null,
  extensions: FALLBACK_EXTENSIONS,
  timeoutSeconds: FALLBACK_TIMEOUT_SECONDS,
  download: null, // { url, filename } of the last converted file
  preferencesTouched: false,
  dragDepth: 0,
};

// ---------------------------------------------------------------------------
// Formatting helpers (pt-BR)
// ---------------------------------------------------------------------------

const decimal1 = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 1 });
const integer = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 0 });

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${(value < 10 ? decimal1 : integer).format(value)} ${units[unit]}`;
}

function formatDuration(ms) {
  if (ms < 1000) return `${integer.format(ms)} ms`;
  return `${decimal1.format(ms / 1000)} s`;
}

function extensionOf(name) {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot).toLowerCase() : '';
}

function stemOf(name) {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(0, dot) : name;
}

function sanitizeFilename(name) {
  const cleaned = name
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .trim();
  return cleaned.replace(/^\.+/, '');
}

// ---------------------------------------------------------------------------
// Live announcements and notices
// ---------------------------------------------------------------------------

let announceTimer = 0;

function announce(message) {
  // Clear first so repeating the same message is announced again; a newer message cancels a
  // pending one so it can never be overwritten by stale text.
  window.clearTimeout(announceTimer);
  ui.announcer.textContent = '';
  announceTimer = window.setTimeout(() => {
    ui.announcer.textContent = message;
  }, 60);
}

function hideNotices() {
  ui.result.hidden = true;
  ui.error.hidden = true;
}

function revealNotice(element) {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  element.scrollIntoView({ block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
}

function showError(message) {
  ui.result.hidden = true;
  ui.errorMessage.textContent = message;
  ui.error.hidden = false;
  announce(`Não foi possível converter. ${message}`);
  revealNotice(ui.error);
}

function showFileError(message) {
  ui.fileErrorText.textContent = message;
  ui.fileError.hidden = false;
  announce(message);
}

function clearFileError() {
  ui.fileError.hidden = true;
  ui.fileErrorText.textContent = '';
}

// ---------------------------------------------------------------------------
// Preferences (format + options), persisted best-effort in localStorage
// ---------------------------------------------------------------------------

function currentFormat() {
  const checked = ui.form.querySelector('input[name="format"]:checked');
  return checked && checked.value in FORMATS ? checked.value : 'pdf';
}

function readOptions() {
  const options = {};
  for (const name of OPTION_SELECTS) options[name] = ui.form.elements[name].value;
  for (const name of OPTION_CHECKBOXES) options[name] = ui.form.elements[name].checked;
  return options;
}

function applyOptions(values) {
  if (!values || typeof values !== 'object') return;
  for (const name of OPTION_SELECTS) {
    const select = ui.form.elements[name];
    const value = values[name];
    if (typeof value === 'string' && Array.from(select.options).some((o) => o.value === value)) {
      select.value = value;
    }
  }
  for (const name of OPTION_CHECKBOXES) {
    if (typeof values[name] === 'boolean') ui.form.elements[name].checked = values[name];
  }
}

function applyFormat(format) {
  if (!(format in FORMATS)) return;
  const radio = ui.form.querySelector(`input[name="format"][value="${format}"]`);
  if (radio) radio.checked = true;
}

function loadPreferences() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

function savePreferences() {
  try {
    const preferences = { format: currentFormat(), ...readOptions() };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  } catch {
    // Storage may be unavailable (private mode, blocked cookies); preferences are optional.
  }
}

function renderFormat() {
  const format = currentFormat();
  ui.body.dataset.format = format;
  if (!state.busy) ui.convertLabel.textContent = FORMATS[format].button;
}

function renderOptionsSummary() {
  const parts = OPTION_SELECTS.slice(0, 3).map((name) => {
    const select = ui.form.elements[name];
    const option = select.options[select.selectedIndex];
    return option ? option.dataset.short || option.textContent.trim() : '';
  });
  const theme = ui.form.elements.mermaid_theme;
  if (theme.value !== 'default') parts.push(theme.options[theme.selectedIndex].dataset.short);
  if (ui.form.elements.toc.checked) parts.push('com sumário');
  if (!ui.form.elements.page_numbers.checked) parts.push('sem numeração');
  ui.optionsSummary.textContent = parts.filter(Boolean).join(', ');
}

// ---------------------------------------------------------------------------
// File selection
// ---------------------------------------------------------------------------

function validateFile(file) {
  const extension = extensionOf(file.name);
  // A name without extension (README, NOTES...) is left to the server, which checks the
  // content itself (text or .zip) and explains what is wrong.
  if (extension && !state.extensions.includes(extension)) {
    return (
      `Arquivos ${extension} não são aceitos. Envie um Markdown (.md, .markdown), ` +
      'texto (.txt) ou um .zip com o Markdown e as imagens.'
    );
  }
  if (file.size === 0) return MESSAGES.emptyFile;
  if (state.maxBytes !== null && file.size > state.maxBytes) {
    const limit = `${decimal1.format(state.maxUploadMb)} MB`;
    const size = formatBytes(file.size);
    // Rounding can make both numbers look equal (e.g. "1 MB"); then omit the file size.
    return size === limit
      ? `O arquivo passa do limite de ${limit}.`
      : `O arquivo tem ${size} e passa do limite de ${limit}.`;
  }
  return '';
}

function selectFile(file) {
  const problem = validateFile(file);
  if (problem) {
    showFileError(problem);
    return;
  }
  // The dropzone is about to be hidden: keep keyboard focus inside the card.
  const active = document.activeElement;
  const replacingFocus = active === ui.dropzone || active === document.body;
  clearFileError();
  hideNotices();
  state.file = file;

  const extension = extensionOf(file.name);
  ui.fileBadge.textContent = extension === '.zip' ? 'ZIP' : extension === '.txt' ? 'TXT' : 'MD';
  ui.fileName.textContent = file.name;
  ui.fileName.title = file.name;
  ui.fileMeta.textContent =
    extension === '.zip' ? `${formatBytes(file.size)}, Markdown + imagens` : formatBytes(file.size);
  ui.fileRemove.setAttribute('aria-label', `Remover arquivo ${file.name}`);
  ui.chip.hidden = false;
  ui.dropzone.hidden = true;
  if (replacingFocus) ui.fileRemove.focus();

  updateConvertButton();
  announce(`Arquivo selecionado: ${file.name}, ${formatBytes(file.size)}.`);
}

function clearFile() {
  state.file = null;
  ui.fileInput.value = '';
  ui.chip.hidden = true;
  ui.dropzone.hidden = false;
  clearFileError();
  hideNotices();
  updateConvertButton();
  ui.dropzone.focus();
  announce('Arquivo removido.');
}

function openFilePicker() {
  if (!state.busy) ui.fileInput.click();
}

function handleDroppedData(dataTransfer) {
  const files = Array.from(dataTransfer.files || []);
  if (files.length === 0) return;
  if (files.length > 1) {
    showFileError(MESSAGES.multipleFiles);
    return;
  }
  // A dropped folder shows up as a File too; the entry API is the reliable way to tell.
  const item = dataTransfer.items && dataTransfer.items[0];
  const canInspect = item && typeof item.webkitGetAsEntry === 'function';
  const entry = canInspect ? item.webkitGetAsEntry() : null;
  if (entry && entry.isDirectory) {
    showFileError(MESSAGES.directory);
    return;
  }
  selectFile(files[0]);
}

// Drops are accepted anywhere on the page; a depth counter tracks enter/leave across children.
function hasFiles(event) {
  const types = event.dataTransfer ? Array.from(event.dataTransfer.types || []) : [];
  return types.includes('Files');
}

function setDragging(active) {
  const show = active && !state.busy;
  ui.body.classList.toggle('is-dragging', show);
  ui.overlay.hidden = !show;
}

function bindDragAndDrop() {
  window.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    state.dragDepth += 1;
    setDragging(true);
  });
  window.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = state.busy ? 'none' : 'copy';
  });
  window.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    state.dragDepth = Math.max(0, state.dragDepth - 1);
    if (state.dragDepth === 0) setDragging(false);
  });
  window.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    state.dragDepth = 0;
    setDragging(false);
    if (!state.busy) handleDroppedData(event.dataTransfer);
  });
}

// ---------------------------------------------------------------------------
// Conversion
// ---------------------------------------------------------------------------

function updateConvertButton() {
  ui.convert.disabled = !state.file;
}

function setBusy(busy) {
  state.busy = busy;
  ui.controls.disabled = busy;
  ui.form.classList.toggle('is-busy', busy);
  ui.form.setAttribute('aria-busy', String(busy));
  // aria-disabled (not disabled) keeps keyboard focus on the button while it works.
  if (busy) ui.convert.setAttribute('aria-disabled', 'true');
  else ui.convert.removeAttribute('aria-disabled');
  ui.convertLabel.textContent = busy ? MESSAGES.converting : FORMATS[currentFormat()].button;
}

function filenameFromDisposition(header) {
  if (!header) return '';
  const extended = /filename\*\s*=\s*([\w!#$%&+^`{}~.-]*)'[^']*'([^;\s]+)/i.exec(header);
  if (extended && /^utf-8$/i.test(extended[1])) {
    try {
      const name = sanitizeFilename(decodeURIComponent(extended[2].replace(/^"|"$/g, '')));
      if (name) return name;
    } catch {
      // Malformed percent-encoding: fall back to the plain parameter.
    }
  }
  const plain = /(?:^|;)\s*filename\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)/i.exec(header);
  if (!plain) return '';
  let value = plain[1].trim();
  if (value.startsWith('"')) value = value.slice(1, -1).replace(/\\(.)/g, '$1');
  return sanitizeFilename(value);
}

function parseWarnings(header) {
  if (!header) return [];
  try {
    const parsed = JSON.parse(header);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item) => typeof item === 'string' && item.trim())
      .map((item) => item.trim())
      .slice(0, MAX_WARNINGS_SHOWN);
  } catch {
    return [];
  }
}

async function describeFailure(response) {
  const contentType = response.headers.get('Content-Type') || '';
  if (contentType.includes('json')) {
    try {
      const body = await response.json();
      if (body && typeof body.detail === 'string' && body.detail.trim()) return body.detail.trim();
    } catch {
      // Not valid JSON: use the generic message for the status below.
    }
  }
  if (response.status === 413 && state.maxUploadMb !== null) {
    const limit = decimal1.format(state.maxUploadMb);
    return `O arquivo é maior que o limite aceito pelo servidor (${limit} MB).`;
  }
  return (
    STATUS_MESSAGES[response.status] ||
    `O servidor respondeu com o erro ${response.status}. Tente novamente em instantes.`
  );
}

function storeDownload(blob, filename) {
  if (state.download) URL.revokeObjectURL(state.download.url);
  state.download = { url: URL.createObjectURL(blob), filename };
}

function startDownload() {
  if (!state.download) return;
  const link = document.createElement('a');
  link.href = state.download.url;
  link.download = state.download.filename;
  link.rel = 'noopener';
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
}

function showResult({ filename, size, durationMs, warnings, warningsTotal }) {
  ui.error.hidden = true;
  ui.resultName.textContent = filename;
  const details = [formatBytes(size)];
  if (Number.isFinite(durationMs) && durationMs > 0) {
    details.push(`gerado em ${formatDuration(durationMs)}`);
  }
  ui.resultMeta.textContent = details.join(', ');

  ui.warningsList.replaceChildren(
    ...warnings.map((text) => {
      const item = document.createElement('li');
      item.textContent = text;
      return item;
    }),
  );
  // The list may end with "… e mais N aviso(s) omitido(s)": count from the server's total.
  const total = Math.max(warningsTotal || 0, warnings.length);
  ui.warningsCount.textContent =
    total === 1 ? '1 aviso durante a conversão' : `${total} avisos durante a conversão`;
  ui.warnings.open = false;
  ui.warnings.hidden = warnings.length === 0;

  ui.result.hidden = false;
  const suffix = warnings.length ? ` ${ui.warningsCount.textContent}.` : '';
  announce(`${MESSAGES.success} ${filename}.${suffix}`);
  revealNotice(ui.result);
}

async function requestConversion(file, format, options) {
  const payload = new FormData();
  payload.append('file', file, file.name);
  for (const [name, value] of Object.entries(options)) payload.append(name, String(value));

  const controller = new AbortController();
  const timer = window.setTimeout(
    () => controller.abort(),
    (state.timeoutSeconds + CLIENT_TIMEOUT_MARGIN_SECONDS) * 1000,
  );
  try {
    const response = await fetch(ENDPOINTS.convert(format), {
      method: 'POST',
      body: payload,
      signal: controller.signal,
      headers: { Accept: `${FORMATS[format].mediaType}, application/json` },
    });
    if (!response.ok) throw new UserFacingError(await describeFailure(response));
    const blob = await response.blob();
    return { response, blob };
  } catch (error) {
    if (error instanceof UserFacingError) throw error;
    if (error && error.name === 'AbortError') throw new UserFacingError(MESSAGES.clientTimeout);
    if (error instanceof TypeError) throw new UserFacingError(MESSAGES.network);
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function convert() {
  if (!state.file || state.busy) return;
  const file = state.file;
  const format = currentFormat();

  hideNotices();
  clearFileError();
  setBusy(true);
  announce(MESSAGES.converting);
  try {
    const { response, blob } = await requestConversion(file, format, readOptions());
    const filename =
      filenameFromDisposition(response.headers.get('Content-Disposition')) ||
      `${sanitizeFilename(stemOf(file.name)) || 'documento'}${FORMATS[format].extension}`;
    storeDownload(blob, filename);
    startDownload();
    showResult({
      filename,
      size: blob.size,
      durationMs: Number(response.headers.get('X-Conversion-Time-Ms')),
      warnings: parseWarnings(response.headers.get('X-Conversion-Warnings')),
      warningsTotal: Number(response.headers.get('X-Conversion-Warnings-Count')),
    });
  } catch (error) {
    if (!(error instanceof UserFacingError)) console.error(error);
    showError(error instanceof UserFacingError ? error.message : MESSAGES.unexpected);
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------------------
// Server info (limits, defaults, version)
// ---------------------------------------------------------------------------

async function loadInfo(hasStoredPreferences) {
  let info;
  try {
    const response = await fetch(ENDPOINTS.info, { headers: { Accept: 'application/json' } });
    if (!response.ok) return;
    info = await response.json();
  } catch {
    return; // API unreachable: keep the built-in fallbacks.
  }
  if (!info || typeof info !== 'object') return;

  const limits = info.limits || {};
  if (typeof limits.max_upload_mb === 'number' && limits.max_upload_mb > 0) {
    state.maxUploadMb = limits.max_upload_mb;
    state.maxBytes = Math.floor(limits.max_upload_mb * 1024 * 1024);
    ui.sizeLimit.textContent = ` · até ${decimal1.format(limits.max_upload_mb)} MB`;
    ui.sizeLimit.hidden = false;
  }
  const timeout = limits.conversion_timeout_seconds;
  if (typeof timeout === 'number' && timeout > 0) state.timeoutSeconds = timeout;
  if (Array.isArray(info.accepted_extensions)) {
    const extensions = info.accepted_extensions
      .filter((ext) => typeof ext === 'string' && ext.startsWith('.'))
      .map((ext) => ext.toLowerCase());
    if (extensions.length) state.extensions = extensions;
  }
  if (typeof info.version === 'string' && info.version) {
    ui.version.textContent = `v${info.version}`;
    ui.version.hidden = false;
  }
  if (!hasStoredPreferences && !state.preferencesTouched) {
    applyOptions(info.defaults);
    renderOptionsSummary();
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function init() {
  const stored = loadPreferences();
  if (stored) {
    applyFormat(stored.format);
    applyOptions(stored);
  }
  renderFormat();
  renderOptionsSummary();
  updateConvertButton();

  ui.dropzone.addEventListener('click', openFilePicker);
  ui.fileInput.addEventListener('change', () => {
    const [file] = ui.fileInput.files || [];
    if (file) selectFile(file);
    ui.fileInput.value = ''; // allow choosing the same file again
  });
  ui.fileRemove.addEventListener('click', clearFile);

  ui.form.addEventListener('change', (event) => {
    if (event.target === ui.fileInput) return;
    state.preferencesTouched = true;
    if (event.target.name === 'format') renderFormat();
    renderOptionsSummary();
    savePreferences();
  });

  ui.form.addEventListener('submit', (event) => {
    event.preventDefault();
    convert();
  });
  ui.downloadAgain.addEventListener('click', startDownload);

  bindDragAndDrop();
  loadInfo(Boolean(stored));
}

init();
