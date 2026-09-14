"use client";
// frontend/app/components/AssistantAvatar.jsx
//
// Small branded mark shown next to assistant replies — the same
// pen-draw logo as the standalone animatedsvg.html reference (same
// path data, same brand red #FF2052, same per-path timelines below),
// just sized down for inline chat use. Two call sites:
//
//   - MessageBubble.jsx renders it once per finished assistant bubble,
//     `thinking={false}` (the default): a plain, fully-filled mark —
//     no motion at all. MessageRow.jsx's list is virtualized
//     (react-window), so this can mount/unmount repeatedly as the user
//     scrolls, and it should just sit there looking like a logo, not
//     replay or idle-pulse every time it remounts.
//
//   - WorkspaceChatPanel.jsx renders it with `thinking` while a reply
//     is in flight: the mark draws itself in with the pen-stroke effect
//     — literally the same drawDuration/drawEasing/fillDelay values as
//     animatedsvg.html's getPath1Timeline/getPath2Timeline — looped
//     for as long as `thinking` stays true, since a reply can take
//     longer than one draw cycle.
//
// No background, no badge shape, no CSS-only idle animation. Only the
// logo itself ever moves, and only in `thinking` mode.
//
// `prefers-reduced-motion` is respected: the thinking loop's mount
// effect bails out before ever starting the WAAPI chain, leaving a
// static filled mark instead.
import { useEffect, useRef } from "react";

const FILL_COLOR = "#FF2052"; // matches /public/minime-logo.svg + animatedsvg.html

const PATH1_D =
  "M3395 7019 c-69 -13 -229 -67 -290 -97 -111 -56 -224 -142 -341 -264\nc-115 -119 -182 -209 -298 -401 -586 -967 -915 -2135 -894 -3173 5 -237 16\n-302 78 -468 99 -267 338 -506 613 -616 157 -62 223 -74 427 -74 157 -1 198 3\n270 21 379 96 676 370 789 725 51 161 62 249 71 553 9 320 23 421 81 600 65\n200 179 377 352 542 127 121 226 191 497 354 461 277 768 565 1079 1014 23 33\n80 116 128 185 123 177 173 238 242 295 128 105 248 159 428 190 292 51 639\n-33 930 -225 42 -28 80 -49 85 -48 30 10 -214 363 -366 528 -151 163 -274 255\n-419 310 -118 46 -196 60 -340 60 -367 0 -682 -152 -930 -449 -116 -138 -134\n-164 -325 -446 -82 -121 -188 -259 -221 -289 -21 -19 -21 -19 -47 5 -34 31\n-107 130 -229 309 -220 324 -364 503 -495 613 -153 129 -339 217 -521 246 -77\n13 -284 12 -354 0z m333 -720 c105 -51 201 -153 350 -371 180 -264 266 -386\n325 -460 37 -48 67 -90 67 -95 0 -4 -94 -69 -208 -143 -261 -171 -394 -274\n-547 -425 -278 -275 -441 -562 -524 -925 -53 -229 -71 -394 -71 -665 0 -203\n-13 -282 -60 -383 -64 -136 -198 -215 -365 -216 -202 0 -347 104 -405 293 -35\n112 -24 563 21 871 90 617 257 1158 527 1710 177 363 317 586 440 701 153 144\n300 179 450 108z";

const PATH2_D =
  "M6322 4382 c-113 23 -230 12 -321 -33 c-79 -38 -189 -152 -228 -234 c-35 -74 -67 -171 -78 -236 l-7 -43 l503 0 l502 0 l-6 64 c-9 97 -63 248 -112 315 c-71 96 -142 143 -253 167 z m358 1918 c0 10 161 6 230 -5 c245 -39 428 -124 626 -290 c238 -201 387 -449 549 -919 c143 -415 238 -841 305 -1370 c20 -161 24 -445 6 -550 c-64 -391 -253 -683 -586 -905 c-230 -154 -526 -253 -854 -286 c-146 -14 -453 -7 -586 15 c-492 79 -920 361 -1147 757 c-194 338 -255 821 -158 1244 c130 568 541 933 1084 962 c460 24 841 -189 1039 -582 c125 -250 181 -533 169 -863 c-4 -89 -9 -167 -12 -172 c-4 -6 -314 -10 -830 -10 l-823 0 l15 -67 c36 -161 132 -320 250 -414 c181 -145 435 -221 738 -221 c160 -1 253 11 411 52 c185 48 359 150 458 271 c53 64 119 200 135 277 c25 120 9 355 -50 732 c-94 602 -339 1316 -624 1815 c-120 210 -185 312 -287 447 c-32 42 -58 79 -58 82 z";

