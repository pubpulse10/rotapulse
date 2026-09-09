// The "add RotaPulse to your home screen" nudge (markup in base.html).
//
// Real report, 2026-09-09: staff at a venue couldn't get an icon onto their
// phones and were going back to their original invite email every shift to
// find the link again. Nothing was broken — the app is a valid installable
// PWA (manifest.json, sw.js, icons) — but nothing ever told them how to
// install it, and the gesture is buried and different on every platform. So
// ask on the two pages staff actually land on, at the moment they're already
// in a browser that can do it.
//
// Three states, in order of preference:
//   1. The browser handed us a real install prompt (Chrome/Edge) — show a
//      button that fires it. One tap, no instructions to misread.
//   2. No prompt, but the platform is recognisable — show that platform's
//      manual steps, including how to get out of an in-app browser, which
//      is the usual reason the option looks like it isn't there.
//   3. Anything else (desktop Safari/Firefox, unknown) — stay hidden rather
//      than give steps that don't match what's actually on screen.

(function () {
  "use strict";

  var DISMISSED_KEY = "rotapulse-install-hint-dismissed";
  // Long enough for Chrome to get through the manifest and installability
  // checks and fire beforeinstallprompt, so Android normally gets the
  // one-tap button rather than the written-out steps.
  var PROMPT_GRACE_MS = 1500;

  var hint = document.getElementById("install-hint");
  if (!hint) return; // not one of the pages that renders it

  // Already launched from the home-screen icon, so there's nothing to ask
  // for. navigator.standalone is the iOS-only spelling of the same thing.
  if (window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true) return;

  function dismissedBefore() {
    // Storage can be unavailable (private browsing, blocked cookies). Losing
    // the dismissal is a far smaller problem than the nudge throwing.
    try {
      return window.localStorage.getItem(DISMISSED_KEY) !== null;
    } catch (e) {
      return false;
    }
  }

  function rememberDismissal() {
    try {
      window.localStorage.setItem(DISMISSED_KEY, "1");
    } catch (e) {}
  }

  if (dismissedBefore()) return;

  function show(id) {
    document.getElementById(id).hidden = false;
    hint.hidden = false;
  }

  function hide() {
    hint.hidden = true;
  }

  document.getElementById("install-hint-dismiss").addEventListener("click", function () {
    hide();
    rememberDismissal();
  });

  // Installed while we were sitting here: the browser tab this ran in is
  // still not standalone, so without this the nudge would keep asking for
  // something they've already done.
  window.addEventListener("appinstalled", function () {
    hide();
    rememberDismissal();
  });

  var prompted = false;

  window.addEventListener("beforeinstallprompt", function (event) {
    // Suppress Chrome's own mini-infobar so there's one nudge, not two.
    event.preventDefault();
    prompted = true;
    document.getElementById("install-hint-button").addEventListener("click", function () {
      hide();
      event.prompt(); // must happen inside the click, not before it
    });
    show("install-hint-prompt");
  });

  var ua = navigator.userAgent;
  // iPadOS 13+ reports a Macintosh user agent; the touch-point count is what
  // separates a real iPad from an actual Mac.
  var isIOS = /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);

  if (isIOS) {
    // Safari implements no install prompt at all and never will, so there's
    // nothing to wait for — show the steps straight away rather than move
    // the page about a second and a half in.
    show("install-hint-ios");
  } else if (/Android/.test(ua)) {
    window.setTimeout(function () {
      if (!prompted) show("install-hint-android");
    }, PROMPT_GRACE_MS);
  }
})();
