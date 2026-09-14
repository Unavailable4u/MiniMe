"use client";
// frontend/app/components/AssistantAvatar.jsx
//
// Small branded mark shown next to assistant replies — the same
// pen-draw logo as LoadingScreen.jsx / public/minime-logo.svg (same
// path data, same brand red #FF2052), just sized down for inline chat
// use instead of a full-screen boot sequence. Two call sites:
//
//   - MessageBubble.jsx renders it once per finished assistant bubble,
//     `thinking={false}` (the default): a plain static filled mark,
//     no animation at all — same treatment as the header logo beside
//     "MiniMe" in AppShell.jsx. No stroke-dash math and no WAAPI here
//     on purpose — MessageRow.jsx's list is virtualized (react-window),
//     so this can mount/unmount repeatedly as the user scrolls.
//
//   - WorkspaceChatPanel.jsx renders it with `thinking` while a reply
//     is in flight (dock.state.loading), in place of the old plain
//     "Working…" text: the mark actually draws itself in and undraws
//     in a loop, same pen-stroke technique as LoadingScreen's intro,
//     looped via the Web Animations API instead of one-shot.
//
// `prefers-reduced-motion` is respected in thinking mode: the loop's
// mount effect bails out before ever starting the WAAPI chain. There's
// nothing to gate in idle mode since it no longer animates.
import { useEffect, useRef } from "react";

const FILL_COLOR = "#FF2052"; // matches /public/minime-logo.svg + LoadingScreen.jsx

const PATH1_D =
  "M3395 7019 c-69 -13 -229 -67 -290 -97 -111 -56 -224 -142 -341 -264\nc-115 -119 -182 -209 -298 -401 -586 -967 -915 -2135 -894 -3173 5 -237 16\n-302 78 -468 99 -267 338 -506 613 -616 157 -62 223 -74 427 -74 157 -1 198 3\n270 21 379 96 676 370 789 725 51 161 62 249 71 553 9 320 23 421 81 600 65\n200 179 377 352 542 127 121 226 191 497 354 461 277 768 565 1079 1014 23 33\n80 116 128 185 123 177 173 238 242 295 128 105 248 159 428 190 292 51 639\n-33 930 -225 42 -28 80 -49 85 -48 30 10 -214 363 -366 528 -151 163 -274 255\n-419 310 -118 46 -196 60 -340 60 -367 0 -682 -152 -930 -449 -116 -138 -134\n-164 -325 -446 -82 -121 -188 -259 -221 -289 -21 -19 -21 -19 -47 5 -34 31\n-107 130 -229 309 -220 324 -364 503 -495 613 -153 129 -339 217 -521 246 -77\n13 -284 12 -354 0z m333 -720 c105 -51 201 -153 350 -371 180 -264 266 -386\n325 -460 37 -48 67 -90 67 -95 0 -4 -94 -69 -208 -143 -261 -171 -394 -274\n-547 -425 -278 -275 -441 -562 -524 -925 -53 -229 -71 -394 -71 -665 0 -203\n-13 -282 -60 -383 -64 -136 -198 -215 -365 -216 -202 0 -347 104 -405 293 -35\n112 -24 563 21 871 90 617 257 1158 527 1710 177 363 317 586 440 701 153 144\n300 179 450 108z";

const PATH2_D =
  "M6322 4382 c-113 23 -230 12 -321 -33 c-79 -38 -189 -152 -228 -234 c-35 -74 -67 -171 -78 -236 l-7 -43 l503 0 l502 0 l-6 64 c-9 97 -63 248 -112 315 c-71 96 -142 143 -253 167 z m358 1918 c0 10 161 6 230 -5 c245 -39 428 -124 626 -290 c238 -201 387 -449 549 -919 c143 -415 238 -841 305 -1370 c20 -161 24 -445 6 -550 c-64 -391 -253 -683 -586 -905 c-230 -154 -526 -253 -854 -286 c-146 -14 -453 -7 -586 15 c-492 79 -920 361 -1147 757 c-194 338 -255 821 -158 1244 c130 568 541 933 1084 962 c460 24 841 -189 1039 -582 c125 -250 181 -533 169 -863 c-4 -89 -9 -167 -12 -172 c-4 -6 -314 -10 -830 -10 l-823 0 l15 -67 c36 -161 132 -320 250 -414 c181 -145 435 -221 738 -221 c160 -1 253 11 411 52 c185 48 359 150 458 271 c53 64 119 200 135 277 c25 120 9 355 -50 732 c-94 602 -339 1316 -624 1815 c-120 210 -185 312 -287 447 c-32 42 -58 79 -58 82 z";

