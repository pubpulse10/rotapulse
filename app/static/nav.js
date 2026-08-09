(function () {
  var toggles = document.querySelectorAll(".nav-toggle");

  toggles.forEach(function (toggle) {
    var nav = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!nav) return;

    function close() {
      nav.classList.remove("nav-open");
      toggle.setAttribute("aria-expanded", "false");
    }

    function toggleOpen() {
      var isOpen = nav.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    }

    toggle.addEventListener("click", function (event) {
      event.stopPropagation();
      toggleOpen();
    });

    document.addEventListener("click", function (event) {
      if (nav.classList.contains("nav-open") && !nav.contains(event.target) && event.target !== toggle) {
        close();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") close();
    });
  });
})();
