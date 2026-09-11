"use client";
// frontend/app/components/LoadingScreen.jsx
//
// Ported from the standalone logo-loading-screen-v3.html template
// (pen-draw logo -> cyberpunk progress bar -> impact flash + glass
// shatter reveal). Mounted by Gate.jsx on top of AppShell the moment a
// user is signed in, whether they just landed on "/app" straight from
// the marketing page or just finished logging in on LoginScreen — see
// Gate.jsx's own comment for why both paths funnel through the same
// branch. The mark/paths below are the exact ones baked into
// /public/minime-logo.svg (same viewBox, same brand red #FF2052), so
// this is the real MiniMe mark drawing itself in, not a generic spinner.
//
// The one real change from the template (besides React-ifying it) is
// what drives the finish: the original ran a fixed 5.2s progress-bar
// timer and shattered on a clock. This version instead takes `progress`
// (0-100) and `ready` from BootProgressContext.jsx via Gate.jsx, which
// reports the real state of AppShell's own bootstrap fetches
// (fetchBatches, fetchWorkspaces, chat-list restore/create — see
// AppShell.jsx). So: fast network -> bar fills and shatters fast; slow
// network -> the bar eases up short of 100 and the logo settles into its
// idle pulse loop (see .settled below) until the real data actually
// shows up, then it finishes. It can never show 100% / shatter before
// `ready` is true, so the visitor is never dropped onto an empty app.
//
// Everything below the JSX return is intentionally close to a straight
// port of the template's own vanilla-JS functions (preparePath,
// animatePath, freezeVisual, impactFlash, buildShatter, ...), just
// reading refs instead of getElementById and reading progress/ready off
// refs each animation frame instead of closing over a fixed duration —
// the shatter effect's "clone the whole frozen screen per shard" trick
// in particular is much simpler to keep as direct DOM work (like the
// original) than to fight into React's declarative model.
import { useEffect, useRef } from "react";

const FILL_COLOR = "#FF2052"; // matches /public/minime-logo.svg exactly

const PATH1_D =
  "M3395 7019 c-69 -13 -229 -67 -290 -97 -111 -56 -224 -142 -341 -264\nc-115 -119 -182 -209 -298 -401 -586 -967 -915 -2135 -894 -3173 5 -237 16\n-302 78 -468 99 -267 338 -506 613 -616 157 -62 223 -74 427 -74 157 -1 198 3\n270 21 379 96 676 370 789 725 51 161 62 249 71 553 9 320 23 421 81 600 65\n200 179 377 352 542 127 121 226 191 497 354 461 277 768 565 1079 1014 23 33\n80 116 128 185 123 177 173 238 242 295 128 105 248 159 428 190 292 51 639\n-33 930 -225 42 -28 80 -49 85 -48 30 10 -214 363 -366 528 -151 163 -274 255\n-419 310 -118 46 -196 60 -340 60 -367 0 -682 -152 -930 -449 -116 -138 -134\n-164 -325 -446 -82 -121 -188 -259 -221 -289 -21 -19 -21 -19 -47 5 -34 31\n-107 130 -229 309 -220 324 -364 503 -495 613 -153 129 -339 217 -521 246 -77\n13 -284 12 -354 0z m333 -720 c105 -51 201 -153 350 -371 180 -264 266 -386\n325 -460 37 -48 67 -90 67 -95 0 -4 -94 -69 -208 -143 -261 -171 -394 -274\n-547 -425 -278 -275 -441 -562 -524 -925 -53 -229 -71 -394 -71 -665 0 -203\n-13 -282 -60 -383 -64 -136 -198 -215 -365 -216 -202 0 -347 104 -405 293 -35\n112 -24 563 21 871 90 617 257 1158 527 1710 177 363 317 586 440 701 153 144\n300 179 450 108z";

const PATH2_D =
  "M6322 4382 c-113 23 -230 12 -321 -33 c-79 -38 -189 -152 -228 -234 c-35 -74 -67 -171 -78 -236 l-7 -43 l503 0 l502 0 l-6 64 c-9 97 -63 248 -112 315 c-71 96 -142 143 -253 167 z m358 1918 c0 10 161 6 230 -5 c245 -39 428 -124 626 -290 c238 -201 387 -449 549 -919 c143 -415 238 -841 305 -1370 c20 -161 24 -445 6 -550 c-64 -391 -253 -683 -586 -905 c-230 -154 -526 -253 -854 -286 c-146 -14 -453 -7 -586 15 c-492 79 -920 361 -1147 757 c-194 338 -255 821 -158 1244 c130 568 541 933 1084 962 c460 24 841 -189 1039 -582 c125 -250 181 -533 169 -863 c-4 -89 -9 -167 -12 -172 c-4 -6 -314 -10 -830 -10 l-823 0 l15 -67 c36 -161 132 -320 250 -414 c181 -145 435 -221 738 -221 c160 -1 253 11 411 52 c185 48 359 150 458 271 c53 64 119 200 135 277 c25 120 9 355 -50 732 c-94 602 -339 1316 -624 1815 c-120 210 -185 312 -287 447 c-32 42 -58 79 -58 82 z";

