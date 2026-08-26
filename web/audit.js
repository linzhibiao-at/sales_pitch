"use strict";
var $ = function (id) { return document.getElementById(id); };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function qs() {
  var p = new URLSearchParams();
  [["f-trace", "trace_id"], ["f-app", "app_id"], ["f-kind", "request_kind"],
   ["f-status", "status"], ["f-from", "ts_from"], ["f-to", "ts_to"],
   ["f-size", "size"]].forEach(function (pair) {
    var v = $(pair[0]).value.trim();
    if (v) p.set(pair[1], v);
  });
  return p.toString();
}

function renderRows(items) {
  var tbody = $("rows");
  tbody.innerHTML = "";
  items.forEach(function (it) {
    var tr = document.createElement("tr");
    var kindTag = '<span class="tag tag-' + (it.request_kind === "recommend" ? "recommend" : "regenerate") + '">' + esc(it.request_kind) + "</span>";
    var statusTag = '<span class="tag ' + (it.status === "ok" ? "tag-ok" : "tag-err") + '">' + esc(it.status) + "</span>";
    var inputTxt = it.outfit_id ? ("outfit=" + esc(it.outfit_id)) : ("sku=" + esc(it.input_sku_id));
    tr.innerHTML =
      "<td>" + esc(it.ts) + "</td>" +
      "<td>" + kindTag + "</td>" +
      "<td>" + statusTag + "</td>" +
      "<td>" + esc(it.trace_id) + "</td>" +
      "<td>" + esc(it.app_id) + "</td>" +
      "<td>" + inputTxt + "</td>" +
      "<td>" + esc(it.outfit_count) + "</td>" +
      "<td>" + esc(it.elapsed_ms) + "</td>";
    tr.addEventListener("click", function () { loadDetail(it.trace_id); });
    tbody.appendChild(tr);
  });
}

function search() {
  $("audit-state").textContent = "加载中…";
  fetch("/api/audit/requests?" + qs())
    .then(function (r) { return r.json(); })
    .then(function (data) {
      $("audit-state").textContent = data.enabled ? ("共 " + data.items.length + " 条") : "审计未启用";
      renderRows(data.items || []);
      $("detail").innerHTML = "";
    })
    .catch(function (e) { $("audit-state").textContent = "查询失败: " + e; });
}

function loadDetail(traceId) {
  $("detail").innerHTML = '<p class="muted">加载 ' + esc(traceId) + " …</p>";
  fetch("/api/audit/requests/" + encodeURIComponent(traceId))
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (doc) {
      $("detail").innerHTML =
        "<h3>详情 " + esc(traceId) + "</h3>" +
        "<pre>" + esc(JSON.stringify(doc, null, 2)) + "</pre>";
    })
    .catch(function (e) {
      $("detail").innerHTML = '<p class="muted">详情加载失败: ' + esc(e.message) + "</p>";
    });
}

window.addEventListener("DOMContentLoaded", function () {
  $("btn-search").addEventListener("click", search);
  $("btn-reset").addEventListener("click", function () {
    ["f-trace", "f-app", "f-kind", "f-status", "f-from", "f-to"].forEach(function (id) { $(id).value = ""; });
    $("f-size").value = "50";
    search();
  });
  search();
});
