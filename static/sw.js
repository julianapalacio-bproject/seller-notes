// Service worker de Seller Notes: solo sirve para poder instalar el panel como app.
// No intercepta ninguna página: el panel pide contraseña (autenticación básica) y, si el service worker
// se pone en medio, el móvil no muestra la petición de contraseña y la página se queda en blanco.
const CACHE = 'seller-notes-v2';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.map((k) => caches.delete(k))))  // borra la caché de la versión anterior
      .then(() => self.clients.claim())
  );
});
