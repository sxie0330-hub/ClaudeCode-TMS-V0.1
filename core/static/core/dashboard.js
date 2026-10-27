// Widget board: drag to reorder, size buttons, remove, add. Saves layout as JSON.
(function () {
  var board = document.getElementById("board");
  if (!board) return;
  var SPANS = {S: [1, 1], M: [2, 1], L: [2, 2], W: [4, 1], XL: [4, 2]};
  var catalog = JSON.parse(document.getElementById("catalog-data").textContent);
  var csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  var editing = false, dragged = null, dirty = false;

  function layout() {
    return Array.prototype.map.call(board.querySelectorAll(".wg"), function (el) {
      return {id: el.dataset.id, type: el.dataset.type, size: el.dataset.size};
    });
  }
  function save(reload) {
    return fetch(board.dataset.save, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": decodeURIComponent(csrf)},
      body: JSON.stringify(layout())
    }).then(function () { dirty = false; if (reload) location.reload(); });
  }
  function setSize(el, s) {
    el.dataset.size = s;
    el.style.gridColumn = "span " + SPANS[s][0];
    el.style.gridRow = "span " + SPANS[s][1];
    el.querySelectorAll(".wg-s").forEach(function (b) { b.classList.toggle("on", b.dataset.s === s); });
    dirty = true;
  }
  function setEditing(on) {
    editing = on;
    board.classList.toggle("editing", on);
    document.getElementById("view-actions").hidden = on;
    document.getElementById("edit-actions").hidden = !on;
    board.querySelectorAll(".wg").forEach(function (el) { el.draggable = on; });
  }

  document.getElementById("edit-on").addEventListener("click", function () { setEditing(true); });
  document.getElementById("edit-done").addEventListener("click", function () {
    setEditing(false);
    if (dirty) save(true);
  });

  board.addEventListener("click", function (e) {
    if (!editing) return;
    var el = e.target.closest(".wg");
    if (!el) return;
    if (e.target.classList.contains("wg-x")) { el.remove(); dirty = true; }
    else if (e.target.classList.contains("wg-s")) { setSize(el, e.target.dataset.s); }
  });

  board.addEventListener("dragstart", function (e) {
    if (!editing) return;
    dragged = e.target.closest(".wg");
    if (dragged) { dragged.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; }
  });
  board.addEventListener("dragend", function () { if (dragged) dragged.classList.remove("dragging"); dragged = null; });
  board.addEventListener("dragover", function (e) {
    if (!editing || !dragged) return;
    e.preventDefault();
    var over = e.target.closest(".wg");
    if (!over || over === dragged) return;
    var r = over.getBoundingClientRect();
    var after = (e.clientY - r.top) / r.height > 0.5 || (e.clientX - r.left) / r.width > 0.6;
    board.insertBefore(dragged, after ? over.nextSibling : over);
    dirty = true;
  });

  var dlg = document.getElementById("add-dlg");
  document.getElementById("add-open").addEventListener("click", function () { dlg.showModal(); });
  document.getElementById("add-close").addEventListener("click", function () { dlg.close(); });
  dlg.addEventListener("click", function (e) {
    var item = e.target.closest(".cat-item");
    if (!item) return;
    var type = item.dataset.type, meta = catalog[type];
    var l = layout();
    l.unshift({id: Math.random().toString(16).slice(2, 10), type: type, size: meta["default"]});
    fetch(board.dataset.save, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": decodeURIComponent(csrf)},
      body: JSON.stringify(l)
    }).then(function () { location.reload(); });
  });
  window.addEventListener("beforeunload", function (e) { if (editing && dirty) { e.preventDefault(); e.returnValue = ""; } });
})();
