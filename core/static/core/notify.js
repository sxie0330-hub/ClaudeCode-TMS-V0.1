// Bell menu, pop-ups that fade after a while, a check for new notices every minute,
// and (if the person allows it) a desktop notification for each new one.
(function () {
  var btn = document.getElementById("bell-btn"), menu = document.getElementById("bell-menu"),
      count = document.getElementById("bell-n"), box = document.getElementById("toasts");
  if (btn && menu) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      btn.setAttribute("aria-expanded", String(!menu.hidden));
    });
    document.addEventListener("click", function (e) { if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true; });
  }
  function fade(el) { setTimeout(function () { el.style.transition = "opacity .5s"; el.style.opacity = "0"; setTimeout(function () { el.remove(); }, 600); }, 12000); }
  if (box) Array.prototype.forEach.call(box.children, fade);
  function toast(n) {
    var a = document.createElement("a");
    a.className = "toast"; a.href = n.url; a.textContent = n.text;
    box.appendChild(a); fade(a);
    if ("Notification" in window && Notification.permission === "granted") {
      try { var d = new Notification("TMS", { body: n.text, tag: "tms-" + n.id }); d.onclick = function () { window.focus(); location.href = n.url; }; } catch (e) { /* ignore */ }
    }
  }
  function poll() {
    if (document.hidden && !("Notification" in window && Notification.permission === "granted")) return;
    fetch(window.TMS_POLL, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (count) { count.textContent = d.unread; count.hidden = !d.unread; }
        d.new.forEach(toast);
      }).catch(function () {});
  }
  setInterval(poll, 60000);
  // "Turn on desktop notifications" button on the notifications page.
  var desk = document.getElementById("desk-btn");
  if (desk && "Notification" in window && Notification.permission === "default") {
    desk.hidden = false;
    desk.addEventListener("click", function () { Notification.requestPermission().then(function () { desk.hidden = true; }); });
  }
})();
