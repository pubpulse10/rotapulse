// Drag-and-drop for the rota grid: pick up a shift chip and drop it onto a
// different person/day cell to reassign it in one move. Uses the Pointer
// Events API deliberately (not the HTML5 Drag and Drop API) — Pointer
// Events unify mouse and touch in one code path, which matters here since
// this grid is used on tablets, and native HTML5 drag-and-drop has poor/
// inconsistent touch support. No new dependency, consistent with the rest
// of the app's vanilla-JS, no-build-step approach.
//
// A chip is a plain <span data-href="...">, not a real <a> — a genuine
// <a href> under a sustained hold triggers the browser's own native link
// menu (iOS Safari's callout, or Chrome for iOS's own "Open in new tab"
// popup), which pops up over the cell being dropped onto and can't be
// suppressed from page JS/CSS at all, since it's rendered by the browser
// chrome above the page before any touch event reaches this script. A
// short-movement tap navigates via data-href directly instead (see
// onPointerUp) — only a genuine drag is treated as a move.
//
// Touch/pen requires a deliberate hold (LONG_PRESS_MS) before a drag arms.
// Chips use touch-action: none (see style.css) so the browser never takes
// over the gesture natively — a chip is the one thing on this page you
// might deliberately hold still on, and once a real WebKit pan gesture has
// started for a touch, a preventDefault() arriving after our hold delay is
// too late to cancel it (this was tried with touch-action: pan-x pan-y
// instead and confirmed live not to work: the page kept scrolling under a
// held/dragged finger instead of the drag taking over). So instead, any
// movement past LONG_PRESS_CANCEL_PX before the hold completes is treated
// as "this was actually a scroll" and onPointerMove manually forwards that
// movement to the relevant scroll containers itself (the .panel horizontal
// scroller and the page's own vertical scroll) for the rest of the
// gesture, since native scrolling is never available on this element at
// all. Mouse has no such ambiguity with scrolling, so it keeps the
// original small-distance-threshold, no-hold-needed feel.
(function () {
  const DRAG_THRESHOLD_PX = 8;
  const LONG_PRESS_MS = 350;
  const LONG_PRESS_CANCEL_PX = 10;
  // How close to a viewport edge (px) auto-scroll kicks in, and the fastest
  // it'll scroll right at the very edge — see edgeScrollTick below.
  const EDGE_SCROLL_MARGIN = 70;
  const EDGE_SCROLL_MAX_SPEED = 16;

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  let drag = null;
  let edgeScrollRAF = null;

  // Auto-scrolls the page (and the grid's own horizontal scroller) while an
  // armed drag's pointer sits near a viewport edge — real report,
  // 2026-08-19: on a grid taller than the screen, a shift couldn't be
  // dragged past the top of the viewport at all; you had to drop it,
  // scroll, and pick it up again. A plain pointermove-driven scroll only
  // fires on actual movement, so holding the pointer still right at the
  // edge (the natural thing to do — you're waiting for the page to catch
  // up to you) wouldn't scroll at all; a requestAnimationFrame loop keeps
  // scrolling every frame for as long as the pointer stays in the margin,
  // moving or not, and re-checks the drop target each frame too so the
  // highlighted cell stays correct as the page moves under a still pointer.
  function edgeScrollTick() {
    if (!drag || !drag.moved) { edgeScrollRAF = null; return; }
    const x = drag.lastX, y = drag.lastY;
    const vh = window.innerHeight, vw = window.innerWidth;
    let scrolled = false;

    const scroller = document.scrollingElement || document.documentElement;
    if (y < EDGE_SCROLL_MARGIN) {
      scroller.scrollTop -= EDGE_SCROLL_MAX_SPEED * (1 - y / EDGE_SCROLL_MARGIN);
      scrolled = true;
    } else if (y > vh - EDGE_SCROLL_MARGIN) {
      scroller.scrollTop += EDGE_SCROLL_MAX_SPEED * (1 - (vh - y) / EDGE_SCROLL_MARGIN);
      scrolled = true;
    }

    if (drag.scrollContainer) {
      if (x < EDGE_SCROLL_MARGIN) {
        drag.scrollContainer.scrollLeft -= EDGE_SCROLL_MAX_SPEED * (1 - x / EDGE_SCROLL_MARGIN);
        scrolled = true;
      } else if (x > vw - EDGE_SCROLL_MARGIN) {
        drag.scrollContainer.scrollLeft += EDGE_SCROLL_MAX_SPEED * (1 - (vw - x) / EDGE_SCROLL_MARGIN);
        scrolled = true;
      }
    }

    if (scrolled) updateDragVisuals(x, y);
    edgeScrollRAF = requestAnimationFrame(edgeScrollTick);
  }

  // Moves the ghost to the pointer and (re)highlights whatever .rota-cell
  // is under it — shared by onPointerMove and the edge-scroll loop above,
  // since the loop needs to redo this on every frame even without a fresh
  // pointermove event.
  function updateDragVisuals(x, y) {
    drag.ghost.style.left = x + "px";
    drag.ghost.style.top = y + "px";

    document.querySelectorAll(".rota-cell.drop-target").forEach((el) => el.classList.remove("drop-target"));
    drag.ghost.style.display = "none";
    const under = document.elementFromPoint(x, y);
    drag.ghost.style.display = "";
    const targetCell = under && under.closest(".rota-cell");
    if (targetCell) targetCell.classList.add("drop-target");
  }

  function armDrag() {
    if (!drag || drag.moved) return;
    drag.moved = true;
    // Guarded the same way teardownDrag's releasePointerCapture already is
    // — setPointerCapture can throw (e.g. no genuinely active pointer
    // session for this id, which is also why this whole function can't be
    // exercised by a synthetic/dispatched PointerEvent in a test). A
    // real gesture that hits this is rare, but letting it throw here would
    // abort the rest of armDrag() unhandled — no ghost, no edge-scroll,
    // stuck with moved=true and nothing to show for it.
    try { drag.chip.setPointerCapture(drag.pointerId); } catch (err) {}
    drag.chip.classList.add("dragging-source");

    const ghost = drag.chip.cloneNode(true);
    ghost.classList.add("shift-chip-ghost");
    ghost.style.left = drag.lastX + "px";
    ghost.style.top = drag.lastY + "px";
    document.body.appendChild(ghost);
    drag.ghost = ghost;

    if (edgeScrollRAF === null) edgeScrollRAF = requestAnimationFrame(edgeScrollTick);
  }

  // Tears down whatever state a drag left behind, including releasing
  // pointer capture. Needed both for normal cleanup and for the defensive
  // call in onPointerDown below — if the OS ever swallows a gesture
  // mid-drag (e.g. an edge-swipe-back gesture cancels it without ever
  // delivering pointerup/pointercancel to the page), the captured chip
  // would otherwise keep claiming pointer events forever, and on some
  // browsers a later touch can be assigned that same now-stale pointerId,
  // silently swallowing an unrelated tap elsewhere on the page (a plain
  // click() or dispatched event doesn't go through real pointer capture,
  // which is why this class of bug doesn't show up under synthetic testing
  // — only a real, interrupted touch gesture triggers it).
  function teardownDrag() {
    if (!drag) return;
    if (drag.longPressTimer) clearTimeout(drag.longPressTimer);
    if (drag.moved) {
      try { drag.chip.releasePointerCapture(drag.pointerId); } catch (err) {}
    }
    if (drag.ghost) drag.ghost.remove();
    drag.chip.classList.remove("dragging-source");
    document.querySelectorAll(".rota-cell.drop-target").forEach((el) => el.classList.remove("drop-target"));
    if (edgeScrollRAF !== null) { cancelAnimationFrame(edgeScrollRAF); edgeScrollRAF = null; }
    drag = null;
  }

  function onPointerDown(e) {
    // A different pointerId arriving while `drag` is still set proves the
    // previous gesture is over one way or another (browsers don't reuse an
    // active pointerId for a second simultaneous touch) — clean it up now
    // rather than risk it lingering indefinitely.
    if (drag && drag.pointerId !== e.pointerId) teardownDrag();

    const chip = e.target.closest(".shift-chip");
    if (!chip || e.button === 2) return;
    drag = {
      chip,
      pointerId: e.pointerId,
      pointerType: e.pointerType,
      startX: e.clientX,
      startY: e.clientY,
      lastX: e.clientX,
      lastY: e.clientY,
      moved: false,
      scrolling: false,
      scrollContainer: chip.closest(".panel"), // horizontal scroller for the grid; vertical uses the page itself
      ghost: null,
      longPressTimer: null,
    };

    if (e.pointerType === "touch" || e.pointerType === "pen") {
      const pointerId = e.pointerId;
      drag.longPressTimer = setTimeout(() => {
        if (drag && drag.pointerId === pointerId) armDrag();
      }, LONG_PRESS_MS);
    }
  }

  function onPointerMove(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const prevX = drag.lastX, prevY = drag.lastY;
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;

    if (drag.scrolling) {
      // Native scroll is never available here (touch-action: none), so
      // this gesture has to keep driving the scroll containers by hand for
      // as long as the finger is down.
      e.preventDefault();
      if (drag.scrollContainer) drag.scrollContainer.scrollLeft -= (e.clientX - prevX);
      (document.scrollingElement || document.documentElement).scrollTop -= (e.clientY - prevY);
      return;
    }

    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    const dist = Math.hypot(dx, dy);

    if (!drag.moved) {
      if (drag.pointerType === "mouse") {
        if (dist > DRAG_THRESHOLD_PX) armDrag();
      } else if (dist > LONG_PRESS_CANCEL_PX) {
        // Moved before the hold completed — this was a scroll attempt, not
        // a pick-up. Switch to manually forwarding movement to the scroll
        // containers for the rest of this gesture (see file header).
        clearTimeout(drag.longPressTimer);
        drag.scrolling = true;
        e.preventDefault();
        if (drag.scrollContainer) drag.scrollContainer.scrollLeft -= (e.clientX - prevX);
        (document.scrollingElement || document.documentElement).scrollTop -= (e.clientY - prevY);
        return;
      }
    }

    if (drag.moved) {
      e.preventDefault();
      updateDragVisuals(e.clientX, e.clientY);
    }
  }

  function onPointerUp(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const { chip, moved, ghost } = drag;

    if (moved) {
      ghost && (ghost.style.display = "none");
      const under = document.elementFromPoint(e.clientX, e.clientY);
      const targetCell = under && under.closest(".rota-cell");
      if (targetCell) {
        moveShift(chip.dataset.shiftId, targetCell.dataset.personId, targetCell.dataset.date);
      }
    } else if (!drag.scrolling && chip.dataset.href) {
      // A genuine short, stationary tap — not a drag, and not a swipe that
      // turned into manual scrolling either. Chips aren't real <a>
      // elements (see the file header comment for why), so navigation on a
      // plain tap has to happen explicitly here instead of falling through
      // to a native link click.
      location.href = chip.dataset.href;
    }
    teardownDrag();
  }

  // Chips are focusable (tabindex="0" in the template) but aren't real
  // links, so Enter/Space needs to trigger navigation explicitly to keep
  // keyboard access working. teardownDrag() first: a pointer drag can be
  // mid-flight on this same chip (armed, ghost visible, pointer captured)
  // without ever having received its pointerup/pointercancel — navigating
  // away via location.href here would otherwise leave that ghost element
  // (and the "dragging-source" faded style) stuck in the DOM.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const chip = e.target.closest(".shift-chip");
    if (!chip || !chip.dataset.href) return;
    e.preventDefault();
    teardownDrag();
    location.href = chip.dataset.href;
  });

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
  // in the markup/CSS: a shift chip's avatar <img> is natively draggable in
  // most browsers by default. If the browser's own drag-and-drop ever
  // engages instead of this pointer-based one, it hijacks the gesture —
  // you'd see the ghost start following, then never get a real drop,
  // because the OS-level drag swallows the actual release. This stops that
  // at the source.
  document.addEventListener("dragstart", (e) => {
    if (e.target.closest(".shift-chip")) e.preventDefault();
  });

  // Android's long-press context menu ("Open link in new tab" / "Copy link
  // address") fires around the same hold duration as our own drag-arm
  // timer, and pops up right over the cell you're trying to drop onto if
  // left unsuppressed. iOS Safari's equivalent is blocked via
  // -webkit-touch-callout: none on .shift-chip in style.css instead, since
  // this event doesn't fire there.
  document.addEventListener("contextmenu", (e) => {
    if (e.target.closest(".shift-chip")) e.preventDefault();
  });

  document.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("pointermove", onPointerMove);
  document.addEventListener("pointerup", onPointerUp);
  document.addEventListener("pointercancel", onPointerUp);

  // Real report, 2026-08-19: tapping an open shift to view its details and
  // going back left the shift stuck "grabbed" — no amount of clicking would
  // release it. teardownDrag() only ran from pointerup/pointercancel, both
  // of which assume the gesture ends cleanly on THIS page; a tap that's
  // fractionally longer than it feels (very easy on a real touchscreen) can
  // arm a drag before the finger lifts, and if the resulting navigation
  // (or the browser swallowing the gesture some other way — see the
  // teardownDrag comment above) happens before pointerup is ever delivered,
  // nothing before this point could have known to clean up. visibilitychange
  // and pagehide both fire reliably as a page is being left, regardless of
  // whether the pointer gesture itself ever completes, so tearing down here
  // closes that gap at the source rather than only mopping up after it.
  document.addEventListener("visibilitychange", () => { if (document.hidden) teardownDrag(); });
  window.addEventListener("pagehide", () => teardownDrag());

  // Belt-and-suspenders against a stuck ghost surviving into a view of this
  // page it doesn't belong to. The listeners above should mean nothing is
  // left behind in the first place, but this is a backstop for whatever
  // they don't catch (e.g. the back/forward cache restoring a page from a
  // moment mid-drag on a browser where that's eligible). Sweeping once at
  // script load handles a fresh page load; pageshow with event.persisted is
  // the standard signal for a bfcache restore, which doesn't re-run this
  // script at all, so needs its own sweep.
  function sweepStaleGhosts() {
    document.querySelectorAll(".shift-chip-ghost").forEach((el) => el.remove());
    document.querySelectorAll(".shift-chip.dragging-source").forEach((el) => el.classList.remove("dragging-source"));
  }
  sweepStaleGhosts();
  window.addEventListener("pageshow", (e) => { if (e.persisted) sweepStaleGhosts(); });
})();
