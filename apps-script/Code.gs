/**
 * Shared decisions + activity log for the Trading Hiring Directory.
 * Every action (job Relevant / Not relevant / clear, company add / remove / restore)
 * is appended as one row: who did what, to which item, when, and why.
 *
 * Deploy: Deploy > New deployment > Web app; Execute as "Me"; Who has access "Anyone".
 * Passcode: Project Settings > Script properties > add PASSCODE.
 */
const SHEET = 'Activity';
const HEAD = ['timestamp', 'entity', 'item_id', 'action', 'who', 'reason', 'title', 'company', 'url', 'details'];
const ACTIONS = {
  job: ['relevant', 'not_relevant', 'clear'],
  company: ['add', 'remove', 'restore']
};

function sheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET);
  if (!sh) { sh = ss.insertSheet(SHEET); sh.appendRow(HEAD); sh.setFrozenRows(1); }
  return sh;
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// GET: the full activity log, oldest first
function doGet() {
  const rows = sheet_().getDataRange().getValues();
  const head = rows.shift() || HEAD;
  return out_({ ok: true, log: rows.map(r => Object.fromEntries(head.map((h, i) => [h, r[i]]))) });
}

// POST (text/plain JSON): {passcode, entity, item_id, action, who, reason, title, company, url, details}
function doPost(e) {
  let d;
  try { d = JSON.parse(e.postData.contents); } catch (err) { return out_({ ok: false, error: 'Bad request' }); }
  const pass = PropertiesService.getScriptProperties().getProperty('PASSCODE') || '';
  if (!pass || d.passcode !== pass) return out_({ ok: false, error: 'Wrong passcode' });
  if (!ACTIONS[d.entity] || !ACTIONS[d.entity].includes(d.action)) return out_({ ok: false, error: 'Bad action' });
  const who = String(d.who || '').slice(0, 40);
  if (!who) return out_({ ok: false, error: 'Missing name' });
  const cut = (v, n) => String(v == null ? '' : v).slice(0, n);
  const lock = LockService.getScriptLock(); lock.waitLock(10000);
  try {
    sheet_().appendRow([new Date().toISOString(), d.entity, cut(d.item_id, 120), d.action, who, cut(d.reason, 500),
      cut(d.title, 300), cut(d.company, 120), cut(d.url, 500), cut(typeof d.details === 'string' ? d.details : JSON.stringify(d.details || {}), 2000)]);
  } finally { lock.releaseLock(); }
  return out_({ ok: true });
}