// Kept identical, value-for-value, to animatedsvg.html's own
// getPath1Timeline/getPath2Timeline. If that reference file's timings
// ever change, update these to match.
function getPath1Timeline() {
  return {
    drawDuration: 5000,
    drawDelay: 0,
    drawEasing: "cubic-bezier(0.65, 0, 0.35, 1)",
    fillDuration: 0,
    fillDelay: -4000,
  };
}
function getPath2Timeline() {
  return {
    drawDuration: 5000,
    drawDelay: 0,
    drawEasing: "cubic-bezier(0.65, 0, 0.35, 1)",
    fillDuration: 0,
    fillDelay: -4000,
  };
}

// No .mm-avatar background/border-radius, no keyframes at all — the
// avatar is the raw SVG mark and nothing else. Sizing is done via the
// span's inline width/height (the `size` prop) with the SVG filling it.
const STYLES = `
.mm-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.mm-avatar-mark {
  width: 100%;
  height: 100%;
  overflow: visible;
}
`;

export default function AssistantAvatar({ size = 26, thinking = false, className = "" }) {
  const path1Ref = useRef(null);
  const path2Ref = useRef(null);

  useEffect(() => {
    const path1 = path1Ref.current;
    const path2 = path2Ref.current;
    if (!path1 || !path2) return;

    const reduceMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // Same prepare() as animatedsvg.html: cancel any running animation
    // from a previous mount/toggle, reset the dash offset so a fresh
    // draw-in always starts from a blank outline.
    function prepare(path) {
      path.getAnimations().forEach((anim) => anim.cancel());
      const length = path.getTotalLength();
      path.style.fill = FILL_COLOR;
      path.style.stroke = FILL_COLOR;
      path.style.strokeWidth = "2.5";
      path.style.strokeLinecap = "round";
      path.style.strokeLinejoin = "round";
      path.style.strokeDasharray = length;
      return length;
    }

    if (!thinking || reduceMotion) {
      // Idle mark: plain and fully filled, no stroke reveal, no motion
      // of any kind — matches MessageBubble's "just sits there" use.
      prepare(path1);
      prepare(path2);
      path1.style.strokeDashoffset = 0;
      path1.style.fillOpacity = "1";
      path1.style.strokeOpacity = "0";
      path2.style.strokeDashoffset = 0;
      path2.style.fillOpacity = "1";
      path2.style.strokeOpacity = "0";
      return () => {
        [path1, path2].forEach((p) => p.getAnimations().forEach((a) => a.cancel()));
      };
    }

    let cancelled = false;

    // Same animatePath() as animatedsvg.html: draw the stroke in over
    // `drawDuration`, and cross-fade from stroke to solid fill starting
    // `fillDelay` relative to when the draw finishes (matches the
    // reference file's math exactly, negative fillDelay included).
    function animatePath(path, timeline) {
      const length = prepare(path);
      path.style.fillOpacity = "0";
      path.style.strokeOpacity = "1";
      path.style.strokeDashoffset = length;

      const strokeAnim = path.animate(
        [{ strokeDashoffset: length }, { strokeDashoffset: 0 }],
        {
          duration: timeline.drawDuration,
          delay: timeline.drawDelay,
          easing: timeline.drawEasing,
          fill: "forwards",
        }
      );

      const totalFillDelay = timeline.drawDelay + timeline.drawDuration + timeline.fillDelay;
      path.animate(
        [
          { fillOpacity: 0, strokeOpacity: 1 },
          { fillOpacity: 1, strokeOpacity: 0 },
        ],
        {
          duration: timeline.fillDuration,
          delay: Math.max(totalFillDelay, 0),
          easing: "ease-out",
          fill: "forwards",
        }
      );

      return strokeAnim;
    }

    // Loops the exact one-shot animatedsvg.html sequence back-to-back —
    // draw in, fill, then immediately redraw — for as long as `thinking`
    // stays true, since a reply's length isn't known up front.
    async function loop() {
      while (!cancelled) {
        const a1 = animatePath(path1, getPath1Timeline());
        const a2 = animatePath(path2, getPath2Timeline());
        await Promise.allSettled([a1.finished, a2.finished]);
      }
    }

    loop();

    return () => {
      cancelled = true;
      [path1, path2].forEach((p) => p.getAnimations().forEach((a) => a.cancel()));
    };
  }, [thinking]);

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: STYLES }} />
      <span
        className={`mm-avatar ${className}`}
        style={{ width: size, height: size }}
        aria-hidden="true"
      >
        <svg className="mm-avatar-mark" viewBox="0 0 1024 844" xmlns="http://www.w3.org/2000/svg">
          <g transform="translate(0,844) scale(0.1,-0.1)">
            <path ref={path1Ref} vectorEffect="non-scaling-stroke" d={PATH1_D} fill={FILL_COLOR} />
            <path ref={path2Ref} vectorEffect="non-scaling-stroke" d={PATH2_D} fill={FILL_COLOR} />
          </g>
        </svg>
      </span>
    </>
  );
}
