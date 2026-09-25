/**
 * V2R 시트 API (Apps Script 웹앱) — 2026-09-25
 * 브라우저 조작 없이 HTTP로 시트를 원자적으로 갱신한다.
 * 허용 시트 5개(브랜드 두 번째 탭)만, 허용 작업 4개만. 비밀번호 열(E)은 절대 읽지도 쓰지도 않는다.
 */
var ALLOWED = {
  "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc": 1325327696, // 팥순이
  "1QYhQFnZznQ-NjIjwiIlmJcf6zkQlb6t3TBK-W6OwJm0": 1454846576, // 우아덤
  "1Ey_laLK5-yJx2malh8s0vbmECpVW7CZQklCGKT3x0ek": 607459493,  // 코숨핏
  "1J8Nq-UQxLzrt3fOqIkZ2HRskZJTFQlOjmFjIh3wlzBs": 295921914,  // 장으뜸
  "1mgqghfeNrSZ1bTSrfTYWojtPGMc0u-K4VXEikxrHLOw": 1782605844  // 뉴더미스
};
var KEY_COL = 8;            // H 키워드
var FORBIDDEN_COLS = [2,3,4,5,6]; // B~F 는 쓰기 금지(E 비밀번호 포함)
var WRITABLE = {A:1, G:7, I:9, J:10, K:11, L:12, M:13, N:14};

function norm(s){ return String(s||"").replace(/\s+/g,"").toLowerCase(); }

function getSheet(sid){
  var gid = ALLOWED[sid]; if (gid === undefined) throw new Error("허용되지 않은 시트");
  var ss = SpreadsheetApp.openById(sid);
  var sheets = ss.getSheets();
  for (var i=0;i<sheets.length;i++) if (sheets[i].getSheetId() === gid) return sheets[i];
  throw new Error("탭을 찾지 못함");
}

function keyIndex(sh){
  var last = sh.getLastRow(); if (last < 2) return {};
  var keys = sh.getRange(2, KEY_COL, last-1, 1).getValues();
  var idx = {};
  for (var i=0;i<keys.length;i++){ var k = norm(keys[i][0]); if (k && idx[k]===undefined) idx[k] = i+2; }
  return idx;
}

function doPost(e){
  var lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    var req = JSON.parse(e.postData.contents);
    var sh = getSheet(req.spreadsheet_id);
    var out;
    if (req.action === "append") out = append(sh, req.rows);           // rows: [{keyword, K, G, I, ...}]
    else if (req.action === "update_by_key") out = updateByKey(sh, req.updates); // updates: [{keyword, G, J, K, L, A, I}]
    else if (req.action === "delete_by_key") out = deleteByKey(sh, req.keywords);
    else if (req.action === "snapshot") out = snapshot(sh);
    else if (req.action === "reapply_format") out = reapplyFormat(sh);
    else if (req.action === "set_header") out = setHeader(sh, req.col, req.value);   // 1행 머리글 한 칸 복구(예: A1 '카페')   // 2행 서식·드롭다운을 전체 데이터 행에 다시 복사(값 불변)
    else throw new Error("허용되지 않은 작업");
    return ContentService.createTextOutput(JSON.stringify({ok:true, result:out})).setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({ok:false, error:String(err)})).setMimeType(ContentService.MimeType.JSON);
  } finally { lock.releaseLock(); }
}

function append(sh, rows){
  var idx = keyIndex(sh); var start = sh.getLastRow()+1; var added = 0, skipped = 0; var values = [];
  var width = 14;
  for (var i=0;i<rows.length;i++){
    var r = rows[i]; var k = norm(r.keyword); if (!k || idx[k]!==undefined) { skipped++; continue; }
    var row = []; for (var c=0;c<width;c++) row.push("");
    row[KEY_COL-1] = r.keyword;
    for (var col in WRITABLE) if (r[col] !== undefined) row[WRITABLE[col]-1] = r[col];
    values.push(row); idx[k] = start + values.length - 1; added++;
  }
  if (values.length){
    sh.getRange(start, 1, values.length, width).setValues(values);
    // 2행 서식·데이터 확인(드롭다운) 복사 — 값은 그대로
    sh.getRange(2, 1, 1, width).copyTo(sh.getRange(start, 1, values.length, width), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    sh.getRange(2, 1, 1, width).copyTo(sh.getRange(start, 1, values.length, width), SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
  }
  return {added: added, skipped: skipped, first_row: start, last_row: sh.getLastRow()};
}

function updateByKey(sh, updates){
  var idx = keyIndex(sh); var done = 0, missing = [];
  for (var i=0;i<updates.length;i++){
    var u = updates[i]; var row = idx[norm(u.keyword)];
    if (!row) { missing.push(u.keyword); continue; }
    for (var col in WRITABLE) if (u[col] !== undefined) sh.getRange(row, WRITABLE[col]).setValue(u[col]);
    done++;
  }
  return {updated: done, missing: missing};
}

function deleteByKey(sh, keywords){
  var idx = keyIndex(sh); var rows = [];
  for (var i=0;i<keywords.length;i++){ var r = idx[norm(keywords[i])]; if (r) rows.push(r); }
  rows.sort(function(a,b){return b-a;});
  for (var j=0;j<rows.length;j++) sh.deleteRow(rows[j]);
  return {deleted: rows.length};
}

function snapshot(sh){
  var last = sh.getLastRow(); if (last < 1) return {rows: []};
  var v = sh.getRange(1, 1, last, 14).getValues();
  for (var i=0;i<v.length;i++){ v[i][4] = ""; } // E(비밀번호) 제거
  return {rows: v, last_row: last};
}

// 2026-09-25 사용자 지시: 키워드 추가 행도 기존 서식(A·G열 드롭다운 등)과 같아야 한다.
// 옛 브라우저 붙여넣기로 들어간 행은 데이터 확인이 빠져 있어, 2행의 서식·데이터 확인을 3행부터 마지막 행까지 다시 복사한다. 값은 건드리지 않는다.
function reapplyFormat(sh){
  var last = sh.getLastRow(); var width = sh.getLastColumn();
  if (last < 3) return {rows: 0};
  var src = sh.getRange(2, 1, 1, width);
  var dst = sh.getRange(3, 1, last - 2, width);
  src.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
  src.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
  return {rows: last - 2, width: width};
}

// 1행 머리글 복구. 사고(2026-09-25)로 팥순이 노출 현황 탭 A1이 'A9199'로 바뀐 것을 되돌리는 용도. 1행만 허용.
function setHeader(sh, col, value){
  var c = String(col||"").toUpperCase();
  if (!/^[A-Z]{1,2}$/.test(c)) throw new Error("머리글 열이 올바르지 않음");
  if (c === "E") throw new Error("E열은 다루지 않음");
  sh.getRange(c + "1").setValue(String(value||""));
  return {cell: c + "1", value: String(value||"")};
}