// Independent draw timelines for the two logo sub-paths, same shape as
// the template's own getPath1Timeline/getPath2Timeline: draw the stroke
// in over `drawDuration`, then swap stroke -> solid fill instantly
// (`fillDuration: 0`) at `drawDelay + drawDuration + fillDelay` ms.
// Shortened from the template's 5000ms default (and given path2 a small
// stagger) so the intro doesn't force a multi-second wait on a fast
// connection on its own — see the file-header comment on how this
// interacts with real `progress`/`ready`.
function getPath1Timeline() {
  return { drawDuration: 1100, drawDelay: 0, drawEasing: "cubic-bezier(0.65, 0, 0.35, 1)", fillDuration: 0, fillDelay: -880 };
}
function getPath2Timeline() {
  return { drawDuration: 1100, drawDelay: 120, drawEasing: "cubic-bezier(0.65, 0, 0.35, 1)", fillDuration: 0, fillDelay: -880 };
}

// Scoped custom properties instead of :root — this mounts inside a much
// bigger app and shouldn't leak --brand/--bg/etc. into the global scope.
const STYLES = `
.mm-loading-screen {
  --mm-brand: ${FILL_COLOR};
  --mm-brand-hot: #FF4D6D;
  --mm-bg: #0B0709;
  --mm-line: rgba(255, 32, 82, 0.28);
  --mm-ink: #9A8D91;

  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background:
    radial-gradient(ellipse at 50% 40%, rgba(255,32,82,0.06), transparent 60%),
    repeating-linear-gradient(180deg, rgba(255,255,255,0.02) 0px, rgba(255,255,255,0.02) 1px, transparent 1px, transparent 3px),
    var(--mm-bg);
}

.mm-vignette {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: radial-gradient(circle at 50% 45%, transparent 25%, rgba(0,0,0,0.65) 100%);
  opacity: 0.55;
}

.mm-loading-screen.settled .mm-vignette { animation: mmDimPulse 2.8s ease-in-out infinite; }

@keyframes mmDimPulse {
  0%, 100% { opacity: 0.5; }
  50%      { opacity: 0.78; }
}

.mm-stage {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 40px;
  position: relative;
  z-index: 1;
}

.mm-mark-wrap { position: relative; display: grid; place-items: center; }

.mm-glow {
  position: absolute;
  width: 420px;
  height: 420px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(255,32,82,0.34) 0%, rgba(255,32,82,0) 70%);
  opacity: 0;
  animation: mmGlowIn 1.4s ease-out 0.2s forwards;
  pointer-events: none;
}

@keyframes mmGlowIn { to { opacity: 0.7; } }

.mm-loading-screen.settled .mm-glow { animation: mmGlowPulse 2.8s ease-in-out infinite; }

@keyframes mmGlowPulse {
  0%, 100% { opacity: 0.55; transform: scale(1); }
  50%      { opacity: 1;    transform: scale(1.18); }
}

.mm-mark {
  width: min(58vw, 300px);
  height: auto;
  overflow: visible;
  position: relative;
  z-index: 2;
}

.mm-mark-inner { transform-origin: 50% 50%; }

.mm-loading-screen.settled .mm-mark-inner { animation: mmBreathe 2.8s ease-in-out infinite; }

@keyframes mmBreathe {
  0%, 100% { transform: scale(1);     filter: drop-shadow(0 0 6px rgba(255,32,82,0.3)); }
  50%      { transform: scale(1.045); filter: drop-shadow(0 0 24px rgba(255,32,82,0.7)); }
}

.mm-bar-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  opacity: 0;
  animation: mmFadeIn 0.5s ease-out 0.5s forwards;
}

@keyframes mmFadeIn { to { opacity: 1; } }

.mm-bar-status { font-size: 11px; letter-spacing: 0.14em; color: var(--mm-ink); }

.mm-bar-track {
  position: relative;
  width: min(64vw, 320px);
  height: 10px;
  background: #16101250;
  border: 1px solid var(--mm-line);
  clip-path: polygon(8px 0, 100% 0, calc(100% - 8px) 100%, 0 100%);
  overflow: hidden;
}

.mm-bar-fill {
  position: absolute;
  inset: 0;
  width: 0%;
  background:
    repeating-linear-gradient(120deg, rgba(255,255,255,0.18) 0 2px, transparent 2px 10px),
    linear-gradient(90deg, var(--mm-brand), var(--mm-brand-hot));
  background-size: 24px 100%, 100% 100%;
  animation: mmBarScan 0.9s linear infinite;
  box-shadow: 0 0 12px rgba(255,32,82,0.55);
}

.mm-bar-fill.complete { box-shadow: 0 0 24px rgba(255,77,109,0.95); }

@keyframes mmBarScan { to { background-position: 24px 0, 0 0; } }

.mm-bar-pct {
  font-family: "SF Mono", "Courier New", monospace;
  font-size: 13px;
  letter-spacing: 0.05em;
  color: var(--mm-brand-hot);
  min-width: 4ch;
  text-align: center;
}

.mm-impact-flash {
  position: fixed;
  inset: 0;
  z-index: 1002;
  pointer-events: none;
  background: radial-gradient(circle at var(--wx,50%) var(--wy,45%), rgba(255,255,255,0.9), rgba(255,32,82,0.45) 40%, transparent 72%);
  opacity: 0;
  animation: mmFlashPulse 420ms ease-out forwards;
}

@keyframes mmFlashPulse {
  0%   { opacity: 0; }
  18%  { opacity: 1; }
  100% { opacity: 0; }
}

.mm-shatter-layer { position: fixed; inset: 0; z-index: 1001; pointer-events: none; }

.mm-shard { position: absolute; overflow: hidden; transform: translate(0, 0) rotate(0deg); opacity: 1; }

.mm-shatter-layer.falling .mm-shard {
  animation-name: mmShardFall;
  animation-timing-function: cubic-bezier(0.55, 0.06, 0.68, 0.19);
  animation-fill-mode: forwards;
}

@keyframes mmShardFall {
  to { transform: translate(var(--dx), 130vh) rotate(var(--rot)); opacity: 0; }
}

.mm-shard-source { position: absolute; pointer-events: none; }

@media (prefers-reduced-motion: reduce) {
  .mm-loading-screen.settled .mm-glow,
  .mm-loading-screen.settled .mm-mark-inner,
  .mm-loading-screen.settled .mm-vignette,
  .mm-bar-fill { animation: none !important; }
}
`;

