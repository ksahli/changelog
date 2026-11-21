// Moving pictures: each story picture is redrawn live as a halftone, with a slow
// camera drift, a gentle breathing, shimmering dots and the odd flicker, like a
// photograph that won't quite sit still. The static halftone <img> stays in place
// as the fallback (no WebGL, reduced motion), and the effect pauses off-screen.
(() => {
  const FPS = 24;
  const FADE = 0.6; // seconds
  const MAX_DOT = 0.88; // keep in sync with scripts/images.py
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const imgs = [...document.querySelectorAll(".story-plate img[data-gray]")];
  if (!imgs.length) return;

  const VERT = `
    attribute vec2 a_pos;
    void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;

  const FRAG = `
    precision highp float;
    uniform sampler2D u_tex;
    uniform vec2 u_res;
    uniform float u_time, u_cell, u_seed;

    float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    float noise(float x) {
      float i = floor(x), f = fract(x);
      return mix(hash(vec2(i, u_seed)), hash(vec2(i + 1.0, u_seed)), f * f * (3.0 - 2.0 * f));
    }

    // Slow pan and zoom plus a faint breathing; never shows past the picture's edges.
    vec2 camera(vec2 uv) {
      float t = u_time;
      float zoom = 1.08 + 0.035 * sin(t * 0.07 + u_seed);
      vec2 drift = 0.018 * vec2(sin(t * 0.05 + u_seed * 1.3), cos(t * 0.043 + u_seed * 0.7));
      vec2 c = uv - 0.5;
      c *= 1.0 + 0.004 * sin(t * 0.8 + u_seed) * (1.0 - length(c));
      return 0.5 + c / zoom + drift;
    }

    void main() {
      vec2 p = vec2(gl_FragCoord.x, u_res.y - gl_FragCoord.y);
      float a = radians(45.0 + 1.5 * sin(u_time * 0.09 + u_seed));
      mat2 rot = mat2(cos(a), sin(a), -sin(a), cos(a));
      mat2 unrot = mat2(cos(a), -sin(a), sin(a), cos(a));
      vec2 q = rot * p;
      vec2 home = floor(q / u_cell);

      // Global flicker, and a brief dip every 6-9 seconds.
      float flick = 1.0 + 0.025 * (noise(u_time * 3.0) * 2.0 - 1.0);
      float period = 6.0 + 3.0 * hash(vec2(u_seed, 7.0));
      float ph = fract((u_time + u_seed * 3.0) / period);
      float dip = 1.0 - 0.1 * smoothstep(0.0, 0.015, ph) * (1.0 - smoothstep(0.03, 0.06, ph));

      // Dots can outgrow their cell, so look at the neighbours too.
      float ink = 0.0;
      for (int dx = -1; dx <= 1; dx++) {
        for (int dy = -1; dy <= 1; dy++) {
          if (dx != 0 && dy != 0) continue;
          vec2 id = home + vec2(float(dx), float(dy));
          vec2 center = (id + 0.5) * u_cell;
          float lum = texture2D(u_tex, camera((unrot * center) / u_res)).r;
          lum = clamp(lum * flick * dip, 0.0, 1.0);
          float r = ${MAX_DOT.toFixed(2)} * sqrt(1.0 - lum) * u_cell * 0.7071;
          r *= 1.0 + 0.04 * sin(u_time * 2.1 + hash(id) * 6.2832);
          float d = length(q - center);
          ink = max(ink, 1.0 - smoothstep(r - 0.7, r + 0.7, d));
        }
      }
      gl_FragColor = vec4(vec3(1.0 - ink), 1.0);
    }`;

  const seedOf = (s) => {
    let h = 2166136261;
    for (const ch of s) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
    return ((h >>> 0) % 10000) / 100;
  };

  function compile(gl) {
    const shader = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const pos = gl.getAttribLocation(prog, "a_pos");
    gl.enableVertexAttribArray(pos);
    gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0);
    const u = (n) => gl.getUniformLocation(prog, n);
    return { res: u("u_res"), time: u("u_time"), cell: u("u_cell"), seed: u("u_seed") };
  }

  const plates = new Map(); // img -> plate
  let running = false;
  let last = 0;

  function makePlate(img) {
    const canvas = document.createElement("canvas");
    canvas.setAttribute("aria-hidden", "true");
    canvas.style.opacity = 0;
    const gl = canvas.getContext("webgl", { antialias: false, alpha: false, preserveDrawingBuffer: false });
    if (!gl) return null;
    let uniforms;
    try { uniforms = compile(gl); } catch (e) { console.warn("motion:", e); return null; }

    const plate = { img, canvas, gl, uniforms, visible: true, ready: false, fade: 0,
                    seed: seedOf(img.dataset.seed || img.src), cell: +img.dataset.cell || 6 };

    const source = new Image();
    source.decoding = "async";
    source.crossOrigin = "anonymous"; // a texture must be CORS-clean (same origin on Pages)
    source.onload = () => {
      gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      try {
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, gl.LUMINANCE, gl.UNSIGNED_BYTE, source);
      } catch (e) {
        console.warn("motion:", e);
        return drop(plate);
      }
      plate.ready = true;
      kick();
    };
    source.onerror = () => drop(plate);
    source.src = img.dataset.gray;

    canvas.addEventListener("webglcontextlost", () => drop(plate));
    img.after(canvas);
    return plate;
  }

  function drop(plate) {
    plate.canvas.remove();
    plate.img.style.visibility = "";
    plates.delete(plate.img);
    io.unobserve(plate.img);
  }

  function draw(plate, t) {
    const { gl, canvas, img, uniforms } = plate;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const w = Math.round(img.clientWidth * dpr), h = Math.round(img.clientHeight * dpr);
    if (!w || !h) return;
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, w, h);
    gl.uniform2f(uniforms.res, w, h);
    gl.uniform1f(uniforms.time, t);
    // Same dot pitch as the static halftone, scaled to the canvas.
    gl.uniform1f(uniforms.cell, plate.cell * (w / (+img.getAttribute("width") || w)));
    gl.uniform1f(uniforms.seed, plate.seed);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    // Fade the live plate in over the first frames, then hide the static one so the
    // two don't double up under multiply. Driven here rather than by a CSS transition.
    if (plate.fade < 1) {
      plate.fade = Math.min(1, plate.fade + 1 / (FADE * FPS));
      canvas.style.opacity = plate.fade;
      if (plate.fade === 1) img.style.visibility = "hidden";
    }
  }

  function frame(now) {
    if (!running) return;
    const live = [...plates.values()].filter((p) => p.visible && p.ready);
    if (!live.length) { running = false; return; }
    if (now - last >= 1000 / FPS) {
      last = now;
      for (const p of live) draw(p, now / 1000);
    }
    requestAnimationFrame(frame);
  }

  function kick() {
    if (!running) { running = true; requestAnimationFrame(frame); }
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      let plate = plates.get(e.target);
      if (e.isIntersecting && !plate) {
        plate = makePlate(e.target);
        if (!plate) { io.unobserve(e.target); continue; }
        plates.set(e.target, plate);
      }
      if (plate) plate.visible = e.isIntersecting;
    }
    kick();
  }, { rootMargin: "200px" });

  function start() { for (const img of imgs) io.observe(img); }
  function stop() {
    io.disconnect();
    for (const p of [...plates.values()]) drop(p);
    running = false;
  }

  if (!reduce.matches) start();
  reduce.addEventListener("change", () => (reduce.matches ? stop() : start()));
})();
