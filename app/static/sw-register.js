if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Registration failing shouldn't be user-visible — the app works
      // fully without a service worker, it just loses "Add to Home Screen".
    });
  });
}
