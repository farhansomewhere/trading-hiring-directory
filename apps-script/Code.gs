/**
 * Shared Relevant / Not relevant decisions for the Trading Hiring Directory.
 * Deploy as a Web app: Execute as "Me", access "Anyone".
 * Set a passcode once: Project Settings > Script properties > add PASSCODE.
 */
const SHEET = 'Decisions';
const HEAD = ['timestamp', 'job_id', 'decision', 'who', 'reason', 'title', 'company', 'url'];

function sheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET);
  if (!sh) { sh = ss.insertSheet(SHEET); sh.appendRow(HEAD); sh.setFrozenRows(1); }
  return sh;
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// GET: every decision row, oldest first
function doGet() {
  const rows = sheet_().getDataRange().getValues();
  const head = rows.shift() || HEAD;
  return out_({ ok: true, decisions: rows.map(r => Object.fromEntries(head.map((h, i) => [h, r[i]]))) });
}

// POST (text/plain JSON): {passcode, job_id, decision: "relevant"|"not_relevant"|"clear", who, reason, title, company, url}
function doPost(e) {
  let d;
  try { d = JSON.parse(e.postData.contents); } catch (err) { return out_({ ok: false, error: 'Bad request' }); }
  const pass = PropertiesService.getScriptProperties().getProperty('PASSCODE') || '';
  if (!pass || d.passcode !== pass) return out_({ ok: false, error: 'Wrong passcode' });
  if (!['relevant', 'not_relevant', 'clear'].includes(d.decision)) return out_({ ok: false, error: 'Bad decision' });
  const who = String(d.who || '').slice(0, 40);
  if (!who) return out_({ ok: false, error: 'Missing name' });
  const lock = LockService.getScriptLock(); lock.waitLock(10000);
  try {
    sheet_().appendRow([new Date().toISOString(), String(d.job_id).slice(0, 40), d.decision, who,
      String(d.reason || '').slice(0, 500), String(d.title || '').slice(0, 300), String(d.company || '').slice(0, 120), String(d.url || '').slice(0, 500)]);
  } finally { lock.releaseLock(); }
  return out_({ ok: true });
}
