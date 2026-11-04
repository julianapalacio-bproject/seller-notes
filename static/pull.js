// Deslizar hacia abajo para actualizar (como en las apps de Apple).
// Arriba del todo, arrastra hacia abajo con el dedo (o con dos dedos en el trackpad) y suelta cuando el círculo se complete:
// en Listings y Profit pide los datos nuevos a Amazon (el mismo botón «Actualizar» de la página); en las demás recarga la página.
(() => {
  const UMBRAL = 72;          // px de recorrido visible del círculo para que se active
  const MAX = 160;            // el círculo no baja más de esto (hace falta tirar unos 240 px)
  const ind = document.createElement('div');
  ind.className = 'ptr';
  ind.setAttribute('aria-hidden', 'true');
  ind.innerHTML = '<svg viewBox="0 0 24 24"><circle class="ptr-track" cx="12" cy="12" r="9"/><circle class="ptr-arc" cx="12" cy="12" r="9" pathLength="100"/>'
    + '<path class="ptr-arrow" d="M12 8v8M8.5 12.5 12 16l3.5-3.5"/></svg>';
  const voz = document.createElement('p');
  voz.className = 'ptr-voz';
  voz.setAttribute('aria-live', 'polite');
  document.body.append(ind, voz);
  const quieto = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let y = 0, ocupado = false;

  // Resistencia progresiva: cuanto más tiras, menos sigue el círculo (rubber-band)
  const goma = (d) => (d * MAX * 0.55) / (MAX + 0.55 * d);
  const pinta = (v, conTransicion) => {
    y = v;
    ind.classList.toggle('suelta', conTransicion);
    const p = Math.min(v / UMBRAL, 1);
    ind.style.transform = `translate(-50%, ${v - 48}px) rotate(${p * 180}deg)`;
    ind.style.opacity = Math.min(v / 24, 1);
    ind.style.setProperty('--p', (p * 100).toFixed(1));
    ind.classList.toggle('listo', p >= 1);
  };
  const vuelve = () => pinta(0, true);

  const actualiza = () => {
    ocupado = true;
    pinta(UMBRAL, true);
    ind.classList.add('gira');
    const form = [...document.querySelectorAll('main form[action$="/actualizar"]')]
      .find((f) => !f.querySelector('button[type=submit]')?.disabled);
    voz.textContent = form ? 'Pidiendo los datos nuevos a Amazon…' : 'Actualizando la página…';
    setTimeout(() => (form ? form.requestSubmit() : location.reload()), quieto ? 0 : 250);
  };
  const suelta = () => (y >= UMBRAL ? actualiza() : vuelve());

  // Dedo (móvil y tableta)
  let inicio = null;
  addEventListener('touchstart', (e) => {
    if (ocupado || scrollY > 0 || e.touches.length !== 1) return;
    inicio = e.touches[0].clientY - (y > 0 ? y : 0);
  }, { passive: true });
  addEventListener('touchmove', (e) => {
    if (inicio === null) return;
    const d = e.touches[0].clientY - inicio;
    if (d <= 0 || scrollY > 0) { if (y) pinta(0, false); return; }
    if (e.cancelable) e.preventDefault();       // la página no se mueve: solo baja el círculo
    pinta(goma(d), false);
  }, { passive: false });
  const fin = () => { if (inicio !== null) { inicio = null; suelta(); } };
  addEventListener('touchend', fin);
  addEventListener('touchcancel', () => { inicio = null; if (!ocupado) vuelve(); });

  // Trackpad o rueda: solo cuenta si el gesto empieza ya arriba del todo (no la inercia de un desplazamiento hacia arriba)
  let acumulado = 0, ultimo = 0, valido = false, pausa;
  addEventListener('wheel', (e) => {
    if (ocupado) return;
    const ahora = performance.now();
    if (ahora - ultimo > 300) valido = scrollY <= 0;  // gesto nuevo
    ultimo = ahora;
    if (!valido || scrollY > 0 || e.deltaY >= 0 || e.ctrlKey) { if (acumulado) { acumulado = 0; vuelve(); } return; }
    acumulado += -e.deltaY;
    pinta(goma(acumulado * 0.6), false);
    clearTimeout(pausa);
    pausa = setTimeout(() => { acumulado = 0; suelta(); }, 160);
  }, { passive: true });

  // Al volver atrás desde la caché del navegador, el círculo no debe quedarse girando
  addEventListener('pageshow', (e) => { if (e.persisted) { ocupado = false; ind.classList.remove('gira'); pinta(0, false); voz.textContent = ''; } });
})();
