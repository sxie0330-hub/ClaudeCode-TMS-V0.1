// 报告问题: remembers the last script errors on this page and sends them with the report.
(function () {
  var KEY = "tms_js_errors";
  function remember(msg) {
    try {
      var list = JSON.parse(sessionStorage.getItem(KEY) || "[]");
      list.push(new Date().toISOString().slice(11, 19) + " " + location.pathname + " " + String(msg).slice(0, 300));
      sessionStorage.setItem(KEY, JSON.stringify(list.slice(-5)));
    } catch (e) { /* storage blocked: nothing to remember */ }
  }
  window.addEventListener("error", function (e) { remember((e.message || "error") + (e.filename ? " @" + e.filename + ":" + e.lineno : "")); });
  window.addEventListener("unhandledrejection", function (e) { remember("promise: " + (e.reason && e.reason.message || e.reason)); });

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("fb-btn"), dlg = document.getElementById("fb-dlg");
    if (!btn || !dlg) return;
    var form = dlg.querySelector("form"), msg = dlg.querySelector(".fb-msg");
    btn.addEventListener("click", function () { msg.textContent = ""; form.reset(); dlg.showModal(); });
    dlg.querySelector(".fb-cancel").addEventListener("click", function () { dlg.close(); });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var data = new FormData(form), errs = "";
      try { errs = (JSON.parse(sessionStorage.getItem(KEY) || "[]")).join("\n"); } catch (e) {}
      data.set("url", location.href); data.set("title", document.title); data.set("js_errors", errs);
      var send = form.querySelector("button[type=submit]");
      send.disabled = true;
      fetch(form.action, { method: "POST", body: data, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          send.disabled = false;
          if (j.ok) {
            try { sessionStorage.removeItem(KEY); } catch (e) {}
            msg.textContent = form.dataset.done.replace("{}", j.id);
            setTimeout(function () { dlg.close(); }, 1500);
          } else { msg.textContent = j.error || form.dataset.fail; }
        })
        .catch(function () { send.disabled = false; msg.textContent = form.dataset.fail; });
    });
  });
})();
