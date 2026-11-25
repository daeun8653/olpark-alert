importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js");

firebase.initializeApp({
  apiKey: "AIzaSyC8gODQTFOA2aXHwbub7CQY9IALk4q5KJQ",
  authDomain: "olpark-alert.firebaseapp.com",
  projectId: "olpark-alert",
  storageBucket: "olpark-alert.firebasestorage.app",
  messagingSenderId: "238172182457",
  appId: "1:238172182457:web:e1b6a3eee421d154248868"
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage(payload => {
  const d = payload.data || {};
  self.registration.showNotification(d.title || "올림픽공원 공연 알림", { body: d.body || "" });
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(clients.openWindow("./"));
});