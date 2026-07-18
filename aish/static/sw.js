/* aish service worker: exists so notifications work in installed PWAs
 * (iOS home-screen apps require showNotification via a registration) and
 * so tapping a notification focuses the app. No caching — the server is
 * local, offline HTML would just be a dead UI. */

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        for (const client of clients) {
          if ("focus" in client) return client.focus();
        }
        return self.clients.openWindow("./");
      })
  );
});