// Draw-in timelines for the "thinking" loop — short (this plays on
// every turn, not once per session the way LoadingScreen's intro
// does), with path2 given a small stagger so the two strokes don't
// land in perfect lockstep.
// fillDelay = -0.8 * drawDuration, same ratio LoadingScreen.jsx's intro
// uses (fillDelay: -880 over a 1100ms draw). The fill only needs to
// take over after a brief opening flourish of stroke — letting the
// stroke trace the *entire* outline at full opacity is what was
// making the mark's tighter corners look like they overlapped
// themselves, since a constant-width round-joined stroke doesn't
// trace a variable-width glyph outline's sharp corners cleanly.
function getPath1Timeline() {
  return {
    drawDuration: 700,
    drawDelay: 0,
    easing: "cubic-bezier(0.65, 0, 0.35, 1)",
    fillDuration: 0,
    fillDelay: -560,
  };
}
function getPath2Timeline() {
  return {
    drawDuration: 700,
    drawDelay: 90,
    easing: "cubic-bezier(0.65, 0, 0.35, 1)",
    fillDuration: 0,
    fillDelay: -560,
  };
}
const HOLD_MS = 480; // pause on the fully-drawn mark before undrawing
const UNDRAW_MS = 340; // quick fade back to blank before the next loop iteration
const REST_MS = 140; // brief blank pause between loop iterations

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
    if (!thinking) return; // idle mark just relies on the CSS breathing loop above

    const path1 = path1Ref.current;
    const path2 = path2Ref.current;
    if (!path1 || !path2) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      // Leave both paths solid-filled (see JSX below) rather than
      // driving a stroke animation neither user nor OS wants.
      return;
    }

    let cancelled = false;
    const timeouts = [];
    const wait = (ms) =>
      new Promise((resolve) => timeouts.push(setTimeout(resolve, ms)));

    function prepare(path) {
      path.getAnimations().forEach((anim) => anim.cancel());
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

    // Two separate animate() calls — same technique as LoadingScreen.jsx's
    // animatePath() / the original reference template — rather than one
    // animation interpolating strokeDashoffset and fillOpacity together.
    // Interpolating both at once made the fill fade in across the *whole*
    // shape while the outline was still only partially drawn, so corners
    // the pen hadn't reached yet were already ghosting in with fill; that
    // mismatch is what read as "overlapping" and made the mark look
    // thicker than intended. Keeping the draw and the fill as independent
    // animations (the fill starting only once its own delay hits, per
    // timeline.fillDelay) keeps them visually in sync with each other.
    function drawIn(path, timeline) {
      const length = prepare(path);
      path.animate(
        [{ strokeDashoffset: length }, { strokeDashoffset: 0 }],
        {
          duration: timeline.drawDuration,
          delay: timeline.drawDelay,
          easing: timeline.easing,
          fill: "forwards",
        }
      );
      const totalFillDelay = timeline.drawDelay + timeline.drawDuration + timeline.fillDelay;
      return path.animate(
        [
          { fillOpacity: 0, strokeOpacity: 1 },
          { fillOpacity: 1, strokeOpacity: 0 },
        ],
        { duration: timeline.fillDuration, delay: totalFillDelay, easing: "ease-out", fill: "forwards" }
      );
    }

    // Just fade the solid mark back out. drawIn() above only ever shows a
    // brief flourish of stroke before the fill takes over (see fillDelay),
    // so there's no long traced length to retrace on the way out — a
    // straight fillOpacity fade is the clean "blank" state the loop
    // pauses on between reps.
    function undraw(path) {
      return path.animate(
        [{ fillOpacity: 1 }, { fillOpacity: 0 }],
        { duration: UNDRAW_MS, easing: "ease-in", fill: "forwards" }
      );
    }

    async function loop() {
      while (!cancelled) {
        const a1 = drawIn(path1, getPath1Timeline());
        const a2 = drawIn(path2, getPath2Timeline());
        await Promise.allSettled([a1.finished, a2.finished]);
        if (cancelled) break;

        await wait(HOLD_MS);
        if (cancelled) break;

        const u1 = undraw(path1);
        const u2 = undraw(path2);
        await Promise.allSettled([u1.finished, u2.finished]);
        if (cancelled) break;

        await wait(REST_MS);
      }
    }

    loop();

    return () => {
      cancelled = true;
      timeouts.forEach(clearTimeout);
      [path1, path2].forEach((p) => p.getAnimations().forEach((a) => a.cancel()));
    };
  }, [thinking]);

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: STYLES }} />
      <span
        className={`mm-avatar ${thinking ? "mm-avatar-thinking" : ""} ${className}`}
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
