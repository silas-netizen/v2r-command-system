/**
 * V2R 시트 API (Apps Script 웹앱) — 2026-09-25
 * 브라우저 조작 없이 HTTP로 시트를 원자적으로 갱신한다.
 * 허용 시트 5개(브랜드 두 번째 탭)만, 허용 작업 10개(append·update_by_key·delete_by_key·snapshot·set_header·reapply_format·set_cells·delete_blank_rows·dedupe_by_key·info). 비밀번호 열(E)은 절대 읽지도 쓰지도 않는다.
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
    else if (req.action === "set_header") out = setHeader(sh, req.col, req.value);     // 1행 머리글 한 칸(E 제외)
    else if (req.action === "reapply_format") out = reapplyFormat(sh);                // 2행 서식 + A·G 드롭다운을 전체 데이터 행에
    else if (req.action === "set_cells") out = setCells(sh, req.cells);               // 합계표 등 임의 칸(B~F 데이터 행·E열 금지)
    else if (req.action === "delete_blank_rows") out = deleteBlankRows(sh);           // 키워드(H) 빈 행 제거
    else if (req.action === "dedupe_by_key") out = dedupeByKey(sh);                   // 정규화 키워드 중복 행 제거(앞 행 유지)
    else if (req.action === "info") out = info(sh);                                   // 행·열 수, 머리글, 드롭다운 누락 수
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

// ---------------------------------------------------------------------------
// 2026-09-25 추가 작업 6개 — 시트 작업에 필요한 것을 한 번에(사용자 지시: "필요한 걸 지금 한 번에 다 끝내").
// 공통 원칙: E열(비밀번호)은 절대 읽거나 쓰지 않는다. 데이터 행의 B~F는 쓰지 않는다. 서식 작업은 값 불변.
// ---------------------------------------------------------------------------

// 1행 머리글 복구(예: 팥순이 노출 현황 탭 A1 'A9199' → '카페').
function setHeader(sh, col, value){
  var c = String(col||"").toUpperCase();
  if (!/^[A-Z]{1,2}$/.test(c)) throw new Error("머리글 열이 올바르지 않음");
  if (c === "E") throw new Error("E열은 다루지 않음");
  sh.getRange(c + "1").setValue(String(value||""));
  return {cell: c + "1", value: String(value||"")};
}

// 열 하나에서 데이터 확인(드롭다운)이 걸린 첫 셀(2행부터). 없으면 null.
function findValidationSource(sh, col, last){
  if (last < 2) return null;
  var rules = sh.getRange(2, col, last-1, 1).getDataValidations();
  for (var i=0;i<rules.length;i++) if (rules[i][0]) return sh.getRange(i+2, col);
  return null;
}

// 2행 서식을 3행부터 끝까지 복사하고, A열·G열은 그 열의 첫 드롭다운 셀 기준으로 2행부터 끝까지 드롭다운을 다시 건다.
// 값은 건드리지 않는다. B~F 열의 데이터 확인은 손대지 않는다.
function reapplyFormat(sh){
  var last = sh.getLastRow(); var width = sh.getLastColumn();
  if (last < 2) return {rows: 0};
  var fixed = {};
  if (last >= 3) sh.getRange(2, 1, 1, width).copyTo(sh.getRange(3, 1, last-2, width), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
  var cols = {A:1, G:7};
  for (var name in cols){
    var src = findValidationSource(sh, cols[name], last);
    if (!src) { fixed[name] = "드롭다운 원본 없음"; continue; }
    var rule = src.getDataValidation();
    var arr = []; for (var r=0;r<last-1;r++) arr.push([rule]);
    try {
      sh.getRange(2, cols[name], last-1, 1).setDataValidations(arr);
      fixed[name] = last-1;
    } catch (e) {
      // 기존 규칙이 다른 파일의 범위를 가리켜 다시 못 거는 경우(코숨핏 실측 2026-09-25:
      // '_드롭다운'!B2:B3). G는 고정 목록, A는 열에 이미 있는 카페명 목록으로 새 규칙을 건다.
      var list = name === "G" ? ["노출완", "밀려남", "미확인"] : distinctValues(sh, cols[name], last);
      if (!list.length) { fixed[name] = "실패: " + String(e); continue; }
      var nr = SpreadsheetApp.newDataValidation().requireValueInList(list, true).setAllowInvalid(true).build();
      var arr2 = []; for (var r2=0;r2<last-1;r2++) arr2.push([nr]);
      sh.getRange(2, cols[name], last-1, 1).setDataValidations(arr2);
      fixed[name] = (last-1) + "(목록 " + list.length + ")";
    }
  }
  return {rows: last-2 > 0 ? last-2 : 0, dropdown: fixed};
}

// 임의 칸 쓰기(합계표 P2 등). 금지: E열 전체, 데이터 행(2행 이상)의 B~F.
function setCells(sh, cells){
  if (!cells || !cells.length) return {written: 0};
  var n = 0;
  for (var i=0;i<cells.length;i++){
    var a1 = String(cells[i].a1||"").toUpperCase();
    if (!/^[A-Z]{1,2}[0-9]+$/.test(a1)) throw new Error("칸 주소 오류: " + a1);
    var rg = sh.getRange(a1); var col = rg.getColumn(); var row = rg.getRow();
    if (col === 5) throw new Error("E열은 다루지 않음");
    if (row >= 2 && col >= 2 && col <= 6) throw new Error("데이터 행의 B~F는 쓰지 않음: " + a1);
    var v = cells[i].value; rg.setValue(v === undefined || v === null ? "" : v);
    n++;
  }
  return {written: n};
}

// 키워드(H)가 빈 행을 아래에서 위로 지운다(E열은 읽지 않음). A·G·I~N 중 값이 있는 행은 남기고 개수만 알린다.
function deleteBlankRows(sh){
  var last = sh.getLastRow(); if (last < 2) return {deleted: 0, kept_without_key: 0};
  var width = Math.max(sh.getLastColumn(), 14);
  var keys = sh.getRange(2, KEY_COL, last-1, 1).getValues();
  var a = sh.getRange(2, 1, last-1, 1).getValues();
  var g = sh.getRange(2, 7, last-1, 1).getValues();
  var rest = sh.getRange(2, 9, last-1, width-8).getValues();
  var del = [], kept = 0;
  for (var i=0;i<keys.length;i++){
    if (norm(keys[i][0])) continue;
    var has = norm(a[i][0]) || norm(g[i][0]);
    for (var c=0;c<rest[i].length && !has;c++) if (norm(rest[i][c])) has = true;
    if (has) { kept++; continue; }
    del.push(i+2);
  }
  var deleted = 0;
  for (var k=del.length-1;k>=0;){
    var end = del[k], start = end;
    while (k-1 >= 0 && del[k-1] === start-1) { k--; start = del[k]; }
    sh.deleteRows(start, end-start+1); deleted += end-start+1; k--;
  }
  return {deleted: deleted, kept_without_key: kept, last_row: sh.getLastRow()};
}

// 정규화 키워드가 같은 행이 여럿이면 맨 앞 행만 남기고 뒤 행을 지운다.
function dedupeByKey(sh){
  var last = sh.getLastRow(); if (last < 3) return {deleted: 0};
  var keys = sh.getRange(2, KEY_COL, last-1, 1).getValues();
  var seen = {}, del = [];
  for (var i=0;i<keys.length;i++){
    var k = norm(keys[i][0]); if (!k) continue;
    if (seen[k]) del.push(i+2); else seen[k] = true;
  }
  var deleted = 0;
  for (var j=del.length-1;j>=0;){
    var end = del[j], start = end;
    while (j-1 >= 0 && del[j-1] === start-1) { j--; start = del[j]; }
    sh.deleteRows(start, end-start+1); deleted += end-start+1; j--;
  }
  return {deleted: deleted, last_row: sh.getLastRow()};
}

// 점검용: 행·열 수, 1행 머리글(E 가림), A·G열 드롭다운 누락 행 수, 키워드 빈 행 수.
function info(sh){
  var last = sh.getLastRow(); var width = sh.getLastColumn();
  var header = last >= 1 ? sh.getRange(1, 1, 1, width).getValues()[0] : [];
  var h = []; for (var c=0;c<header.length;c++) h.push(c === 4 ? "(E)" : String(header[c]));
  var out = {last_row: last, last_col: width, header: h, missing_dropdown: {}, blank_key_rows: 0};
  if (last >= 2){
    var cols = {A:1, G:7};
    for (var name in cols){
      var rules = sh.getRange(2, cols[name], last-1, 1).getDataValidations(); var miss = 0;
      for (var i=0;i<rules.length;i++) if (!rules[i][0]) miss++;
      out.missing_dropdown[name] = miss;
    }
    var keys = sh.getRange(2, KEY_COL, last-1, 1).getValues();
    for (var i2=0;i2<keys.length;i2++) if (!norm(keys[i2][0])) out.blank_key_rows++;
  }
  return out;
}

// 열의 비어 있지 않은 값 목록(중복 제거, 최대 200개).
function distinctValues(sh, col, last){
  var v = sh.getRange(2, col, last-1, 1).getValues(); var seen = {}, out = [];
  for (var i=0;i<v.length && out.length<200;i++){ var x = String(v[i][0]||"").trim(); if (x && !seen[x]) { seen[x] = true; out.push(x); } }
  return out;
}
