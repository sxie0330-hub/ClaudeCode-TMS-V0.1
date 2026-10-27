// Gmail-style suggestions for the global search box. ~2 KB, no libraries.
(function () {
  var input = document.getElementById("gq");
  if (!input) return;
  var box = document.getElementById("sugg"), url = input.dataset.suggest, timer = null, items = [], active = -1, last = "";

  function esc(s) { return String(s || "").replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function hi(text, q) {
    var t = esc(text), words = q.split(/\s+/).filter(function (w) { return w && w.indexOf(":") < 0 && w.indexOf("：") < 0; });
    words.forEach(function (w) {
      var i = t.toLowerCase().indexOf(esc(w).toLowerCase());
      if (i >= 0) t = t.slice(0, i) + "<mark>" + t.slice(i, i + esc(w).length) + "</mark>" + t.slice(i + esc(w).length);
    });
    return t;
  }
  function close() { box.hidden = true; active = -1; }
  function select(i) {
    items.forEach(function (el, j) { el.classList.toggle("on", j === i); });
    active = i;
  }
  function render(data, q) {
    var html = "";
    (data.groups || []).forEach(function (g) {
      html += '<div class="sg">' + esc(g.label) + "</div>";
      g.items.forEach(function (it) {
        html += '<a class="si" href="' + esc(it.url) + '"><span class="st">' + esc(it.tag) + "</span><b>" + hi(it.title, q) + "</b><small>" + hi(it.sub, q) + "</small></a>";
      });
    });
    if (!html) html = '<div class="sg">没有直接匹配，按回车查看完整搜索结果</div>';
    html += '<div class="sf"><a href="' + esc(data.all_url || "#") + '">查看全部结果 ↵</a><span>筛选：客户:Hithium 承运商:Harris 状态:在途 POD:未收 公司:PTT</span></div>';
    box.innerHTML = html;
    items = Array.prototype.slice.call(box.querySelectorAll(".si"));
    box.hidden = false;
    active = -1;
  }
  input.addEventListener("input", function () {
    var q = input.value.trim();
    clearTimeout(timer);
    if (q.length < 2) { close(); return; }
    timer = setTimeout(function () {
      last = q;
      fetch(url + "?q=" + encodeURIComponent(q), {credentials: "same-origin"})
        .then(function (r) { return r.json(); })
        .then(function (d) { if (input.value.trim() === last) render(d, q); })
        .catch(function () {});
    }, 150);
  });
  input.addEventListener("keydown", function (e) {
    if (box.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); select(Math.min(active + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); select(Math.max(active - 1, 0)); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); location.href = items[active].href; }
    else if (e.key === "Escape") { close(); }
  });
  document.addEventListener("click", function (e) { if (!input.form.contains(e.target)) close(); });
  document.addEventListener("keydown", function (e) {
    var tag = (document.activeElement || {}).tagName;
    if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") { e.preventDefault(); input.focus(); input.select(); }
  });
})();
