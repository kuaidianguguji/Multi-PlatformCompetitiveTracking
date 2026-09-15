// Clone table DOM only, replacing captured canvas text in the clone.
// The real page is not modified; offscreen DOM columns are preserved.
const roots = [...document.querySelectorAll('.vxe-table')];
if (!roots.length) return '';
return roots.map(root => {
  const clone = root.cloneNode(true);
  const original = root.querySelectorAll('canvas');
  clone.querySelectorAll('canvas').forEach((canvas, i) => {
    const value = window.__ctCanvasText ? window.__ctCanvasText(original[i]) : '';
    canvas.setAttribute('data-ct-text', value);
  });
  return clone.outerHTML;
}).join('\n');
