// Long drop-downs (customers, addresses, carriers…) get a small search box:
// type a few letters of the code or name and the list narrows to matches.
(function () {
  var MIN_OPTIONS = 12;
  function enhance(sel) {
    if (sel.multiple || sel.dataset.search === "off" || sel.options.length < MIN_OPTIONS) return;
    var box = document.createElement("input");
    box.type = "search";
    box.className = "select-search";
    box.placeholder = "输入几个字搜索…";
    box.setAttribute("aria-label", "搜索选项");
    sel.parentNode.insertBefore(box, sel);
    var opts = Array.prototype.slice.call(sel.options);
    box.addEventListener("input", function () {
      var q = box.value.trim().toLowerCase(), first = null;
      opts.forEach(function (o) {
        var hit = !q || o.text.toLowerCase().indexOf(q) !== -1 || o.value === "";
        o.hidden = !hit;
        if (hit && q && o.value !== "" && !first) first = o;
      });
      if (first) { sel.value = first.value; sel.dispatchEvent(new Event("change", { bubbles: true })); }
    });
    box.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
  }
  document.querySelectorAll("form.form select").forEach(enhance);
})();
