// Loaded only for a family-admin support session, which may open every admin
// screen but cannot save anything (app/rota_auth.py::require_permission).
//
// The server is the guarantee: any non-GET is refused with a 403. This is
// about not offering the control in the first place. Reported live on
// 2026-09-17 -- a venue setting was ticked, Save was pressed, the save was
// refused, and the only sign was an unexplained "Forbidden" page. Somebody who
// misses that page believes the change went through, which for a setting that
// governs what staff are told is worse than an error.
//
// Swept generically rather than guarded template by template: there are dozens
// of forms across the admin screens and any one missed would put the old
// behaviour back.
(function () {
  "use strict";

  function lockDown() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      var form = forms[i];
      // GET forms are how you FILTER things -- the leave report's date range,
      // the staff picker. Those must keep working; looking is the whole point
      // of a support session.
      var method = (form.getAttribute("method") || "get").toLowerCase();
      if (method !== "post") continue;

      form.addEventListener("submit", function (event) {
        event.preventDefault();
      });

      var controls = form.querySelectorAll("button, input[type=submit]");
      for (var j = 0; j < controls.length; j++) {
        controls[j].disabled = true;
        controls[j].title = "Read-only while signed in as support";
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", lockDown);
  } else {
    lockDown();
  }
})();
