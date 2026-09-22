// V2R 시트 쓰기 웹앱 (Apps Script). POST JSON:
// {"spreadsheet_id":"...", "sheet":"노출 현황", "action":"append"|"update"|"set_cells"|"read",
//  "rows":[[...],...], "start_row":2, "cells":[["P1",123],["Q1",45]], "match_col":"H", "updates":{"키워드":{"G":"노출완","J":"2026-..."}}}
function doPost(e) {
  var body = JSON.parse(e.postData.contents || "{}");
  var ss = SpreadsheetApp.openById(body.spreadsheet_id);
  var sh = ss.getSheetByName(body.sheet) || ss.getSheets()[1];
  var res = {ok:true, sheet: sh.getName()};
  try {
    if (body.action === "append") {
      var rows = body.rows || [];
      if (rows.length) sh.getRange(sh.getLastRow()+1, 1, rows.length, rows[0].length).setValues(rows);
      res.appended = rows.length;
    } else if (body.action === "update") {
      var r = body.start_row || 2, rows2 = body.rows || [];
      if (rows2.length) sh.getRange(r, 1, rows2.length, rows2[0].length).setValues(rows2);
      res.updated = rows2.length;
    } else if (body.action === "set_cells") {
      (body.cells || []).forEach(function(c){ sh.getRange(c[0]).setValue(c[1]); });
      res.cells = (body.cells||[]).length;
    } else if (body.action === "update_by_key") {
      var col = colIndex(body.match_col || "H"), last = sh.getLastRow();
      var keys = sh.getRange(2, col, Math.max(last-1,1), 1).getValues().map(function(v){return String(v[0]).trim();});
      var n = 0, ups = body.updates || {};
      for (var k in ups) { var i = keys.indexOf(k); if (i < 0) continue;
        for (var c2 in ups[k]) { sh.getRange(i+2, colIndex(c2)).setValue(ups[k][c2]); n++; } }
      res.cells = n;
    } else if (body.action === "read") {
      res.values = sh.getRange(body.range || ("A1:O" + sh.getLastRow())).getValues();
    } else res = {ok:false, error:"unknown action"};
  } catch (err) { res = {ok:false, error:String(err)}; }
  return out(res);
}
function doGet() { return out({ok:true, ping:"v2r-sheets"}); }
function colIndex(a) { var n=0; a=a.toUpperCase(); for (var i=0;i<a.length;i++) n=n*26+(a.charCodeAt(i)-64); return n; }
function out(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