export default function LoadingScreen({ progress = 0, ready = false, onDone }) {
  const rootRef = useRef(null);
  const path1Ref = useRef(null);
  const path2Ref = useRef(null);
  const markInnerRef = useRef(null);
  const glowRef = useRef(null);
  const vignetteRef = useRef(null);
  const markElRef = useRef(null);
  const barFillRef = useRef(null);
  const barPctRef = useRef(null);

  // Read as refs (updated every render, below) rather than effect deps,
  // so the rAF loop / in-flight WAAPI animations started in the mount
  // effect are never torn down and restarted by a progress tick.
  const progressRef = useRef(progress);
  const readyRef = useRef(ready);
  progressRef.current = progress;
  readyRef.current = ready;

  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => {
    const root = rootRef.current;
    const path1 = path1Ref.current;
    const path2 = path2Ref.current;
    const markInner = markInnerRef.current;
    const glowEl = glowRef.current;
    const markEl = markElRef.current;
    const vignette = vignetteRef.current;
    const barFill = barFillRef.current;
    const barPct = barPctRef.current;

    const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let cancelled = false;
    let rafId = null;
    let displayPct = 0;
    let introDone = REDUCED_MOTION;
    let completing = false;
    const timeouts = [];
    const appendedNodes = [];

    function preparePath(path) {
      const length = path.getTotalLength();
      path.style.fill = FILL_COLOR;
      path.style.fillOpacity = "0";
      path.style.stroke = FILL_COLOR;
      path.style.strokeWidth = "2.5";
      path.style.strokeLinecap = "round";
      path.style.strokeLinejoin = "round";
      path.style.strokeOpacity = "1";
      path.style.strokeDasharray = length;
      path.style.strokeDashoffset = length;
      return length;
    }

    function animatePath(path, timeline) {
      const length = preparePath(path);
      const strokeAnim = path.animate(
        [{ strokeDashoffset: length }, { strokeDashoffset: 0 }],
        { duration: timeline.drawDuration, delay: timeline.drawDelay, easing: timeline.drawEasing, fill: "forwards" }
      );
      const totalFillDelay = timeline.drawDelay + timeline.drawDuration + timeline.fillDelay;
      path.animate(
        [{ fillOpacity: 0, strokeOpacity: 1 }, { fillOpacity: 1, strokeOpacity: 0 }],
        { duration: timeline.fillDuration, delay: totalFillDelay, easing: "ease-out", fill: "forwards" }
      );
      return strokeAnim;
    }

    // ---------- progress bar, driven by real progress/ready ----------
    function updateBar() {
      const target = readyRef.current ? 100 : Math.max(0, Math.min(progressRef.current, 100));
      if (displayPct < target) {
        const next = displayPct + (target - displayPct) * 0.12;
        displayPct = target - next < 0.15 ? target : next;
      } else if (!readyRef.current && displayPct < 92) {
        // Gentle idle creep: if the real signal sits still for a while
        // (a slow fetch between two milestones), the bar keeps inching
        // forward instead of looking frozen — capped well under 100 so
        // it can never claim "done" before `ready` actually says so.
        displayPct += 0.04;
      }

      const pct = Math.round(displayPct);
      if (barFill) barFill.style.width = displayPct + "%";
      if (barPct) barPct.textContent = String(pct).padStart(3, "0") + "%";

      if (readyRef.current && introDone && displayPct >= 99.5 && !completing) {
        completing = true;
        if (barFill) { barFill.classList.add("complete"); barFill.style.width = "100%"; }
        if (barPct) barPct.textContent = "100%";
        timeouts.push(setTimeout(triggerCompletion, REDUCED_MOTION ? 0 : 380));
        return;
      }

      if (!cancelled) rafId = requestAnimationFrame(updateBar);
    }

    // ---------- glass shatter transition (same trick as the template:
    // freeze one bright frame, then clone the whole frozen node once per
    // shard so every window shows the identical still image) ----------
    function freezeVisual() {
      root.classList.remove("settled");
      if (markInner) {
        markInner.style.animation = "none";
        markInner.style.transform = "scale(1.08)";
        markInner.style.filter = "drop-shadow(0 0 28px rgba(255,32,82,0.8))";
      }
      if (glowEl) {
        glowEl.style.animation = "none";
        glowEl.style.opacity = "1";
        glowEl.style.transform = "scale(1.2)";
      }
      if (barFill) {
        barFill.style.animation = "none";
        barFill.style.backgroundPosition = "0 0, 0 0";
      }
      if (vignette) {
        vignette.style.animation = "none";
        vignette.style.opacity = "0.85";
      }
    }

    function impactFlash(wx, wy) {
      const flash = document.createElement("div");
      flash.className = "mm-impact-flash";
      flash.style.setProperty("--wx", wx);
      flash.style.setProperty("--wy", wy);
      document.body.appendChild(flash);
      appendedNodes.push(flash);
      timeouts.push(setTimeout(() => flash.remove(), 450));
    }

    function randInt(min, max) { return Math.floor(min + Math.random() * (max - min + 1)); }

    function irregularSplits(total, count) {
      const points = [0];
      const base = total / count;
      for (let i = 1; i < count; i++) {
        const jitter = (Math.random() - 0.5) * base * 0.6;
        points.push(Math.round(points[i - 1] + base + jitter));
      }
      points.push(total);
      for (let i = 1; i < points.length; i++) {
        if (points[i] <= points[i - 1]) points[i] = points[i - 1] + 4;
      }
      points[points.length - 1] = total;
      return points;
    }

    function jaggedPolygon() {
      const j = () => (2 + Math.random() * 7).toFixed(1);
      return `polygon(${j()}% 0%, ${100 - j()}% 0%, 100% ${j()}%, 100% ${100 - j()}%, ${100 - j()}% 100%, ${j()}% 100%, 0% ${100 - j()}%, 0% ${j()}%)`;
    }

    function finish() {
      if (root) root.style.display = "none";
      if (onDoneRef.current) onDoneRef.current();
    }

    function buildShatter() {
      const vw = window.innerWidth, vh = window.innerHeight;
      const layer = document.createElement("div");
      layer.className = "mm-shatter-layer";

      const cols = randInt(6, 9);
      const rows = randInt(5, 7);
      const xs = irregularSplits(vw, cols);
      const ys = irregularSplits(vh, rows);
      let maxDuration = 0;

      for (let i = 0; i < cols; i++) {
        for (let j = 0; j < rows; j++) {
          const x = xs[i], cw = xs[i + 1] - xs[i];
          const y = ys[j], ch = ys[j + 1] - ys[j];
          if (cw <= 0 || ch <= 0) continue;

          const shard = document.createElement("div");
          shard.className = "mm-shard";
          shard.style.left = x + "px";
          shard.style.top = y + "px";
          shard.style.width = cw + "px";
          shard.style.height = ch + "px";
          shard.style.clipPath = jaggedPolygon();

          // Live-sprite technique: a full-size clone of the (now frozen)
          // loading screen node, shifted so only this cell's slice shows
          // through the shard's jagged clip-path window. root's own
          // <style> tag is rendered as a sibling in the component's
          // return (not a child of root), so this clone never drags a
          // duplicate copy of the whole stylesheet along with it.
          const inner = root.cloneNode(true);
          inner.classList.add("mm-shard-source");
          inner.style.left = -x + "px";
          inner.style.top = -y + "px";
          inner.style.width = vw + "px";
          inner.style.height = vh + "px";
          shard.appendChild(inner);

          const dx = (Math.random() * 180 - 90).toFixed(1) + "px";
          const rot = (Math.random() * 110 - 55).toFixed(1) + "deg";
          const delay = Math.random() * 220;
          const dur = 700 + Math.random() * 550;
          shard.style.setProperty("--dx", dx);
          shard.style.setProperty("--rot", rot);
          shard.style.animationDelay = delay + "ms";
          shard.style.animationDuration = dur + "ms";
          maxDuration = Math.max(maxDuration, delay + dur);

          layer.appendChild(shard);
        }
      }

      document.body.appendChild(layer);
      appendedNodes.push(layer);
      root.style.visibility = "hidden"; // clones already show the identical frame

      requestAnimationFrame(() => layer.classList.add("falling"));

      timeouts.push(setTimeout(() => {
        layer.remove();
        finish();
      }, maxDuration + 150));
    }

    function triggerCompletion() {
      const rect = markEl.getBoundingClientRect();
      const wx = ((rect.left + rect.width / 2) / window.innerWidth * 100).toFixed(2) + "%";
      const wy = ((rect.top + rect.height / 2) / window.innerHeight * 100).toFixed(2) + "%";

      freezeVisual();

      if (REDUCED_MOTION) {
        finish();
        return;
      }

      impactFlash(wx, wy);
      buildShatter();
    }

    // ---------- intro: draw the mark in, or paint it solid for reduced motion ----------
    if (REDUCED_MOTION) {
      [path1, path2].forEach((p) => {
        preparePath(p);
        p.style.fillOpacity = "1";
        p.style.strokeOpacity = "0";
      });
      introDone = true;
    } else {
      const a1 = animatePath(path1, getPath1Timeline());
      const a2 = animatePath(path2, getPath2Timeline());
      Promise.all([a1.finished, a2.finished])
        .then(() => {
          introDone = true;
          if (!cancelled) root.classList.add("settled");
        })
        .catch(() => {
          // Interrupted (e.g. fast unmount) — still let updateBar's
          // `ready` check proceed rather than waiting forever on a
          // promise that will never resolve.
          introDone = true;
        });
    }

    rafId = requestAnimationFrame(updateBar);

    return () => {
      cancelled = true;
      if (rafId) cancelAnimationFrame(rafId);
      timeouts.forEach(clearTimeout);
      appendedNodes.forEach((node) => node.remove());
    };
    // Deliberately empty deps: this runs once on mount and reads
    // progress/ready off the refs kept up to date above, so a progress
    // update never restarts the pen-draw animation or the rAF loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <style>{STYLES}</style>
      <div ref={rootRef} className="mm-loading-screen">
        <div className="mm-vignette" ref={vignetteRef} />
        <div className="mm-stage">
          <div className="mm-mark-wrap">
            <div className="mm-glow" ref={glowRef} />
            <svg
              className="mm-mark"
              ref={markElRef}
              viewBox="0 0 1024 844"
              xmlns="http://www.w3.org/2000/svg"
            >
              <g className="mm-mark-inner" ref={markInnerRef}>
                <g transform="translate(0,844) scale(0.1,-0.1)">
                  <path ref={path1Ref} vectorEffect="non-scaling-stroke" d={PATH1_D} />
                  <path ref={path2Ref} vectorEffect="non-scaling-stroke" d={PATH2_D} />
                </g>
              </g>
            </svg>
          </div>

          <div className="mm-bar-block">
            <div className="mm-bar-status">initializing</div>
            <div className="mm-bar-track">
              <div className="mm-bar-fill" ref={barFillRef} />
            </div>
            <div className="mm-bar-pct" ref={barPctRef}>000%</div>
          </div>
        </div>
      </div>
    </>
  );
}
