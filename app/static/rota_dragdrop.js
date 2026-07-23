// Drag-and-drop for the rota grid: pick up a shift chip and drop it onto a
// different person/day cell to reassign it in one move. Uses the Pointer
// Events API deliberately (not the HTML5 Drag and Drop API) — Pointer
// Events unify mouse and touch in one code path, which matters here since
// this grid is used on tablets, and native HTML5 drag-and-drop has poor/
// inconsistent touch support. No new dependency, consistent with the rest
// of the app's vanilla-JS, no-build-step approach.
//
// A short-movement tap still falls through to the cell's own <a> link
// (open the edit panel) — only a genuine drag (movement past a small
// threshold) is treated as a move and suppresses that click.
(function () {
  const DRAG_THRESHOLD_PX = 8;

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  let drag = null;

  function suppressNextClick(el) {
    const handler = (e) => {
      e.preventDefault();
      e.stopPropagation();
    };
    el.addEventListener("click", handler, { capture: true, once: true });
  }

  function onPointerDown(e) {
    const chip = e.target.closest(".shift-chip");
    if (!chip || e.button === 2) return;
    drag = {
      chip,
      link: chip.closest("a"),
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      moved: false,
      ghost: null,
    };
  }

  function onPointerMove(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;

    if (!drag.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD_PX) {
      drag.moved = true;
      drag.chip.setPointerCapture(drag.pointerId);
      if (drag.link) suppressNextClick(drag.link);
      drag.chip.classList.add("dragging-source");

      const ghost = drag.chip.cloneNode(true);
      ghost.classList.add("shift-chip-ghost");
      document.body.appendChild(ghost);
      drag.ghost = ghost;
    }

    if (drag.moved) {
      e.preventDefault(); // stop touch-scroll from fighting the drag
      drag.ghost.style.left = e.clientX + "px";
      drag.ghost.style.top = e.clientY + "px";

      document.querySelectorAll(".rota-cell.drop-target").forEach((el) => el.classList.remove("drop-target"));
      drag.ghost.style.display = "none";
      const under = document.elementFromPoint(e.clientX, e.clientY);
      drag.ghost.style.display = "";
      const targetCell = under && under.closest(".rota-cell");
      if (targetCell) targetCell.classList.add("drop-target");
    }
  }

  function onPointerUp(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const { chip, moved, ghost } = drag;

    if (ghost) ghost.remove();
    chip.classList.remove("dragging-source");
    document.querySelectorAll(".rota-cell.drop-target").forEach((el) => el.classList.remove("drop-target"));

    if (moved) {
      ghost && (ghost.style.display = "none");
      const under = document.elementFromPoint(e.clientX, e.clientY);
      const targetCell = under && under.closest(".rota-cell");
      if (targetCell) {
        moveShift(chip.dataset.shiftId, targetCell.dataset.personId, targetCell.dataset.date);
      }
    }
    drag = null;
  }

  function moveShift(shiftId, personId, shiftDate) {
    fetch(`shift/${shiftId}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({ person_id: personId, shift_date: shiftDate }),
    })
      .then((resp) => resp.json().then((data) => ({ ok: resp.ok, data })))
      .then(({ ok, data }) => {
        if (ok) {
          location.reload();
        } else {
          alert(data.error || "Couldn't move that shift.");
        }
      })
      .catch(() => alert("Couldn't move that shift — check your connection and try again."));
  }

  // Belt-and-suspenders on top of draggable="false"/-webkit-user-drag:none
  // in the markup/CSS: a shift chip sits inside a link (and sometimes an
  // avatar <img>), both natively draggable in most browsers. If the
  // browser's own drag-and-drop ever engages instead of this pointer-based
  // one, it hijacks the gesture — you'd see the ghost start following, then
  // never get a real drop, because the OS-level drag swallows the actual
  // release. This stops that at the source.
  document.addEventListener("dragstart", (e) => {
    if (e.target.closest(".shift-chip")) e.preventDefault();
  });

  document.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("pointermove", onPointerMove);
  document.addEventListener("pointerup", onPointerUp);
  document.addEventListener("pointercancel", onPointerUp);
})();
