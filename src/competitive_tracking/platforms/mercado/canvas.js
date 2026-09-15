// Installed before navigation. Capture only text actually drawn on page canvases.
// No network interception or private application state is used.
(() => {
  if (window.__ctCanvasInstalled) return;
  window.__ctCanvasInstalled = true;
  const draws = new WeakMap();
  window.__ctCanvasText = canvas => (draws.get(canvas) || [])
    .slice().sort((a, b) => a.y - b.y || a.x - b.x).map(x => x.text).join('');
  for (const name of ['fillText', 'strokeText']) {
    const original = CanvasRenderingContext2D.prototype[name];
    CanvasRenderingContext2D.prototype[name] = function(text, x, y, ...args) {
      const result = original.call(this, text, x, y, ...args);
      let rows = draws.get(this.canvas) || [];
      rows = rows.filter(r => r.x !== x || r.y !== y);
      rows.push({text: String(text), x, y});
      draws.set(this.canvas, rows);
      return result;
    };
  }
  const clear = CanvasRenderingContext2D.prototype.clearRect;
  CanvasRenderingContext2D.prototype.clearRect = function(x, y, w, h) {
    const result = clear.call(this, x, y, w, h);
    if (x <= 0 && y <= 0 && w >= this.canvas.width && h >= this.canvas.height)
      draws.delete(this.canvas);
    return result;
  };
  for (const name of ['width', 'height']) {
    const desc = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, name);
    Object.defineProperty(HTMLCanvasElement.prototype, name, {
      ...desc, set(value) { draws.delete(this); desc.set.call(this, value); }
    });
  }
})();
