"use client";

import { useEffect, useRef } from "react";
import { supabase } from "../lib/supabaseClient";
import { clearLocalAppState } from "../lib/clearLocalAppState";   // BUGFIX — see that file's header comment; this page's own Sign out button calls supabase.auth.signOut() directly, bypassing AuthContext.jsx's signOut(), so it needs this same cleanup independently

// ---------------------------------------------------------------------
// Ported verbatim from minime_new.html (the standalone static template).
// This is intentionally a self-contained, unstyled-by-the-app landing
// page: its own <style> block below carries the full design (colors,
// fonts, layout) independent of app/globals.css, and its own markup/JS
// are the same ones the template shipped with. The app's real theme
// (globals.css, AppShell, etc.) lives at "/app", entirely separate from
// this page (rendered at "/", see app/page.js), and never loads here, so
// there's no visual clash to reconcile yet -- that's expected until the
// app's theme is redone to match.
//
// CTA wiring: "Start building" (nav + final CTA) points at "/app" -- the
// app's actual, unmodified entry point. "/app" already branches correctly
// on its own (Gate.jsx: LoginScreen for signed-out visitors, AppShell
// for signed-in ones), so a signed-up visitor coming from this landing
// page lands straight in the app and a new visitor lands on sign-up,
// without this page needing to know or check which case it is. Every
// other link (Capabilities, How it works, Pricing, Built for, footer
// About/Contact/Privacy/Terms) is unchanged same-page anchor navigation,
// exactly as in the template.
//
// Nav "Sign in" was replaced with a profile control: it imports the same
// cookie-backed `supabase` client AuthContext.jsx/middleware.js already
// use (see lib/supabaseClient.js), so it sees the same session as "/app"
// without this page needing its own AuthProvider -- signed out shows a
// plain profile-icon link to "/app"; signed in shows the user's
// name/avatar and a small menu (Open MiniMe / Sign out). This is also
// what a later transaction system will key off of to know who's buying.
//
// Pricing "Start with <tier>" buttons no longer link to "/app" -- that
// section is for buying, not for entering the app, so they now open a
// mock checkout card (below) instead. The checkout card is a full
// layout preview only: its submit button is disabled and reads "Not
// available yet" until real payments are wired up.
// ---------------------------------------------------------------------

const LANDING_STYLES = "  :root{\n    --ink:#0A0A0C;\n    --ink-2:#121214;\n    --ink-3:#19191C;\n    --paper:#0D0D0F;\n    --paper-2:#161618;\n    --line:#FF2052;\n    --line-soft:rgba(255,32,82,0.16);\n    --line-mid:rgba(255,32,82,0.45);\n    --coral:#FF2052;\n    --coral-soft:rgba(255,32,82,0.16);\n    --coral-mid:rgba(255,32,82,0.5);\n    --text:#F2F1ED;\n    --text-dim:#A8A6A0;\n    --text-dim-2:#726F68;\n    --ink-on-paper:#F2F1ED;\n    --font-display:'Sora',sans-serif;\n    --font-hero:'Sora',sans-serif;\n    --font-body:'Inter',sans-serif;\n    --font-mono:'JetBrains Mono',monospace;\n    --radius:6px;\n    --container:1180px;\n    --cut:14px;\n  }\n  *{box-sizing:border-box;}\n  html{scroll-behavior:smooth;}\n  @media (prefers-reduced-motion: reduce){\n    html{scroll-behavior:auto;}\n    *,*::before,*::after{animation-duration:0.001ms !important;animation-iteration-count:1 !important;transition-duration:0.001ms !important;}\n  }\n  body{\n    margin:0;\n    background:var(--ink);\n    color:var(--text);\n    font-family:var(--font-body);\n    font-size:16px;\n    line-height:1.6;\n    -webkit-font-smoothing:antialiased;\n  }\n  ::-webkit-scrollbar{width:10px;height:10px;}\n  ::-webkit-scrollbar-track{background:var(--ink);}\n  ::-webkit-scrollbar-thumb{background:linear-gradient(var(--coral),var(--line));border-radius:0;}\n  body::before{\n    content:\"\";\n    position:fixed;\n    inset:-2px;\n    z-index:0;\n    pointer-events:none;\n    background-image:\n      linear-gradient(var(--line-soft) 1px, transparent 1px),\n      linear-gradient(90deg, var(--line-soft) 1px, transparent 1px);\n    background-size:44px 44px;\n    opacity:0.35;\n    -webkit-mask-image:radial-gradient(ellipse 70% 55% at 50% 0%, #000 40%, transparent 85%);\n    mask-image:radial-gradient(ellipse 70% 55% at 50% 0%, #000 40%, transparent 85%);\n    animation:gridDrift 26s linear infinite;\n  }\n  @keyframes gridDrift{\n    0%{background-position:0 0, 0 0;}\n    100%{background-position:44px 88px, 44px 88px;}\n  }\n  .boot-flash{\n    position:fixed;inset:0;z-index:999;pointer-events:none;background:var(--text);\n    animation:bootFlash 1.1s cubic-bezier(.5,0,.2,1) forwards;\n  }\n  @keyframes bootFlash{\n    0%{opacity:0.9;}\n    12%{opacity:0.05;}\n    16%{opacity:0.6;}\n    22%{opacity:0;}\n    100%{opacity:0;}\n  }\n  .spotlight{\n    position:fixed;inset:0;z-index:1;pointer-events:none;\n    background:radial-gradient(480px circle at var(--mx,50%) var(--my,20%), rgba(255,32,82,0.10), transparent 60%);\n    transition:opacity .3s ease;\n    opacity:0;\n  }\n  .hero:hover .spotlight, .hero.spot-active .spotlight{opacity:1;}\n  img,svg{display:block;max-width:100%;}\n  a{color:inherit;text-decoration:none;}\n  ul{margin:0;padding:0;list-style:none;}\n  h1,h2,h3,h4{font-family:var(--font-display);font-weight:600;margin:0;color:var(--text);}\n  p{margin:0;}\n  .wrap{max-width:var(--container);margin:0 auto;padding:0 24px;position:relative;z-index:1;}\n  @media (max-width:640px){.wrap{padding:0 18px;}}\n  ::selection{background:var(--coral-soft);color:var(--text);}\n  a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--line);outline-offset:3px;border-radius:3px;}\n  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;}\n  .skip-link{position:absolute;left:12px;top:-60px;background:var(--coral);color:#fff;padding:10px 16px;border-radius:6px;z-index:200;transition:top .2s ease;}\n  .skip-link:focus{top:12px;}\n\n  /* ---------- Buttons ---------- */\n  .btn{\n    position:relative;display:inline-flex;align-items:center;justify-content:center;gap:8px;\n    font-family:var(--font-display);font-weight:600;font-size:1rem;letter-spacing:0.02em;\n    padding:13px 26px;border-radius:0;border:1px solid transparent;\n    clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px);\n    cursor:pointer;transition:transform .18s ease, box-shadow .18s ease, background .18s ease, border-color .18s ease;\n    white-space:nowrap;overflow:hidden;\n  }\n  .btn::after{\n    content:\"\";position:absolute;inset:0;background:linear-gradient(115deg,transparent 20%,rgba(255,255,255,0.35) 42%,transparent 60%);\n    transform:translateX(-120%);transition:none;\n  }\n  .btn:hover::after{transform:translateX(120%);transition:transform .65s ease;}\n  .btn-primary{background:var(--coral);color:#fff;box-shadow:0 0 0 1px rgba(255,32,82,0.35), 0 0 22px -4px rgba(255,32,82,0.75), 0 8px 24px -8px rgba(255,32,82,0.55);}\n  .btn-primary:hover{transform:translateY(-1px);box-shadow:0 0 0 1px rgba(255,32,82,0.55), 0 0 32px -2px rgba(255,32,82,0.9), 0 12px 28px -8px rgba(255,32,82,0.65);}\n  .btn-ghost{background:rgba(255,32,82,0.04);color:var(--text);border-color:rgba(255,255,255,0.25);}\n  .btn-ghost:hover{border-color:var(--line);background:rgba(255,32,82,0.09);box-shadow:0 0 18px -6px var(--line-mid);}\n  .btn-block{width:100%;}\n\n  /* ---------- Header ---------- */\n  header{\n    position:sticky;top:0;z-index:100;\n    background:rgba(5,5,9,0.86);\n    backdrop-filter:blur(14px);\n    border-bottom:1px solid rgba(255,32,82,0.16);\n    box-shadow:0 1px 24px -4px rgba(255,32,82,0.12);\n    transition:box-shadow .3s ease, border-color .3s ease;\n  }\n  header.scrolled{border-bottom-color:rgba(255,32,82,0.3);box-shadow:0 1px 30px -2px rgba(255,32,82,0.18);}\n  .nav-row{display:flex;align-items:center;justify-content:space-between;height:72px;gap:24px;}\n  .brand{display:flex;align-items:center;gap:10px;}\n  .brand-mark{width:26px;height:26px;color:var(--coral);filter:drop-shadow(0 0 6px var(--coral-mid));}\n  .brand-word{font-family:var(--font-hero);font-weight:700;font-size:1.1rem;letter-spacing:0.01em;}\n  .brand-word em{font-style:normal;color:var(--coral);text-shadow:0 0 12px var(--coral-mid);}\n  nav.primary-nav{display:flex;align-items:center;gap:4px;}\n  .nav-link{\n    position:relative;padding:9px 14px;font-size:0.94rem;font-family:var(--font-display);font-weight:600;letter-spacing:0.02em;color:var(--text-dim);\n    transition:color .18s ease;\n  }\n  .nav-link::after{\n    content:\"\";position:absolute;left:14px;right:14px;bottom:5px;height:1.5px;\n    background:linear-gradient(90deg,var(--coral),var(--line));transform:scaleX(0);transform-origin:left;\n    transition:transform .25s ease;box-shadow:0 0 8px var(--coral-mid);\n  }\n  .nav-link:hover{color:var(--text);text-shadow:0 0 10px var(--line-mid);}\n  .nav-link:hover::after{transform:scaleX(1);}\n  .nav-actions{display:flex;align-items:center;gap:12px;}\n  .nav-toggle{display:none;background:none;border:none;color:var(--text);width:36px;height:36px;cursor:pointer;}\n  .mobile-nav{display:none;flex-direction:column;gap:2px;padding:8px 0 18px;border-top:1px solid rgba(255,255,255,0.07);}\n  .mobile-nav a{padding:12px 4px;color:var(--text-dim);font-size:0.98rem;}\n  .mobile-nav.open{display:flex;}\n  @media (max-width:860px){\n    nav.primary-nav{display:none;}\n    .nav-toggle{display:inline-flex;align-items:center;justify-content:center;}\n    .nav-actions .btn{padding:10px 16px;font-size:0.88rem;}\n  }\n\n  /* ---------- Reveal on scroll ---------- */\n  .reveal{opacity:0;transform:translateY(16px);transition:opacity .7s ease, transform .7s ease;}\n  .reveal.is-visible{opacity:1;transform:translateY(0);}\n\n  /* ---------- Section rhythm ---------- */\n  section{position:relative;padding:96px 0;}\n  section.tight{padding:72px 0;}\n  @media (max-width:720px){section{padding:64px 0;} section.tight{padding:48px 0;}}\n  .section-head{max-width:640px;margin-bottom:52px;}\n  .section-tag{\n    font-family:var(--font-mono);font-size:0.72rem;letter-spacing:0.09em;\n    color:var(--line);text-transform:uppercase;display:inline-flex;align-items:center;gap:8px;margin-bottom:14px;\n    text-shadow:0 0 10px var(--line-mid);\n  }\n  .section-head h2{font-size:clamp(1.65rem,3vw,2.3rem);line-height:1.18;letter-spacing:-0.01em;}\n  .section-head p{margin-top:14px;color:var(--text-dim);font-size:1.05rem;max-width:56ch;}\n  .divider{border:none;border-top:1px solid rgba(255,255,255,0.07);}\n\n  /* ================= HERO ================= */\n  .hero{padding-top:64px;padding-bottom:80px;overflow:hidden;position:relative;}\n  .hero-top{position:relative;z-index:2;display:grid;grid-template-columns:1.25fr 1fr;gap:32px;align-items:center;}\n  @media (max-width:960px){.hero-top{grid-template-columns:1fr;gap:8px;}}\n  .hero-top-text{max-width:640px;}\n  .hero-visual{position:relative;display:flex;align-items:center;justify-content:center;}\n  .hero-visual::before{\n    content:\"\";position:absolute;inset:auto;width:70%;height:70%;\n    background:radial-gradient(circle,rgba(255,32,82,0.22),transparent 70%);\n    filter:blur(10px);z-index:0;pointer-events:none;\n    animation:heroGlowBlink 2.6s ease-in-out infinite;\n  }\n  @keyframes heroGlowBlink{\n    0%,100%{opacity:0.5;transform:scale(0.92);}\n    50%{opacity:1;transform:scale(1.08);}\n  }\n  .hero-logo-draw{\n    width:min(30vw,340px);height:auto;color:var(--coral);position:relative;z-index:1;\n    animation:heroLogoBlink 2.6s ease-in-out infinite;\n  }\n  @keyframes heroLogoBlink{\n    0%,100%{filter:drop-shadow(0 0 26px rgba(255,32,82,0.3));}\n    50%{filter:drop-shadow(0 0 52px rgba(255,32,82,0.85));}\n  }\n  @media (max-width:960px){\n    .hero-visual{order:-1;padding-top:8px;}\n    .hero-logo-draw{width:min(46vw,190px);}\n  }\n  .eyebrow-plate{\n    display:inline-flex;align-items:center;gap:8px;\n    font-family:var(--font-mono);font-size:0.72rem;letter-spacing:0.08em;\n    color:var(--text-dim);text-transform:uppercase;margin-bottom:20px;\n    border:1px solid var(--line-soft);padding:6px 12px 6px 10px;background:rgba(255,32,82,0.05);\n  }\n  .eyebrow-plate .dot{width:6px;height:6px;border-radius:50%;background:var(--coral);box-shadow:0 0 8px 1px var(--coral-mid);animation:pulse-dot 2.2s ease-in-out infinite;}\n  @keyframes pulse-dot{0%,100%{opacity:1;}50%{opacity:0.35;}}\n  h1.hero-h{\n    font-family:var(--font-hero);font-weight:700;\n    font-size:clamp(1.9rem,4.4vw,3.15rem);line-height:1.12;letter-spacing:-0.01em;\n    max-width:16ch;\n    animation:heroFadeIn .8s cubic-bezier(.25,.46,.45,.94) both;\n  }\n  @keyframes heroFadeIn{\n    0%{opacity:0;transform:translateY(14px);}\n    100%{opacity:1;transform:translateY(0);}\n  }\n  .hero-sub{margin-top:22px;font-size:1.13rem;color:var(--text-dim);max-width:54ch;line-height:1.65;}\n  .hero-ctas{display:flex;flex-wrap:wrap;gap:14px;margin-top:32px;}\n  .mode-chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:34px;}\n  .mode-chip{\n    display:inline-flex;align-items:center;gap:7px;\n    font-family:var(--font-mono);font-size:0.76rem;color:var(--text-dim);\n    border:1px solid var(--line-soft);border-radius:2px;padding:7px 14px 7px 10px;\n    background:rgba(255,32,82,0.03);\n  }\n  .mode-chip .sw{width:7px;height:7px;border-radius:50%;background:var(--line);box-shadow:0 0 6px var(--line-mid);}\n  .mode-chip.locked{color:var(--text-dim-2);border-color:rgba(255,255,255,0.1);}\n  .mode-chip.locked .sw{background:var(--text-dim-2);box-shadow:none;}\n  .mode-chip .tier-tag{color:var(--coral);font-weight:600;}\n\n  .hero-grid{margin-top:64px;display:grid;grid-template-columns:1.55fr 1fr;gap:22px;align-items:start;}\n  @media (max-width:960px){.hero-grid{grid-template-columns:1fr;}}\n\n  /* Demo card styled like a blueprint sheet */\n  .sheet{\n    background:linear-gradient(165deg,var(--ink-2),var(--ink-3));\n    border:1px solid var(--line-soft);\n    border-radius:var(--radius);\n    padding:26px;\n    box-shadow:0 0 0 1px rgba(255,32,82,0.05), 0 30px 70px -30px rgba(255,32,82,0.25);\n  }\n  .sheet-titlebar{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:8px;}\n  .sheet-titlebar .lab{font-family:var(--font-mono);font-size:0.7rem;letter-spacing:0.07em;color:var(--text-dim-2);text-transform:uppercase;}\n  .msg-row{display:flex;gap:14px;align-items:flex-start;}\n  .msg-row + .msg-row{margin-top:18px;}\n  .avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:0.8rem;}\n  .avatar.user{background:var(--ink-3);color:var(--text-dim);border:1px solid var(--line-soft);}\n  .avatar.ai{background:var(--coral);color:#fff;box-shadow:0 0 14px -1px var(--coral-mid);}\n  .bubble{flex:1;background:var(--ink-3);border-radius:4px;padding:16px 18px;}\n  .bubble.ai-bubble{background:var(--ink);border:1px solid var(--coral-soft);}\n  .bubble p{color:var(--text);font-size:0.97rem;}\n  .bubble p + p{margin-top:12px;}\n  .reasoning-toggle{\n    margin-top:14px;font-family:var(--font-mono);font-size:0.72rem;color:var(--line);\n    cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:6px;\n  }\n  .reasoning-toggle::-webkit-details-marker{display:none;}\n  .reasoning-body{margin-top:10px;padding:12px 14px;background:var(--ink-2);border-radius:7px;font-family:var(--font-mono);font-size:0.76rem;color:var(--text-dim);line-height:1.8;}\n  .stat-trio{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px;}\n  .stat-block{background:var(--ink-2);border-radius:2px;padding:12px 14px;border:1px solid rgba(255,255,255,0.06);}\n  .stat-block .k{font-family:var(--font-mono);font-size:0.66rem;letter-spacing:0.05em;color:var(--text-dim-2);text-transform:uppercase;display:block;}\n  .stat-block .v{font-family:var(--font-hero);font-weight:700;font-size:1.1rem;margin-top:4px;color:var(--text);}\n  .stat-block.accent .v{color:var(--line);text-shadow:0 0 10px var(--line-mid);}\n  .sheet-footer{display:flex;align-items:center;justify-content:space-between;margin-top:18px;flex-wrap:wrap;gap:10px;}\n  .ghost-link{font-size:0.85rem;color:var(--line);display:inline-flex;align-items:center;gap:6px;}\n\n  /* Right rail */\n  .rail{display:flex;flex-direction:column;gap:16px;}\n  .rail-card{background:linear-gradient(165deg,var(--ink-2),var(--ink-3));border:1px solid var(--line-soft);border-radius:var(--radius);padding:20px;}\n  .rail-card h4{font-size:0.72rem;font-family:var(--font-mono);letter-spacing:0.06em;color:var(--text-dim-2);text-transform:uppercase;margin-bottom:14px;}\n  .flow-list{display:flex;flex-direction:column;}\n  .flow-item{display:flex;align-items:center;gap:12px;padding:9px 0;position:relative;}\n  .flow-item:not(:last-child)::after{\n    content:\"\";position:absolute;left:11px;top:32px;width:1px;height:16px;background:rgba(255,255,255,0.2);\n  }\n  .flow-dot{width:23px;height:23px;border-radius:50%;background:var(--ink-3);border:1px solid var(--line-mid);display:flex;align-items:center;justify-content:center;flex-shrink:0;color:var(--line);box-shadow:0 0 8px -2px var(--line-mid);}\n  .flow-dot svg{width:12px;height:12px;}\n  .flow-item span.lbl{font-size:0.92rem;color:var(--text);}\n  .flow-item span.chk{margin-left:auto;font-family:var(--font-mono);font-size:0.68rem;color:var(--coral);}\n  .depth-rail .bars{display:flex;gap:5px;align-items:flex-end;height:34px;margin-bottom:10px;}\n  .depth-rail .bars i{flex:1;background:var(--line);border-radius:1px;opacity:0.3;box-shadow:0 0 8px var(--line-mid);}\n  .depth-rail .bars i:nth-child(1){height:35%;opacity:0.9;}\n  .depth-rail .bars i:nth-child(2){height:65%;opacity:0.55;}\n  .depth-rail .bars i:nth-child(3){height:100%;background:var(--coral);box-shadow:0 0 10px var(--coral-mid);}\n  .depth-labels{display:flex;justify-content:space-between;font-family:var(--font-mono);font-size:0.66rem;color:var(--text-dim-2);text-transform:uppercase;}\n\n  /* ================= OLD WAY / NEW WAY ================= */\n  .split-compare{display:grid;grid-template-columns:1fr 1fr;gap:20px;}\n  @media (max-width:760px){.split-compare{grid-template-columns:1fr;}}\n  .compare-card{border-radius:var(--radius);padding:30px;}\n  .compare-card.old{background:var(--ink-2);border:1px dashed rgba(255,255,255,0.2);}\n  .compare-card.new{background:linear-gradient(165deg, var(--ink-3), var(--ink-2));border:1px solid var(--coral-mid);box-shadow:0 0 40px -18px var(--coral-mid);}\n  .compare-label{font-family:var(--font-mono);font-size:0.72rem;letter-spacing:0.07em;text-transform:uppercase;margin-bottom:16px;display:block;}\n  .compare-card.old .compare-label{color:var(--text-dim-2);}\n  .compare-card.new .compare-label{color:var(--coral);}\n  .compare-card ul{display:flex;flex-direction:column;gap:12px;}\n  .compare-card li{display:flex;gap:10px;color:var(--text-dim);font-size:0.96rem;line-height:1.55;}\n  .compare-card.new li{color:var(--text);}\n  .compare-card li svg{width:17px;height:17px;flex-shrink:0;margin-top:3px;}\n  .compare-card.old li svg{color:var(--text-dim-2);}\n  .compare-card.new li svg{color:var(--coral);}\n\n  /* ================= CAPABILITIES ================= */\n  /* --- top tab strip, à la Opera GX's settings/playground/etc nav --- */\n  .cap-tabs{\n    display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;-ms-overflow-style:none;\n    padding-bottom:14px;margin-bottom:32px;border-bottom:1px solid rgba(255,255,255,0.08);\n  }\n  .cap-tabs::-webkit-scrollbar{display:none;}\n  .cap-tab{\n    flex:0 0 auto;appearance:none;background:none;border:none;cursor:pointer;\n    font-family:var(--font-display);font-weight:600;font-size:0.82rem;letter-spacing:0.02em;\n    color:var(--text-dim-2);padding:8px 15px 13px;position:relative;white-space:nowrap;\n    transition:color .25s ease, font-size .25s ease, text-shadow .25s ease;\n  }\n  .cap-tab::after{\n    content:\"\";position:absolute;left:15px;right:15px;bottom:-1px;height:2px;\n    background:linear-gradient(90deg,var(--coral),var(--line));transform:scaleX(0);transform-origin:center;\n    transition:transform .3s ease;box-shadow:0 0 8px var(--coral-mid);\n  }\n  .cap-tab:hover{color:var(--text-dim);}\n  .cap-tab.is-active{color:var(--text);font-size:0.95rem;text-shadow:0 0 12px var(--line-mid);}\n  .cap-tab.is-active::after{transform:scaleX(1);}\n\n  /* --- carousel stage --- */\n  .cap-stage{display:flex;align-items:center;gap:14px;}\n  .cap-nav{\n    flex:0 0 auto;width:44px;height:44px;border-radius:50%;padding:0;\n    border:1px solid rgba(255,255,255,0.14);background:var(--ink-2);color:var(--text-dim);\n    display:flex;align-items:center;justify-content:center;cursor:pointer;\n    transition:border-color .2s ease, color .2s ease, background .2s ease, box-shadow .2s ease;\n  }\n  .cap-nav svg{width:18px;height:18px;}\n  .cap-nav:hover{border-color:var(--line-mid);color:var(--text);background:var(--ink-3);box-shadow:0 0 16px -4px var(--coral-mid);}\n  @media (max-width:640px){.cap-nav{width:36px;height:36px;}.cap-nav svg{width:16px;height:16px;}}\n\n  /* Height is pinned in px by JS (syncCarouselHeight) to the tallest a card\n     ever gets (including the enlarged is-center state), so the viewport's\n     box size stays constant across slides instead of following whichever\n     card currently happens to be centered — which is what was making the\n     whole page's rendered height shift as the carousel animated. height:100%\n     here is a no-op until JS sets that px height (percentage of an auto\n     parent resolves to auto), so nothing changes before JS runs. */\n  .cap-track-viewport{flex:1 1 auto;overflow:hidden;}\n  .cap-track{display:flex;gap:22px;height:100%;will-change:transform;transition:transform .55s cubic-bezier(.65,0,.35,1);}\n  .cap-track.no-anim{transition:none;}\n\n  .cap-card{\n    flex:0 0 calc((100% - 44px)/3);\n    background:linear-gradient(165deg, rgba(255,32,82,0.08), rgba(13,13,15,0.2)), var(--ink-2);\n    border:1px solid rgba(255,32,82,0.14);border-radius:var(--radius);\n    padding:26px;display:flex;flex-direction:column;cursor:pointer;\n    opacity:0.5;\n    transition:opacity .5s ease, border-color .35s ease, background .35s ease, box-shadow .35s ease, transform .35s ease;\n  }\n  @media (max-width:900px){.cap-card{flex:0 0 100%;}}\n  .cap-icon{width:38px;height:38px;border-radius:4px;background:var(--ink-3);display:flex;align-items:center;justify-content:center;color:var(--line);margin-bottom:18px;transition:color .3s ease,box-shadow .3s ease;}\n  .cap-icon svg{width:20px;height:20px;}\n  .cap-card h3{font-size:1.08rem;margin-bottom:9px;transition:font-size .4s ease, color .3s ease, text-shadow .4s ease;}\n  .cap-card p{color:var(--text-dim);font-size:0.93rem;line-height:1.6;flex:1 1 auto;}\n  .cap-card .cap-tag{\n    display:inline-block;margin-top:14px;font-family:var(--font-mono);font-size:0.66rem;\n    letter-spacing:0.05em;text-transform:uppercase;color:var(--text-dim-2);\n    border-top:1px solid rgba(255,255,255,0.08);padding-top:12px;width:100%;\n  }\n  .cap-card.is-center{\n    opacity:1;transform:translateY(-3px);cursor:default;\n    border-color:var(--line-mid);\n    background:linear-gradient(165deg, rgba(255,32,82,0.16), rgba(13,13,15,0.3)), var(--ink-3);\n    box-shadow:0 16px 40px -20px var(--line-mid);\n  }\n  .cap-card.is-center .cap-icon{color:var(--coral);box-shadow:0 0 16px -3px var(--coral-mid);}\n  .cap-card.is-center h3{font-size:1.34rem;color:var(--text);text-shadow:0 0 14px var(--line-mid);}\n  @media (max-width:640px){.cap-card.is-center h3{font-size:1.18rem;}}\n\n  .cap-dots{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin-top:30px;}\n  .cap-dot{width:7px;height:7px;padding:0;border-radius:4px;border:none;background:rgba(255,255,255,0.16);cursor:pointer;transition:all .3s ease;}\n  .cap-dot:hover{background:rgba(255,255,255,0.32);}\n  .cap-dot.is-active{width:24px;background:var(--coral);box-shadow:0 0 10px var(--coral-mid);}\n\n  /* ================= HOW IT WORKS ================= */\n  .steps{display:grid;grid-template-columns:repeat(4,1fr);gap:0;position:relative;}\n  @media (max-width:860px){.steps{grid-template-columns:1fr;gap:28px;}}\n  .step{position:relative;padding:0 20px 0 0;}\n  .step:first-child{padding-left:0;}\n  .step-num{\n    font-family:var(--font-mono);font-size:0.78rem;color:var(--line);margin-bottom:14px;display:block;\n    text-shadow:0 0 8px var(--line-mid);\n  }\n  .step h3{font-size:1.02rem;margin-bottom:8px;}\n  .step p{color:var(--text-dim);font-size:0.9rem;line-height:1.6;}\n  .step-line{display:none;}\n  @media (min-width:861px){\n    .steps::before{\n      content:\"\";position:absolute;top:9px;left:0;right:0;height:1px;background:rgba(255,255,255,0.1);\n    }\n  }\n\n  /* ================= USE CASES ================= */\n  .use-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;}\n  @media (max-width:860px){.use-grid{grid-template-columns:1fr;}}\n  .use-card{background:transparent;border:1px solid rgba(255,255,255,0.12);border-radius:var(--radius);padding:28px;transition:border-color .2s ease,box-shadow .2s ease;}\n  .use-card:hover{border-color:var(--coral-mid);box-shadow:0 0 30px -18px var(--coral-mid);}\n  .use-icon{width:34px;height:34px;color:var(--coral);margin-bottom:18px;filter:drop-shadow(0 0 6px var(--coral-mid));}\n  .use-icon svg{width:100%;height:100%;}\n  .use-card h3{font-size:1.05rem;margin-bottom:10px;}\n  .use-card p{color:var(--text-dim);font-size:0.93rem;line-height:1.6;}\n\n  /* ================= PRICING ================= */\n  .price-toggle-row{display:flex;align-items:center;gap:14px;margin-bottom:44px;flex-wrap:wrap;}\n  .switch{\n    position:relative;width:52px;height:28px;background:var(--ink-3);border:1px solid var(--line-soft);\n    border-radius:999px;cursor:pointer;flex-shrink:0;\n  }\n  .switch .thumb{\n    position:absolute;top:2px;left:2px;width:22px;height:22px;border-radius:50%;background:var(--text);\n    transition:transform .28s cubic-bezier(.65,0,.35,1);\n  }\n  .switch.on{background:var(--coral-soft);border-color:var(--coral-mid);}\n  .switch.on .thumb{transform:translateX(24px);background:var(--coral);box-shadow:0 0 10px var(--coral-mid);}\n  .price-toggle-row .toggle-label{font-size:0.92rem;color:var(--text-dim);}\n  .price-toggle-row .save-tag{font-family:var(--font-mono);font-size:0.7rem;color:var(--line);border:1px solid var(--line-mid);padding:3px 9px;border-radius:999px;text-shadow:0 0 8px var(--line-mid);}\n\n  .price-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;align-items:stretch;}\n  @media (max-width:960px){.price-grid{grid-template-columns:1fr;}}\n\n  /* Flip-card mechanics: front = features, back = pricing/models/token detail.\n     Height is measured and set in px by JS (syncPriceCardHeights) so content\n     never clips or overflows; this is a safe fallback before JS runs. */\n  .price-card-flip{\n    position:relative;perspective:1800px;min-height:540px;height:auto;cursor:pointer;\n  }\n  .price-card-inner{\n    position:relative;width:100%;height:100%;z-index:3;\n    transition:transform .7s cubic-bezier(.65,0,.35,1);transform-style:preserve-3d;\n  }\n  .price-card-flip:hover .price-card-inner,\n  .price-card-flip.flipped .price-card-inner{transform:rotateY(180deg);}\n  .price-card-face{\n    position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden;\n    background:linear-gradient(165deg,var(--ink-2),var(--paper));color:var(--text);border-radius:var(--radius);\n    padding:32px 28px;display:flex;flex-direction:column;\n    border:1px solid var(--line-soft);\n    z-index:2;\n  }\n  .price-card-face.back{transform:rotateY(180deg);}\n  .price-card-flip.hi .price-card-face{\n    background:linear-gradient(170deg,#1a0a14,#0d0812);color:var(--text);border:1px solid var(--coral-mid);\n    box-shadow:0 0 0 1px rgba(255,32,82,0.15), 0 30px 70px -30px var(--coral-mid);\n  }\n  .rev-tag{font-family:var(--font-mono);font-size:0.68rem;letter-spacing:0.08em;color:var(--text-dim-2);text-transform:uppercase;}\n  .recommended-tag{\n    position:absolute;top:-12px;right:24px;background:var(--coral);color:#fff;z-index:3;\n    font-family:var(--font-mono);font-size:0.65rem;letter-spacing:0.06em;text-transform:uppercase;\n    padding:5px 11px;border-radius:2px;box-shadow:0 0 14px -1px var(--coral-mid);\n  }\n  .price-card-face h3{font-family:var(--font-hero);font-weight:700;font-size:1.3rem;margin:10px 0 6px;color:var(--text);}\n  .price-card-face .tier-desc{font-size:0.9rem;color:var(--text-dim);margin-bottom:20px;line-height:1.55;}\n  .price-features{display:flex;flex-direction:column;gap:11px;margin-bottom:22px;}\n  .price-features li{display:flex;gap:9px;font-size:0.89rem;line-height:1.5;color:var(--text-dim);}\n  .price-features li svg{width:16px;height:16px;flex-shrink:0;margin-top:2px;color:var(--line);}\n  .price-card-flip.hi .price-features li svg{color:var(--coral);}\n  .tier-fit{border-top:1px solid rgba(255,255,255,0.08);padding-top:16px;margin-bottom:20px;}\n  .tier-fit .k{display:block;font-family:var(--font-mono);font-size:0.66rem;letter-spacing:0.06em;text-transform:uppercase;color:var(--text-dim-2);margin-bottom:6px;}\n  .tier-fit .v{font-size:0.87rem;color:var(--text);line-height:1.5;}\n  /* Bottom prompt on the front face — replaces a button; the only clickable\n     \"start\" action lives on the back, revealed by hovering (or tapping on touch).\n     margin-top:auto pins it to the card's bottom edge without stretching the\n     feature list or leaving a dead gap above it. */\n  .flip-hint{\n    display:flex;align-items:center;justify-content:center;gap:7px;margin-top:auto;\n    font-family:var(--font-mono);font-size:0.7rem;letter-spacing:0.04em;text-transform:uppercase;\n    color:var(--text-dim-2);border:1px dashed var(--line-soft);border-radius:999px;padding:10px 14px;\n    width:100%;transition:color .2s ease, border-color .2s ease;\n  }\n  .flip-hint svg{width:13px;height:13px;color:var(--coral);flex-shrink:0;}\n  .flip-hint .hint-tap{display:none;}\n  @media (hover:none){\n    .flip-hint .hint-hover{display:none;}\n    .flip-hint .hint-tap{display:inline;}\n  }\n  .pricing-note{margin-top:24px;font-size:0.82rem;color:var(--text-dim-2);}\n\n  /* Back face: pricing / model / token detail */\n  .price-num{display:flex;align-items:baseline;gap:6px;margin-bottom:2px;}\n  .price-num .amt{font-family:var(--font-hero);font-size:2.4rem;font-weight:700;transition:opacity .2s ease;color:var(--line);text-shadow:0 0 18px var(--line-mid);}\n  .price-card-flip.hi .price-num .amt{color:var(--coral);text-shadow:0 0 18px var(--coral-mid);}\n  .price-num .per{font-size:0.85rem;color:var(--text-dim);}\n  .billed-note{font-family:var(--font-mono);font-size:0.68rem;color:var(--text-dim-2);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:18px;min-height:14px;}\n  .back-label{font-family:var(--font-mono);font-size:0.66rem;letter-spacing:0.07em;text-transform:uppercase;color:var(--text-dim-2);margin-bottom:16px;}\n  .price-back-rows{display:flex;flex-direction:column;flex:1;}\n  .price-back-row{padding:12px 0;border-top:1px solid rgba(255,255,255,0.08);}\n  .price-back-row:first-child{border-top:none;padding-top:0;}\n  .price-back-row .k{display:block;font-family:var(--font-mono);font-size:0.66rem;letter-spacing:0.05em;text-transform:uppercase;color:var(--text-dim-2);margin-bottom:5px;}\n  .price-back-row .v{font-size:0.87rem;color:var(--text);line-height:1.5;}\n  .token-badge{\n    display:inline-flex;align-items:center;gap:5px;margin-left:8px;\n    background:var(--coral-soft);color:var(--coral);border:1px solid var(--coral-mid);\n    font-family:var(--font-mono);font-size:0.68rem;font-weight:600;letter-spacing:0.02em;\n    padding:2px 8px;border-radius:999px;white-space:nowrap;\n  }\n\n  /* ================= TRUST ================= */\n  .trust-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;}\n  .trust-item{padding:22px 0;border-top:1px solid rgba(255,255,255,0.08);}\n  .trust-item h4{font-size:0.98rem;margin-bottom:8px;}\n  .trust-item p{color:var(--text-dim);font-size:0.88rem;line-height:1.6;}\n\n  /* ================= HORIZON ================= */\n  .horizon-row{display:flex;flex-direction:column;gap:0;}\n  .horizon-item{\n    display:flex;align-items:center;justify-content:space-between;gap:20px;\n    padding:22px 0;border-top:1px solid rgba(255,255,255,0.08);flex-wrap:wrap;\n  }\n  .horizon-item:last-child{border-bottom:1px solid rgba(255,255,255,0.08);}\n  .horizon-item h4{font-size:1rem;font-weight:500;font-family:var(--font-body);}\n  .horizon-item p{color:var(--text-dim);font-size:0.87rem;margin-top:4px;max-width:52ch;}\n  .horizon-tag{\n    font-family:var(--font-mono);font-size:0.68rem;letter-spacing:0.05em;text-transform:uppercase;\n    color:var(--line);border:1px solid var(--line-mid);padding:5px 11px;border-radius:2px;flex-shrink:0;\n    text-shadow:0 0 8px var(--line-mid);\n  }\n\n  /* ================= FINAL CTA ================= */\n  .cta-band{\n    background:linear-gradient(135deg,var(--ink-3),var(--ink-2));\n    border:1px solid var(--coral-mid);border-radius:4px;\n    padding:56px 48px;display:flex;align-items:center;justify-content:space-between;gap:32px;flex-wrap:wrap;\n    position:relative;overflow:hidden;\n  }\n  .cta-band::after{\n    content:\"\";position:absolute;right:-60px;top:-60px;width:240px;height:240px;\n    background:var(--coral-soft);border-radius:50%;filter:blur(50px);\n    animation:ctaPulse 4s ease-in-out infinite;\n  }\n  @keyframes ctaPulse{0%,100%{opacity:0.7;transform:scale(1);}50%{opacity:1;transform:scale(1.15);}}\n  .cta-band h2{font-size:clamp(1.5rem,3vw,2rem);max-width:20ch;position:relative;}\n  .cta-band p{color:var(--text-dim);margin-top:10px;position:relative;}\n  .cta-band-actions{position:relative;display:flex;flex-direction:column;gap:10px;align-items:flex-start;}\n  .cta-fine{font-size:0.8rem;color:var(--text-dim-2);}\n  .pay-row{\n    margin-top:22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;\n  }\n  .pay-chip{\n    display:flex;align-items:center;gap:10px;\n    font-family:var(--font-mono);font-size:0.76rem;color:var(--text-dim);line-height:1.4;\n    border:1px solid rgba(255,255,255,0.09);border-radius:4px;padding:12px 14px;\n    background:rgba(255,255,255,0.02);\n  }\n  .pay-chip svg{width:17px;height:17px;color:var(--coral);flex-shrink:0;}\n\n  /* ================= FOOTER ================= */\n  footer{border-top:1px solid rgba(255,255,255,0.07);padding:64px 0 32px;}\n  .foot-grid{display:grid;grid-template-columns:1.6fr repeat(3,1fr);gap:32px;}\n  @media (max-width:760px){.foot-grid{grid-template-columns:1fr 1fr;}}\n  .foot-brand p{color:var(--text-dim);font-size:0.88rem;margin-top:12px;max-width:34ch;line-height:1.6;}\n  .foot-col h5{font-family:var(--font-mono);font-size:0.7rem;letter-spacing:0.06em;text-transform:uppercase;color:var(--text-dim-2);margin-bottom:14px;}\n  .foot-col ul{display:flex;flex-direction:column;gap:10px;}\n  .foot-col a{color:var(--text-dim);font-size:0.89rem;transition:color .18s ease;}\n  .foot-col a:hover{color:var(--text);}\n  .foot-bottom{\n    margin-top:48px;padding-top:24px;border-top:1px solid rgba(255,255,255,0.07);\n    display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;\n  }\n  .foot-bottom p{color:var(--text-dim-2);font-size:0.82rem;}\n  .foot-plate{font-family:var(--font-mono);font-size:0.68rem;color:var(--line);letter-spacing:0.05em;text-transform:uppercase;text-shadow:0 0 6px var(--line-mid);}\n\n  /* ================= RESPONSIVE POLISH ================= */\n  @media (max-width:640px){\n    .btn{clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%,0 8px);}\n  }\n\n  /* ================= NAV ACCOUNT / PROFILE ================= */\n  .nav-profile-btn{\n    width:38px;height:38px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;\n    border:1px solid rgba(255,255,255,0.25);background:rgba(255,32,82,0.04);color:var(--text);\n    transition:border-color .18s ease, background .18s ease, box-shadow .18s ease;\n  }\n  .nav-profile-btn:hover{border-color:var(--line);background:rgba(255,32,82,0.09);box-shadow:0 0 14px -4px var(--line-mid);}\n  .nav-profile-btn svg{width:18px;height:18px;}\n  .nav-account{position:relative;display:flex;align-items:center;}\n  .nav-account-btn{\n    display:none;align-items:center;gap:8px;background:rgba(255,32,82,0.04);border:1px solid rgba(255,255,255,0.2);\n    border-radius:999px;padding:5px 14px 5px 5px;cursor:pointer;color:var(--text);\n    font-family:var(--font-display);font-size:0.85rem;font-weight:600;max-width:180px;\n    transition:border-color .18s ease, background .18s ease;\n  }\n  .nav-account-btn:hover{border-color:var(--line-mid);background:rgba(255,32,82,0.09);}\n  .nav-account.is-signed-in .nav-profile-btn{display:none;}\n  .nav-account.is-signed-in .nav-account-btn{display:inline-flex;}\n  .nav-avatar{\n    width:26px;height:26px;border-radius:50%;background:var(--coral);color:#fff;flex-shrink:0;\n    display:flex;align-items:center;justify-content:center;font-size:0.72rem;font-weight:700;overflow:hidden;\n  }\n  .nav-avatar img{width:100%;height:100%;object-fit:cover;}\n  .nav-account-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}\n  .nav-account-menu{\n    position:absolute;top:calc(100% + 10px);right:0;min-width:210px;\n    background:linear-gradient(165deg,var(--ink-2),var(--ink-3));\n    border:1px solid var(--line-soft);border-radius:8px;padding:8px;display:none;z-index:150;\n    box-shadow:0 20px 50px -20px rgba(0,0,0,0.6);\n  }\n  .nav-account-menu.open{display:block;}\n  .nav-account-menu-email{\n    font-family:var(--font-mono);font-size:0.7rem;color:var(--text-dim-2);padding:6px 10px 10px;\n    border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:6px;word-break:break-all;\n  }\n  .nav-account-menu-item{\n    display:block;width:100%;text-align:left;padding:8px 10px;border-radius:5px;font-size:0.86rem;\n    color:var(--text-dim);background:none;border:none;cursor:pointer;font-family:var(--font-body);\n  }\n  .nav-account-menu-item:hover{background:rgba(255,32,82,0.08);color:var(--text);}\n  @media (max-width:860px){\n    .nav-profile-btn{width:34px;height:34px;}\n    .nav-account-btn{padding:4px 10px 4px 4px;font-size:0.8rem;max-width:130px;}\n  }\n\n  /* ================= CHECKOUT (mock) ================= */\n  .checkout-overlay{\n    position:fixed;inset:0;z-index:300;display:none;align-items:center;justify-content:center;\n    background:rgba(5,5,9,0.72);backdrop-filter:blur(6px);padding:24px;\n  }\n  .checkout-overlay.open{display:flex;}\n  .checkout-card{\n    position:relative;width:100%;max-width:420px;max-height:90vh;overflow-y:auto;\n    background:linear-gradient(165deg,var(--ink-2),var(--paper));border:1px solid var(--line-soft);\n    border-radius:var(--radius);padding:32px 28px;box-shadow:0 30px 80px -30px rgba(255,32,82,0.4);\n  }\n  .checkout-close{\n    position:absolute;top:16px;right:16px;width:30px;height:30px;border-radius:50%;\n    display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,0.06);\n    border:1px solid rgba(255,255,255,0.12);color:var(--text-dim);cursor:pointer;\n  }\n  .checkout-close:hover{color:var(--text);border-color:var(--line-mid);}\n  .checkout-close svg{width:14px;height:14px;}\n  .checkout-tag{\n    font-family:var(--font-mono);font-size:0.7rem;letter-spacing:0.08em;color:var(--line);\n    text-transform:uppercase;text-shadow:0 0 10px var(--line-mid);\n  }\n  .checkout-head h3{font-family:var(--font-hero);font-weight:700;font-size:1.4rem;margin:10px 0 6px;}\n  .checkout-head h3 span{color:var(--coral);}\n  .checkout-sub{color:var(--text-dim);font-size:0.9rem;margin-bottom:22px;}\n  .checkout-summary{\n    background:var(--ink-3);border:1px solid rgba(255,255,255,0.08);border-radius:6px;\n    padding:14px 16px;margin-bottom:24px;\n  }\n  .checkout-summary-row{display:flex;justify-content:space-between;font-size:0.86rem;color:var(--text-dim);padding:6px 0;}\n  .checkout-summary-row.total{\n    border-top:1px solid rgba(255,255,255,0.08);margin-top:4px;padding-top:10px;color:var(--text);font-weight:600;\n  }\n  .checkout-summary-row.total span:last-child{color:var(--line);text-shadow:0 0 8px var(--line-mid);}\n  .checkout-field{margin-bottom:14px;}\n  .checkout-field label{\n    display:block;font-family:var(--font-mono);font-size:0.68rem;letter-spacing:0.05em;\n    text-transform:uppercase;color:var(--text-dim-2);margin-bottom:6px;\n  }\n  .checkout-field input{\n    width:100%;background:var(--ink-3);border:1px solid rgba(255,255,255,0.14);border-radius:5px;\n    padding:10px 12px;color:var(--text);font-size:0.9rem;font-family:var(--font-body);\n    transition:border-color .18s ease;\n  }\n  .checkout-field input::placeholder{color:var(--text-dim-2);}\n  .checkout-field input:focus{outline:none;border-color:var(--line-mid);box-shadow:0 0 0 3px var(--line-soft);}\n  .checkout-field-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}\n  .checkout-submit{margin-top:6px;opacity:0.55;cursor:not-allowed;}\n  .checkout-submit:hover{transform:none !important;box-shadow:none !important;}\n  .checkout-fine{margin-top:12px;font-size:0.78rem;color:var(--text-dim-2);text-align:center;line-height:1.5;}\n";

const LANDING_BODY_HTML = "<div class=\"boot-flash\" aria-hidden=\"true\"></div>\n<a href=\"#main\" class=\"skip-link\">Skip to content</a>\n\n<header>\n  <div class=\"wrap nav-row\">\n    <a href=\"#top\" class=\"brand\">\n      <svg class=\"brand-mark\" viewBox=\"0 0 1024 844\" xmlns=\"http://www.w3.org/2000/svg\" fill=\"currentColor\" aria-hidden=\"true\">\n        <g transform=\"translate(0,844) scale(0.1,-0.1)\">\n          <path d=\"M3395 7019 c-69 -13 -229 -67 -290 -97 -111 -56 -224 -142 -341 -264 c-115 -119 -182 -209 -298 -401 -586 -967 -915 -2135 -894 -3173 5 -237 16 -302 78 -468 99 -267 338 -506 613 -616 157 -62 223 -74 427 -74 157 -1 198 3 270 21 379 96 676 370 789 725 51 161 62 249 71 553 9 320 23 421 81 600 65 200 179 377 352 542 127 121 226 191 497 354 461 277 768 565 1079 1014 23 33 80 116 128 185 123 177 173 238 242 295 128 105 248 159 428 190 292 51 639 -33 930 -225 42 -28 80 -49 85 -48 30 10 -214 363 -366 528 -151 163 -274 255 -419 310 -118 46 -196 60 -340 60 -367 0 -682 -152 -930 -449 -116 -138 -134 -164 -325 -446 -82 -121 -188 -259 -221 -289 -21 -19 -21 -19 -47 5 -34 31 -107 130 -229 309 -220 324 -364 503 -495 613 -153 129 -339 217 -521 246 -77 13 -284 12 -354 0z m333 -720 c105 -51 201 -153 350 -371 180 -264 266 -386 325 -460 37 -48 67 -90 67 -95 0 -4 -94 -69 -208 -143 -261 -171 -394 -274 -547 -425 -278 -275 -441 -562 -524 -925 -53 -229 -71 -394 -71 -665 0 -203 -13 -282 -60 -383 -64 -136 -198 -215 -365 -216 -202 0 -347 104 -405 293 -35 112 -24 563 21 871 90 617 257 1158 527 1710 177 363 317 586 440 701 153 144 300 179 450 108z\"/>\n          <path d=\"M6740 6254 c0 -3 26 -40 58 -82 102 -135 167 -237 287 -447 285 -499 530 -1213 624 -1815 59 -377 75 -612 50 -732 -16 -77 -82 -213 -135 -277 -99 -121 -273 -223 -458 -271 -158 -41 -251 -53 -411 -52 -303 0 -557 76 -738 221 -118 94 -214 253 -250 414 l-15 67 823 0 c516 0 826 4 830 10 3 5 8 83 12 172 12 330 -44 613 -169 863 -198 393 -579 606 -1039 582 -543 -29 -954 -394 -1084 -962 -97 -423 -36 -906 158 -1244 227 -396 655 -678 1147 -757 133 -22 440 -29 586 -15 328 33 624 132 854 286 333 222 522 514 586 905 18 105 14 389 -6 550 -67 529 -162 955 -305 1370 -162 470 -311 718 -549 919 -198 166 -381 251 -626 290 -69 11 -230 15 -230 5z m-358 -1918 c111 -24 182 -71 253 -167 49 -67 103 -218 112 -315 l6 -64 -502 0 -503 0 7 43 c11 65 43 162 78 236 39 82 149 196 228 234 91 45 208 56 321 33z\"/>\n        </g>\n      </svg>\n      <span class=\"brand-word\">Mini<em>Me</em></span>\n    </a>\n    <nav class=\"primary-nav\" aria-label=\"Primary\">\n      <a class=\"nav-link\" href=\"#capabilities\">Capabilities</a>\n      <a class=\"nav-link\" href=\"#how-it-works\">How it works</a>\n      <a class=\"nav-link\" href=\"#pricing\">Pricing</a>\n      <a class=\"nav-link\" href=\"#use-cases\">Built for</a>\n    </nav>\n    <div class=\"nav-actions\">\n      <div class=\"nav-account\" id=\"navAccount\">\n        <a href=\"/app\" class=\"nav-profile-btn\" id=\"navProfileBtn\" aria-label=\"Account, sign in\" title=\"Sign in\">\n          <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><circle cx=\"12\" cy=\"8\" r=\"4\"/><path d=\"M4 21c0-4 4-6 8-6s8 2 8 6\"/></svg>\n        </a>\n        <button type=\"button\" class=\"nav-account-btn\" id=\"navAccountBtn\" aria-haspopup=\"true\" aria-expanded=\"false\">\n          <span class=\"nav-avatar\" id=\"navAvatar\">?</span>\n          <span class=\"nav-account-name\" id=\"navAccountName\"></span>\n        </button>\n        <div class=\"nav-account-menu\" id=\"navAccountMenu\" role=\"menu\">\n          <div class=\"nav-account-menu-email\" id=\"navAccountEmail\"></div>\n          <a href=\"/app\" class=\"nav-account-menu-item\" role=\"menuitem\">Open MiniMe</a>\n          <button type=\"button\" class=\"nav-account-menu-item\" id=\"navSignOutBtn\" role=\"menuitem\">Sign out</button>\n        </div>\n      </div>\n      <a href=\"/app\" class=\"btn btn-primary\">Start building</a>\n      <button class=\"nav-toggle\" id=\"navToggle\" aria-label=\"Toggle menu\" aria-expanded=\"false\" aria-controls=\"mobileNav\">\n        <svg width=\"22\" height=\"22\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path id=\"navIconPath\" d=\"M3 6h18M3 12h18M3 18h18\"/></svg>\n      </button>\n    </div>\n  </div>\n  <div class=\"wrap mobile-nav\" id=\"mobileNav\">\n    <a href=\"#capabilities\">Capabilities</a>\n    <a href=\"#how-it-works\">How it works</a>\n    <a href=\"#pricing\">Pricing</a>\n    <a href=\"#use-cases\">Built for</a>\n  </div>\n</header>\n\n<main id=\"main\">\n\n  <!-- ================= HERO ================= -->\n  <section class=\"hero\" id=\"top\">\n    <div class=\"spotlight\" aria-hidden=\"true\"></div>\n    <div class=\"wrap\">\n      <div class=\"hero-top\">\n        <div class=\"hero-top-text\">\n          <div class=\"eyebrow-plate\"><span class=\"dot\"></span> One system, every stage of the build</div>\n          <h1 class=\"hero-h\">From a rough idea to a finished build — carried by one AI, start to finish.</h1>\n          <p class=\"hero-sub\">MiniMe plans the spec, studies the material, and writes the code — holding onto the same idea the whole way through, so nothing gets lost translating between four or five different tools.</p>\n          <div class=\"hero-ctas\">\n            <a href=\"#how-it-works\" class=\"btn btn-ghost\">See how it works</a>\n          </div>\n          <div class=\"mode-chips\" aria-label=\"Reasoning modes\">\n            <span class=\"mode-chip\"><span class=\"sw\"></span>Quick</span>\n            <span class=\"mode-chip\"><span class=\"sw\"></span>Balanced</span>\n            <span class=\"mode-chip locked\"><span class=\"sw\"></span>Deep <span class=\"tier-tag\">Moderate+</span></span>\n          </div>\n        </div>\n\n        <div class=\"hero-visual\">\n          <svg id=\"heroLogo\" class=\"hero-logo-draw\" viewBox=\"0 0 1024 844\" xmlns=\"http://www.w3.org/2000/svg\" aria-hidden=\"true\">\n            <g transform=\"translate(0,844) scale(0.1,-0.1)\">\n              <path id=\"hp1\" d=\"M3395 7019 c-69 -13 -229 -67 -290 -97 -111 -56 -224 -142 -341 -264 c-115 -119 -182 -209 -298 -401 -586 -967 -915 -2135 -894 -3173 5 -237 16 -302 78 -468 99 -267 338 -506 613 -616 157 -62 223 -74 427 -74 157 -1 198 3 270 21 379 96 676 370 789 725 51 161 62 249 71 553 9 320 23 421 81 600 65 200 179 377 352 542 127 121 226 191 497 354 461 277 768 565 1079 1014 23 33 80 116 128 185 123 177 173 238 242 295 128 105 248 159 428 190 292 51 639 -33 930 -225 42 -28 80 -49 85 -48 30 10 -214 363 -366 528 -151 163 -274 255 -419 310 -118 46 -196 60 -340 60 -367 0 -682 -152 -930 -449 -116 -138 -134 -164 -325 -446 -82 -121 -188 -259 -221 -289 -21 -19 -21 -19 -47 5 -34 31 -107 130 -229 309 -220 324 -364 503 -495 613 -153 129 -339 217 -521 246 -77 13 -284 12 -354 0z m333 -720 c105 -51 201 -153 350 -371 180 -264 266 -386 325 -460 37 -48 67 -90 67 -95 0 -4 -94 -69 -208 -143 -261 -171 -394 -274 -547 -425 -278 -275 -441 -562 -524 -925 -53 -229 -71 -394 -71 -665 0 -203 -13 -282 -60 -383 -64 -136 -198 -215 -365 -216 -202 0 -347 104 -405 293 -35 112 -24 563 21 871 90 617 257 1158 527 1710 177 363 317 586 440 701 153 144 300 179 450 108z\"/>\n              <path id=\"hp2\" d=\"M6740 6254 c0 -3 26 -40 58 -82 102 -135 167 -237 287 -447 285 -499 530 -1213 624 -1815 59 -377 75 -612 50 -732 -16 -77 -82 -213 -135 -277 -99 -121 -273 -223 -458 -271 -158 -41 -251 -53 -411 -52 -303 0 -557 76 -738 221 -118 94 -214 253 -250 414 l-15 67 823 0 c516 0 826 4 830 10 3 5 8 83 12 172 12 330 -44 613 -169 863 -198 393 -579 606 -1039 582 -543 -29 -954 -394 -1084 -962 -97 -423 -36 -906 158 -1244 227 -396 655 -678 1147 -757 133 -22 440 -29 586 -15 328 33 624 132 854 286 333 222 522 514 586 905 18 105 14 389 -6 550 -67 529 -162 955 -305 1370 -162 470 -311 718 -549 919 -198 166 -381 251 -626 290 -69 11 -230 15 -230 5z m-358 -1918 c111 -24 182 -71 253 -167 49 -67 103 -218 112 -315 l6 -64 -502 0 -503 0 7 43 c11 65 43 162 78 236 39 82 149 196 228 234 91 45 208 56 321 33z\"/>\n            </g>\n          </svg>\n        </div>\n      </div>\n\n      <div class=\"hero-grid\">\n        <div class=\"sheet reveal\">\n          <div class=\"sheet-titlebar\">\n            <span class=\"lab\">Plan — session view</span>\n            <span class=\"lab\">Rev A</span>\n          </div>\n          <div class=\"msg-row\">\n            <div class=\"avatar user\">\n              <svg width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><circle cx=\"12\" cy=\"8\" r=\"4\"/><path d=\"M4 21c0-4 4-6 8-6s8 2 8 6\"/></svg>\n            </div>\n            <div class=\"bubble\">\n              <p>I want a wall-mounted stand for a Raspberry Pi. Keep parts under $15, and it has to survive a 1&nbsp;m drop.</p>\n            </div>\n          </div>\n          <div class=\"msg-row\">\n            <div class=\"avatar ai\">\n              <svg width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"#fff\" stroke-width=\"1.8\"><path d=\"M12 3v4M12 17v4M3 12h4M17 12h4\"/><circle cx=\"12\" cy=\"12\" r=\"3.2\"/></svg>\n            </div>\n            <div class=\"bubble ai-bubble\">\n              <p>Here's a bracket design that clears both constraints. The stock mount wouldn't have survived the drop test, so I thickened the mounting boss and reran the check — it passes now.</p>\n              <details>\n                <summary class=\"reasoning-toggle\">Show reasoning (3 steps) <svg width=\"11\" height=\"11\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M6 9l6 6 6-6\"/></svg></summary>\n                <div class=\"reasoning-body\">\n                  [1] Checked drop-impact tolerance for the plastic and mount thickness.<br>\n                  [2] Compared three bracket shapes and priced parts for each.<br>\n                  [3] Swapped to cheaper screws and rechecked the total against budget.\n                </div>\n              </details>\n              <div class=\"stat-trio\">\n                <div class=\"stat-block\"><span class=\"k\">Parts cost</span><span class=\"v\">$12.40</span></div>\n                <div class=\"stat-block accent\"><span class=\"k\">Drop rating</span><span class=\"v\">1.2 m</span></div>\n                <div class=\"stat-block\"><span class=\"k\">Print time</span><span class=\"v\">3h 40m</span></div>\n              </div>\n              <div class=\"sheet-footer\">\n                <span class=\"ghost-link\">Continue to Build\n                  <svg width=\"13\" height=\"13\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M5 12h14M13 6l6 6-6 6\"/></svg>\n                </span>\n                <span class=\"lab\">4 stages · same memory</span>\n              </div>\n            </div>\n          </div>\n        </div>\n\n        <div class=\"rail\">\n          <div class=\"rail-card reveal\">\n            <h4>Same memory, every stage</h4>\n            <div class=\"flow-list\">\n              <div class=\"flow-item\">\n                <div class=\"flow-dot\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M4 19l5-14 5 14M6 13h6M14 5l6 14\"/></svg></div>\n                <span class=\"lbl\">Plan</span><span class=\"chk\">carried</span>\n              </div>\n              <div class=\"flow-item\">\n                <div class=\"flow-dot\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><circle cx=\"10\" cy=\"10\" r=\"6\"/><path d=\"M14.5 14.5L20 20\"/></svg></div>\n                <span class=\"lbl\">Research</span><span class=\"chk\">carried</span>\n              </div>\n              <div class=\"flow-item\">\n                <div class=\"flow-dot\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M4 5.5c2.5-1 5-1 8 .3v13c-3-1.3-5.5-1.3-8-.3v-13Z\"/><path d=\"M20 5.5c-2.5-1-5-1-8 .3v13c3-1.3 5.5-1.3 8-.3v-13Z\"/></svg></div>\n                <span class=\"lbl\">Study</span><span class=\"chk\">carried</span>\n              </div>\n              <div class=\"flow-item\">\n                <div class=\"flow-dot\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M8 6L3 12l5 6M16 6l5 6-5 6\"/></svg></div>\n                <span class=\"lbl\">Build</span><span class=\"chk\">carried</span>\n              </div>\n            </div>\n          </div>\n\n          <div class=\"rail-card depth-rail\">\n            <h4>Reasoning depth, on demand</h4>\n            <div class=\"bars\"><i></i><i></i><i></i></div>\n            <div class=\"depth-labels\"><span>Quick</span><span>Balanced</span><span>Deep</span></div>\n          </div>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= OLD WAY / NEW WAY ================= -->\n  <section class=\"tight\">\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">The tab problem</span>\n        <h2>One system instead of five open tabs</h2>\n        <p>Most AI tools pick one job and do it well. The cost shows up in between them — every time you move from planning to research to writing, you're the one re-explaining the context.</p>\n      </div>\n      <div class=\"split-compare reveal\">\n        <div class=\"compare-card old\">\n          <span class=\"compare-label\">The usual way</span>\n          <ul>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M18 6L6 18M6 6l12 12\"/></svg>A planning tool for the spec, a separate app for the reading</li>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M18 6L6 18M6 6l12 12\"/></svg>Copy-pasting context between tools every single time</li>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M18 6L6 18M6 6l12 12\"/></svg>Re-explaining who you are and how you like things done, again</li>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M18 6L6 18M6 6l12 12\"/></svg>A coding assistant that has no idea what the spec said</li>\n          </ul>\n        </div>\n        <div class=\"compare-card new\">\n          <span class=\"compare-label\">The MiniMe way</span>\n          <ul>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>One system across planning, research, study, and code</li>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Start a spec in Plan, finish it in Build — nothing exported by hand</li>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Tell it once how you like things — it holds onto that, silently</li>\n            <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>One correction fixes a wrong guess for good, not just for now</li>\n          </ul>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= CAPABILITIES ================= -->\n  <section id=\"capabilities\">\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">What it actually does</span>\n        <h2>Everything you're building, under one roof</h2>\n        <p>One shared memory behind every capability below — so what you build in one carries straight into the next.</p>\n      </div>\n      <div class=\"cap-carousel reveal\" aria-label=\"MiniMe capabilities\" aria-roledescription=\"carousel\">\n        <div class=\"cap-tabs\" role=\"tablist\" aria-label=\"Choose a capability\">\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"0\">Brain</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"1\">Plan &amp; Blueprint</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"2\">Notebooks &amp; Study</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"3\">Research</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"4\">Build Pipeline</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"5\">Personalization</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"6\">Reasoning Depth</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"7\">Test &amp; Harden</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"8\">Growth</button>\n          <button type=\"button\" class=\"cap-tab\" role=\"tab\" data-index=\"9\">Roles Library</button>\n        </div>\n\n        <div class=\"cap-stage\">\n          <button type=\"button\" class=\"cap-nav cap-nav-prev\" aria-label=\"Previous capability\">\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M15 4l-8 8 8 8\"/></svg>\n          </button>\n          <div class=\"cap-track-viewport\">\n            <div class=\"cap-track\">\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M9 4.5c-3.3 0-5 3-5 6.2 0 2 .9 3.3 2 4.3.8.7 1 1.3 1 2.3v1.2h6v-1.2c0-1 .2-1.6 1-2.3 1.1-1 2-2.3 2-4.3 0-3.2-1.7-6.2-5-6.2Z\"/><path d=\"M9.3 21h5.4M12 4.5V2M9 8.5a3 3 0 0 1 3-3\"/></svg></div>\n                <h3>Brain</h3>\n                <p>A dedicated planning layer reads the task and staffs it with the right specialist model for the job — planning, research, study, or code — while lighter models quietly step in for the simpler parts.</p>\n                <span class=\"cap-tag\">Fable 5.1 · plans, then delegates</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><circle cx=\"12\" cy=\"7\" r=\"2.1\"/><path d=\"M12 9.2L6.5 20M12 9.2L17.5 20M8.7 15.6h6.6\"/></svg></div>\n                <h3>Plan &amp; Blueprint</h3>\n                <p>Turn a rough idea into a build-ready spec — dimensions, materials, and a running parts list — checked for the mistakes that only surface after you've already ordered everything.</p>\n                <span class=\"cap-tag\">Sketch it, spec it, price it</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M4 5.5c2.5-1 5-1 8 .3v13c-3-1.3-5.5-1.3-8-.3v-13Z\"/><path d=\"M20 5.5c-2.5-1-5-1-8 .3v13c3-1.3 5.5-1.3 8-.3v-13Z\"/></svg></div>\n                <h3>Notebooks &amp; Study</h3>\n                <p>Drop in your own sources and get flashcards, a quiz, a mind map, and a narrated overview — built from exactly what you gave it, not some generic internet summary.</p>\n                <span class=\"cap-tag\">Feed it anything, get a study kit</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><circle cx=\"10\" cy=\"10\" r=\"6\"/><path d=\"M14.5 14.5L20 20M7 10h6M7 7.3h3.5\"/></svg></div>\n                <h3>Research</h3>\n                <p>Pulls the parts that matter out of long sources and keeps a live trail of where every claim came from, so you can check it later without re-reading everything.</p>\n                <span class=\"cap-tag\">Read once, cite forever</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M8 6L3 12l5 6M16 6l5 6-5 6\"/></svg></div>\n                <h3>Build Pipeline</h3>\n                <p>Writes, reviews, tests, and hardens code the way a careful engineering team would — the same disciplined pipeline, minus the back-and-forth waiting between every stage of review.</p>\n                <span class=\"cap-tag\">From spec to shipped code</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M12 3a9 9 0 0 1 9 9M12 3a9 9 0 0 0-9 9\"/><path d=\"M6 12a6 6 0 0 1 12 0M9 12a3 3 0 0 1 6 0\"/><path d=\"M12 12v6.5\"/></svg></div>\n                <h3>Personalization</h3>\n                <p>Tell it once that you already know the basics, or that you'd rather get short answers — it holds onto that from day one. Get something wrong? One correction fixes it for good.</p>\n                <span class=\"cap-tag\">It remembers how you work</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M4.5 16a7.5 7.5 0 0 1 15 0\"/><path d=\"M12 16l4.2-5.6\"/><circle cx=\"12\" cy=\"16\" r=\"1.1\" fill=\"currentColor\" stroke=\"none\"/></svg></div>\n                <h3>Reasoning Depth</h3>\n                <p>Quick answers for quick questions, and a deep, multi-pass reasoning mode for the tasks that actually call for it — you choose the depth, or let it decide for you.</p>\n                <span class=\"cap-tag\">Choose how hard it thinks</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M9 3h6l1 3h3v3l-3 1v8a2 2 0 0 1-2 2h-6a2 2 0 0 1-2-2v-8l-3-1V6h3l1-3Z\"/><path d=\"M10 12.5l1.6 1.6L14.5 11\"/></svg></div>\n                <h3>Test &amp; Harden</h3>\n                <p>Generated code runs through automated checks and a hardening pass before it's handed back — the same discipline a careful reviewer would apply, minus the wait between each round.</p>\n                <span class=\"cap-tag\">Reviewed before it reaches you</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M4 19h16M6 19V11l4-4 4 3 4-6\"/></svg></div>\n                <h3>Growth</h3>\n                <p>A running view of how a project — or your own skills — develop over time: what's shipped, what's been studied, and what's worth tackling next on the list.</p>\n                <span class=\"cap-tag\">See your own progress</span>\n              </div>\n              <div class=\"cap-card\">\n                <div class=\"cap-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><circle cx=\"8\" cy=\"8\" r=\"3\"/><circle cx=\"17\" cy=\"7\" r=\"2.4\"/><path d=\"M3 20c0-3 2.2-5 5-5s5 2 5 5M14.5 20c.3-2.4 1.8-4 4-4.3\"/></svg></div>\n                <h3>Roles Library</h3>\n                <p>Browse the specialist roles MiniMe can staff onto any task — reviewers, researchers, and more — and see exactly who's working on what, instead of a black box.</p>\n                <span class=\"cap-tag\">Every specialist, visible</span>\n              </div>\n            </div>\n          </div>\n          <button type=\"button\" class=\"cap-nav cap-nav-next\" aria-label=\"Next capability\">\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M9 4l8 8-8 8\"/></svg>\n          </button>\n        </div>\n\n        <div class=\"cap-dots\">\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Brain\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Plan &amp; Blueprint\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Notebooks &amp; Study\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Research\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Build Pipeline\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Personalization\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Reasoning Depth\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Test &amp; Harden\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Growth\"></button>\n          <button type=\"button\" class=\"cap-dot\" aria-label=\"Go to Roles Library\"></button>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= HOW IT WORKS ================= -->\n  <section id=\"how-it-works\" class=\"tight\">\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">How it works</span>\n        <h2>Four stages, one running thread</h2>\n        <p>Nothing here is a separate app you switch into — it's one conversation that keeps its own memory as it moves.</p>\n      </div>\n      <div class=\"steps reveal\">\n        <div class=\"step\">\n          <span class=\"step-num\">01</span>\n          <h3>Tell it what you need</h3>\n          <p>In plain language — a spec, a source to study, a bug to fix. No special syntax to learn.</p>\n        </div>\n        <div class=\"step\">\n          <span class=\"step-num\">02</span>\n          <h3>It plans the approach</h3>\n          <p>MiniMe decides how much structure the task actually needs, instead of running one fixed routine for everything.</p>\n        </div>\n        <div class=\"step\">\n          <span class=\"step-num\">03</span>\n          <h3>It works across stages</h3>\n          <p>Planning, research, and building stay connected — a decision made early carries through to the last step.</p>\n        </div>\n        <div class=\"step\">\n          <span class=\"step-num\">04</span>\n          <h3>You refine, it remembers</h3>\n          <p>Push back, correct a detail, ask for more depth — it adjusts, and keeps that preference for next time.</p>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= USE CASES ================= -->\n  <section id=\"use-cases\">\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">Built for</span>\n        <h2>People who make things</h2>\n      </div>\n      <div class=\"use-grid\">\n        <div class=\"use-card reveal\">\n          <div class=\"use-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.6\"><path d=\"M14.7 3.2a4.6 4.6 0 0 0-5.9 5.9L3 15l6 6 5.9-5.8a4.6 4.6 0 0 0 5.9-5.9l-3.3 3.3-2.4-2.4Z\"/></svg></div>\n          <h3>Makers &amp; hobbyists</h3>\n          <p>Take a weekend project from a sketch on a napkin to a bracket that actually survives being dropped.</p>\n        </div>\n        <div class=\"use-card reveal\">\n          <div class=\"use-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.6\"><path d=\"M12 4L22 9l-10 5L2 9l10-5Z\"/><path d=\"M6 11.5V16c0 1.5 3 3 6 3s6-1.5 6-3v-4.5\"/></svg></div>\n          <h3>Students &amp; researchers</h3>\n          <p>Turn a stack of readings into flashcards, a mind map, and a study guide that's actually yours.</p>\n        </div>\n        <div class=\"use-card reveal\">\n          <div class=\"use-icon\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.6\"><path d=\"M12 2.5c3 2 5 6 5 9.7 0 2-1 4-2 5l-3 3-3-3c-1-1-2-3-2-5 0-3.7 2-7.7 5-9.7Z\"/><path d=\"M9 16l-3 2.5.7 3 3-1.2M15 16l3 2.5-.7 3-3-1.2\"/></svg></div>\n          <h3>Developers &amp; founders</h3>\n          <p>Go from a product idea to a working prototype without stitching together five different tools by hand.</p>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= PRICING ================= -->\n  <section id=\"pricing\" class=\"tight\">\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">Pricing</span>\n        <h2>Three tiers, matched to how deep you need to go</h2>\n        <p>Every tier gets the full set of capabilities. What changes is how much reasoning depth, project size, priority, and which models — including Brain — are doing the work. Hover a card (or tap it, on mobile) to see what each package actually runs on.</p>\n      </div>\n\n      <div class=\"price-toggle-row reveal\">\n        <button class=\"switch\" id=\"billingSwitch\" role=\"switch\" aria-checked=\"false\" aria-label=\"Toggle yearly billing\">\n          <span class=\"thumb\"></span>\n        </button>\n        <span class=\"toggle-label\" id=\"toggleLabel\">Billed monthly</span>\n        <span class=\"save-tag\">Save ~20% yearly</span>\n      </div>\n\n      <div class=\"price-grid reveal\">\n\n        <!-- BASIC -->\n        <div class=\"price-card-flip\" tabindex=\"0\" aria-label=\"Basic plan — hover or tap to see pricing and models\">\n          <div class=\"price-card-inner\">\n            <div class=\"price-card-face front\">\n              <span class=\"rev-tag\">Rev A</span>\n              <h3>Basic</h3>\n              <p class=\"tier-desc\">Everyday tasks — quick research, study help, simple planning and code — on fast, efficient reasoning.</p>\n              <ul class=\"price-features\">\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Every capability, full access</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Quick &amp; Balanced reasoning modes</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Fast, efficient models — Brain not included</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Personal profile &amp; memory</li>\n              </ul>\n              <div class=\"tier-fit\"><span class=\"k\">Best for</span><span class=\"v\">Weekend builds, quick research runs, and everyday study help.</span></div>\n              <span class=\"flip-hint\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M4 4v5h5M20 20v-5h-5\"/><path d=\"M4.5 15a8 8 0 0 0 14.4 3M19.5 9A8 8 0 0 0 5.1 6\"/></svg><span class=\"hint-tap\">Tap to see pricing &amp; start</span></span>\n            </div>\n            <div class=\"price-card-face back\">\n              <span class=\"back-label\">Basic — what's inside</span>\n              <div class=\"price-num\"><span class=\"amt\" data-monthly=\"9\" data-yearly=\"7\">$9</span><span class=\"per\">/mo</span></div>\n              <div class=\"billed-note\"></div>\n              <div class=\"price-back-rows\">\n                <div class=\"price-back-row\"><span class=\"k\">Brain</span><span class=\"v\">Not included on Basic.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Models</span><span class=\"v\">Fast, efficient models only — no Fable&nbsp;5 or other frontier/best-in-class models.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Response</span><span class=\"v\">Always real-time, standard API.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Token limit</span><span class=\"v\">1,000,000 tokens / mo</span></div>\n              </div>\n              <button type=\"button\" class=\"btn btn-ghost btn-block js-buy-btn\" data-plan=\"Basic\">Start with Basic</button>\n            </div>\n          </div>\n        </div>\n\n        <!-- MODERATE -->\n        <div class=\"price-card-flip hi\" tabindex=\"0\" aria-label=\"Moderate plan — hover or tap to see pricing and models\">\n          <div class=\"price-card-inner\">\n            <div class=\"price-card-face front\">\n              <span class=\"recommended-tag\">Recommended</span>\n              <span class=\"rev-tag\">Rev B</span>\n              <h3>Moderate</h3>\n              <p class=\"tier-desc\">Real projects — longer builds, more complex specs, deeper research — with room to go further.</p>\n              <ul class=\"price-features\">\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Everything in Basic</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Deep reasoning + Brain, batch-priced</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Broader best/mid-class model roster</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Priority processing</li>\n              </ul>\n              <div class=\"tier-fit\"><span class=\"k\">Best for</span><span class=\"v\">Real client work — multi-stage builds where Brain's planning actually pays off.</span></div>\n              <span class=\"flip-hint\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M4 4v5h5M20 20v-5h-5\"/><path d=\"M4.5 15a8 8 0 0 0 14.4 3M19.5 9A8 8 0 0 0 5.1 6\"/></svg><span class=\"hint-tap\">Tap to see pricing &amp; start</span></span>\n            </div>\n            <div class=\"price-card-face back\">\n              <span class=\"back-label\">Moderate — what's inside</span>\n              <div class=\"price-num\"><span class=\"amt\" data-monthly=\"29\" data-yearly=\"23\">$29</span><span class=\"per\">/mo</span></div>\n              <div class=\"billed-note\"></div>\n              <div class=\"price-back-rows\">\n                <div class=\"price-back-row\"><span class=\"k\">Brain</span><span class=\"v\">Fable&nbsp;5 engages for Deep-mode tasks, running on the batch API — lower cost, but not real-time; responses land after a short processing window.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Models</span><span class=\"v\">Adds a shortlist of best- and mid-class models across planning, research, study, and code.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Response</span><span class=\"v\">Real-time for everyday models; Brain/Fable&nbsp;5 tasks queue on batch.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Token limit</span><span class=\"v\">4,000,000 tokens / mo<span class=\"token-badge\">4× Basic</span></span></div>\n              </div>\n              <button type=\"button\" class=\"btn btn-primary btn-block js-buy-btn\" data-plan=\"Moderate\">Start with Moderate</button>\n            </div>\n          </div>\n        </div>\n\n        <!-- ULTIMATE -->\n        <div class=\"price-card-flip\" tabindex=\"0\" aria-label=\"Ultimate plan — hover or tap to see pricing and models\">\n          <div class=\"price-card-inner\">\n            <div class=\"price-card-face front\">\n              <span class=\"rev-tag\">Rev C</span>\n              <h3>Ultimate</h3>\n              <p class=\"tier-desc\">The hardest tasks — frontier-level reasoning with the most freedom to think before committing to a plan.</p>\n              <ul class=\"price-features\">\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Everything in Moderate</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Full Brain access — Fable 5.1, real-time</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>Every top-tier model, every sector</li>\n                <li><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 6L9 17l-5-5\"/></svg>No project-size ceiling</li>\n              </ul>\n              <div class=\"tier-fit\"><span class=\"k\">Best for</span><span class=\"v\">Production-critical builds and the hardest problems, where you want frontier reasoning by default.</span></div>\n              <span class=\"flip-hint\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M4 4v5h5M20 20v-5h-5\"/><path d=\"M4.5 15a8 8 0 0 0 14.4 3M19.5 9A8 8 0 0 0 5.1 6\"/></svg><span class=\"hint-tap\">Tap to see pricing &amp; start</span></span>\n            </div>\n            <div class=\"price-card-face back\">\n              <span class=\"back-label\">Ultimate — what's inside</span>\n              <div class=\"price-num\"><span class=\"amt\" data-monthly=\"79\" data-yearly=\"63\">$79</span><span class=\"per\">/mo</span></div>\n              <div class=\"billed-note\"></div>\n              <div class=\"price-back-rows\">\n                <div class=\"price-back-row\"><span class=\"k\">Brain</span><span class=\"v\">Full access — Fable&nbsp;5 / 5.1 as the primary orchestrating brain, on every task that calls for it.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Models</span><span class=\"v\">Every latest high- and mid-tier model, across every sector — the full roster, no gating.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Response</span><span class=\"v\">Real-time by default. Batch stays available too, for lower-cost tasks — Ultimate includes everything Moderate has.</span></div>\n                <div class=\"price-back-row\"><span class=\"k\">Token limit</span><span class=\"v\">12,000,000 tokens / mo<span class=\"token-badge\">3× Moderate</span></span></div>\n              </div>\n              <button type=\"button\" class=\"btn btn-ghost btn-block js-buy-btn\" data-plan=\"Ultimate\">Start with Ultimate</button>\n            </div>\n          </div>\n        </div>\n\n      </div>\n      <p class=\"pricing-note reveal\">Illustrative pricing shown for planning purposes — final pricing is confirmed at launch. Model names, routing, and token limits are subject to change as Brain and the wider model roster roll out.</p>\n    </div>\n  </section>\n\n  <!-- ================= TRUST ================= -->\n  <section>\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">Privacy</span>\n        <h2>Your ideas stay yours</h2>\n      </div>\n      <div class=\"trust-grid reveal\">\n        <div class=\"trust-item\">\n          <h4>Private by default</h4>\n          <p>Every plan, spec, and line of code stays private to your account, not pooled with anyone else's.</p>\n        </div>\n        <div class=\"trust-item\">\n          <h4>You're in control of what it remembers</h4>\n          <p>See, edit, or delete anything MiniMe has learned about how you work, any time, from your account settings.</p>\n        </div>\n        <div class=\"trust-item\">\n          <h4>Never trained on without consent</h4>\n          <p>Your conversations aren't used to train models unless you explicitly choose to share them.</p>\n        </div>\n        <div class=\"trust-item\">\n          <h4>Proven before it ships</h4>\n          <p>New models and capabilities — Brain included — are scored against real historical outcomes before any real user sees them. Nothing goes live on a guess.</p>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= HORIZON ================= -->\n  <section class=\"tight\">\n    <div class=\"wrap\">\n      <div class=\"section-head reveal\">\n        <span class=\"section-tag\">On the horizon</span>\n        <h2>Always building toward more</h2>\n      </div>\n      <div class=\"horizon-row reveal\">\n        <div class=\"horizon-item\">\n          <div>\n            <h4>Adaptive planning for Ultimate</h4>\n            <p>A planning layer that can pause mid-task, reconsider its approach, and replan — instead of following one fixed routine.</p>\n          </div>\n          <span class=\"horizon-tag\">In the works</span>\n        </div>\n        <div class=\"horizon-item\">\n          <div>\n            <h4>A local workspace companion</h4>\n            <p>An opt-in companion that works directly with files on your own machine, sandboxed and permission-gated.</p>\n          </div>\n          <span class=\"horizon-tag\">In the works</span>\n        </div>\n        <div class=\"horizon-item\">\n          <div>\n            <h4>Closed-loop safety monitoring</h4>\n            <p>A live watcher that follows each stage of a build as it happens, so a problem gets caught while a task is still running — not discovered after the fact.</p>\n          </div>\n          <span class=\"horizon-tag\">In the works</span>\n        </div>\n        <div class=\"horizon-item\">\n          <div>\n            <h4>Sandboxed, human-reviewed code execution</h4>\n            <p>Generated code that needs real-world access always runs against a sandbox first, with no real credentials reachable — and any new integration gets a human sign-off before it's ever wired to the real thing.</p>\n          </div>\n          <span class=\"horizon-tag\">In the works</span>\n        </div>\n        <div class=\"horizon-item\">\n          <div>\n            <h4>Team accounts</h4>\n            <p>Shared projects and a common memory for small teams building together.</p>\n          </div>\n          <span class=\"horizon-tag\">Exploring</span>\n        </div>\n      </div>\n    </div>\n  </section>\n\n  <!-- ================= FINAL CTA ================= -->\n  <section>\n    <div class=\"wrap\">\n      <div class=\"cta-band reveal\">\n        <div>\n          <h2>Ready to build your first idea?</h2>\n          <p>Start on Basic in a couple of minutes — upgrade whenever a task actually needs more.</p>\n        </div>\n        <div class=\"cta-band-actions\">\n          <a href=\"/app\" class=\"btn btn-primary\">Start building</a>\n          <span class=\"cta-fine\">No credit card required to start</span>\n        </div>\n      </div>\n      <div class=\"pay-row reveal\">\n        <div class=\"pay-chip\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><rect x=\"3\" y=\"6\" width=\"18\" height=\"13\" rx=\"2\"/><path d=\"M3 10.5h18M7 15h4\"/></svg>Visa, Mastercard, Amex &amp; PayPal</div>\n        <div class=\"pay-chip\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3Z\"/></svg>Encrypted checkout — card details never touch our servers</div>\n        <div class=\"pay-chip\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M8 12.5l2.5 2.5L16 9.5\"/></svg>Cancel anytime, no lock-in contracts</div>\n        <div class=\"pay-chip\"><svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\"><path d=\"M4 12h16M4 12l4-4M4 12l4 4\"/></svg>Switch or refund a wrong charge within 14 days</div>\n      </div>\n    </div>\n  </section>\n\n</main>\n\n<footer>\n  <div class=\"wrap\">\n    <div class=\"foot-grid\">\n      <div class=\"foot-brand\">\n        <a href=\"#top\" class=\"brand\">\n          <svg class=\"brand-mark\" style=\"width:22px;height:22px;\" viewBox=\"0 0 1024 844\" xmlns=\"http://www.w3.org/2000/svg\" fill=\"currentColor\" aria-hidden=\"true\">\n            <g transform=\"translate(0,844) scale(0.1,-0.1)\">\n              <path d=\"M3395 7019 c-69 -13 -229 -67 -290 -97 -111 -56 -224 -142 -341 -264 c-115 -119 -182 -209 -298 -401 -586 -967 -915 -2135 -894 -3173 5 -237 16 -302 78 -468 99 -267 338 -506 613 -616 157 -62 223 -74 427 -74 157 -1 198 3 270 21 379 96 676 370 789 725 51 161 62 249 71 553 9 320 23 421 81 600 65 200 179 377 352 542 127 121 226 191 497 354 461 277 768 565 1079 1014 23 33 80 116 128 185 123 177 173 238 242 295 128 105 248 159 428 190 292 51 639 -33 930 -225 42 -28 80 -49 85 -48 30 10 -214 363 -366 528 -151 163 -274 255 -419 310 -118 46 -196 60 -340 60 -367 0 -682 -152 -930 -449 -116 -138 -134 -164 -325 -446 -82 -121 -188 -259 -221 -289 -21 -19 -21 -19 -47 5 -34 31 -107 130 -229 309 -220 324 -364 503 -495 613 -153 129 -339 217 -521 246 -77 13 -284 12 -354 0z m333 -720 c105 -51 201 -153 350 -371 180 -264 266 -386 325 -460 37 -48 67 -90 67 -95 0 -4 -94 -69 -208 -143 -261 -171 -394 -274 -547 -425 -278 -275 -441 -562 -524 -925 -53 -229 -71 -394 -71 -665 0 -203 -13 -282 -60 -383 -64 -136 -198 -215 -365 -216 -202 0 -347 104 -405 293 -35 112 -24 563 21 871 90 617 257 1158 527 1710 177 363 317 586 440 701 153 144 300 179 450 108z\"/>\n              <path d=\"M6740 6254 c0 -3 26 -40 58 -82 102 -135 167 -237 287 -447 285 -499 530 -1213 624 -1815 59 -377 75 -612 50 -732 -16 -77 -82 -213 -135 -277 -99 -121 -273 -223 -458 -271 -158 -41 -251 -53 -411 -52 -303 0 -557 76 -738 221 -118 94 -214 253 -250 414 l-15 67 823 0 c516 0 826 4 830 10 3 5 8 83 12 172 12 330 -44 613 -169 863 -198 393 -579 606 -1039 582 -543 -29 -954 -394 -1084 -962 -97 -423 -36 -906 158 -1244 227 -396 655 -678 1147 -757 133 -22 440 -29 586 -15 328 33 624 132 854 286 333 222 522 514 586 905 18 105 14 389 -6 550 -67 529 -162 955 -305 1370 -162 470 -311 718 -549 919 -198 166 -381 251 -626 290 -69 11 -230 15 -230 5z m-358 -1918 c111 -24 182 -71 253 -167 49 -67 103 -218 112 -315 l6 -64 -502 0 -503 0 7 43 c11 65 43 162 78 236 39 82 149 196 228 234 91 45 208 56 321 33z\"/>\n            </g>\n          </svg>\n          <span class=\"brand-word\">Mini<em>Me</em></span>\n        </a>\n        <p>One AI system carrying an idea through planning, research, study, and code.</p>\n      </div>\n      <div class=\"foot-col\">\n        <h5>Product</h5>\n        <ul>\n          <li><a href=\"#capabilities\">Capabilities</a></li>\n          <li><a href=\"#how-it-works\">How it works</a></li>\n          <li><a href=\"#pricing\">Pricing</a></li>\n        </ul>\n      </div>\n      <div class=\"foot-col\">\n        <h5>Company</h5>\n        <ul>\n          <li><a href=\"#top\">About</a></li>\n          <li><a href=\"#use-cases\">Built for</a></li>\n          <li><a href=\"#top\">Contact</a></li>\n        </ul>\n      </div>\n      <div class=\"foot-col\">\n        <h5>Legal</h5>\n        <ul>\n          <li><a href=\"#top\">Privacy policy</a></li>\n          <li><a href=\"#top\">Terms of service</a></li>\n        </ul>\n      </div>\n    </div>\n    <div class=\"foot-bottom\">\n      <p>© 2026 MiniMe. All rights reserved.</p>\n      <span class=\"foot-plate\">Sheet 01 / Rev launch</span>\n    </div>\n  </div>\n</footer>\n\n<div class=\"checkout-overlay\" id=\"checkoutOverlay\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"checkoutTitle\">\n  <div class=\"checkout-card\">\n    <button type=\"button\" class=\"checkout-close\" id=\"checkoutClose\" aria-label=\"Close\">\n      <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M18 6L6 18M6 6l12 12\"/></svg>\n    </button>\n    <div class=\"checkout-head\">\n      <span class=\"checkout-tag\">Checkout preview</span>\n      <h3 id=\"checkoutTitle\">Start with <span id=\"checkoutPlanName\">Moderate</span></h3>\n      <p class=\"checkout-sub\">This is a mockup of the purchase flow, not a live payment yet.</p>\n    </div>\n    <div class=\"checkout-summary\">\n      <div class=\"checkout-summary-row\"><span>Plan</span><span id=\"checkoutSummaryPlan\">Moderate</span></div>\n      <div class=\"checkout-summary-row\"><span>Billing</span><span id=\"checkoutSummaryBilling\">Billed monthly</span></div>\n      <div class=\"checkout-summary-row total\"><span>Total due today</span><span id=\"checkoutSummaryPrice\">$29</span></div>\n    </div>\n    <form class=\"checkout-form\" id=\"checkoutForm\">\n      <div class=\"checkout-field\">\n        <label for=\"checkoutEmail\">Email</label>\n        <input type=\"email\" id=\"checkoutEmail\" placeholder=\"you@example.com\" autocomplete=\"email\" />\n      </div>\n      <div class=\"checkout-field\">\n        <label for=\"checkoutCardName\">Name on card</label>\n        <input type=\"text\" id=\"checkoutCardName\" placeholder=\"Jane Doe\" autocomplete=\"cc-name\" />\n      </div>\n      <div class=\"checkout-field\">\n        <label for=\"checkoutCardNumber\">Card number</label>\n        <input type=\"text\" id=\"checkoutCardNumber\" placeholder=\"4242 4242 4242 4242\" inputmode=\"numeric\" autocomplete=\"cc-number\" />\n      </div>\n      <div class=\"checkout-field-row\">\n        <div class=\"checkout-field\">\n          <label for=\"checkoutExpiry\">Expiry</label>\n          <input type=\"text\" id=\"checkoutExpiry\" placeholder=\"MM / YY\" autocomplete=\"cc-exp\" />\n        </div>\n        <div class=\"checkout-field\">\n          <label for=\"checkoutCvc\">CVC</label>\n          <input type=\"text\" id=\"checkoutCvc\" placeholder=\"CVC\" inputmode=\"numeric\" autocomplete=\"cc-csc\" />\n        </div>\n      </div>\n      <button type=\"submit\" class=\"btn btn-primary btn-block checkout-submit\" id=\"checkoutSubmit\" disabled title=\"Payments aren't available yet\">Not available yet</button>\n      <p class=\"checkout-fine\">Payments aren't live yet, this is a layout preview so checkout is ready to wire up later. No card is charged.</p>\n    </form>\n  </div>\n</div>\n\n";

export default function LandingPage() {
  const containerRef = useRef(null);
  const initedRef = useRef(false);

  useEffect(() => {
    // The template's own two <script> blocks below are vanilla DOM code
    // written to run exactly once, top-to-bottom, against a freshly
    // parsed document -- e.g. the capabilities carousel clones its own
    // slides into the track on first run, and re-running that against
    // its own output would clone the clones. React 18 StrictMode (on,
    // see next.config.js) deliberately mounts -> cleans up -> re-mounts
    // every effect once in development to surface missing cleanup, but
    // the DOM this effect touches isn't reset in between those two
    // runs. Since this page's only exit is a full browser navigation
    // (every CTA above is a plain <a> to "/", not client-side routing)
    // rather than an in-app unmount, guarding against a second
    // real run -- rather than writing a full teardown for interval
    // timers, observers, etc. that will never actually fire before the
    // tab navigates away -- is the right fix here: it keeps the script
    // below running exactly once per real page load, in dev and prod
    // alike, unmodified from the template.
    if (initedRef.current) return;
    initedRef.current = true;

    (function(){
      // Cyberpunk cursor spotlight (hero only, ignores reduced-motion users' preference for less motion by only reacting to real input)
      var hero = document.querySelector('.hero');
      if (hero && window.matchMedia && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        hero.addEventListener('mousemove', function (e) {
          var r = hero.getBoundingClientRect();
          document.documentElement.style.setProperty('--mx', (e.clientX) + 'px');
          document.documentElement.style.setProperty('--my', (e.clientY) + 'px');
        });
      }

      // Header energy-line intensifies on scroll
      var header = document.querySelector('header');
      if (header) {
        var onScroll = function () {
          header.classList.toggle('scrolled', window.scrollY > 40);
        };
        window.addEventListener('scroll', onScroll, { passive: true });
        onScroll();
      }

      // Remove boot-flash overlay from the DOM once its animation finishes
      var bootFlash = document.querySelector('.boot-flash');
      if (bootFlash) {
        setTimeout(function () { bootFlash.remove(); }, 1300);
      }

      // Mobile nav toggle
      var toggle = document.getElementById('navToggle');
      var mobileNav = document.getElementById('mobileNav');
      if (toggle && mobileNav) {
        var iconPath = document.getElementById('navIconPath');
        toggle.addEventListener('click', function () {
          var isOpen = mobileNav.classList.toggle('open');
          toggle.setAttribute('aria-expanded', String(isOpen));
          if (iconPath) iconPath.setAttribute('d', isOpen ? 'M6 6l12 12M18 6L6 18' : 'M3 6h18M3 12h18M3 18h18');
        });
        mobileNav.querySelectorAll('a').forEach(function (a) {
          a.addEventListener('click', function () { mobileNav.classList.remove('open'); toggle.setAttribute('aria-expanded','false'); });
        });
      }

      // Reveal on scroll
      var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      var revealEls = document.querySelectorAll('.reveal');
      if (reduceMotion) {
        revealEls.forEach(function (el) { el.classList.add('is-visible'); });
      } else if ('IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              entry.target.classList.add('is-visible');
              io.unobserve(entry.target);
            }
          });
        }, { threshold: 0.12 });
        revealEls.forEach(function (el) { io.observe(el); });
      } else {
        revealEls.forEach(function (el) { el.classList.add('is-visible'); });
      }

      // Hero logo pen-draw animation (plays once on load)
      function animateLogo() {
        var p1 = document.getElementById('hp1');
        var p2 = document.getElementById('hp2');
        if (!p1 || !p2) return;
        if (reduceMotion) {
          [p1, p2].forEach(function (p) { p.style.fill = '#FF2052'; p.style.stroke = 'none'; });
          return;
        }
        [p1, p2].forEach(function (path, i) {
          var length = path.getTotalLength();
          path.style.fill = '#FF2052';
          path.style.fillOpacity = '0';
          path.style.stroke = '#FF2052';
          path.style.strokeWidth = '3';
          path.style.strokeLinecap = 'round';
          path.style.strokeLinejoin = 'round';
          path.style.strokeDasharray = length;
          path.style.strokeDashoffset = length;
          var drawDuration = 1300, drawDelay = i * 250;
          path.animate(
            [{ strokeDashoffset: length }, { strokeDashoffset: 0 }],
            { duration: drawDuration, delay: drawDelay, easing: 'cubic-bezier(.65,0,.35,1)', fill: 'forwards' }
          );
          path.animate(
            [{ fillOpacity: 0, strokeOpacity: 1 }, { fillOpacity: 1, strokeOpacity: 0 }],
            { duration: 350, delay: drawDelay + drawDuration - 150, easing: 'ease-out', fill: 'forwards' }
          );
        });
      }
      animateLogo();

      // Pricing billing toggle
      var billingSwitch = document.getElementById('billingSwitch');
      var toggleLabel = document.getElementById('toggleLabel');
      var amounts = document.querySelectorAll('.amt');
      var billedNotes = document.querySelectorAll('.billed-note');
      var isYearly = false;
      if (billingSwitch) {
        billingSwitch.addEventListener('click', function () {
          isYearly = !isYearly;
          billingSwitch.classList.toggle('on', isYearly);
          billingSwitch.setAttribute('aria-checked', String(isYearly));
          toggleLabel.textContent = isYearly ? 'Billed yearly' : 'Billed monthly';
          amounts.forEach(function (el) {
            el.style.opacity = '0';
            setTimeout(function () {
              var val = isYearly ? el.getAttribute('data-yearly') : el.getAttribute('data-monthly');
              el.textContent = '$' + val;
              el.style.opacity = '1';
            }, 160);
          });
          billedNotes.forEach(function (el) {
            el.textContent = isYearly ? 'billed annually' : '';
          });
        });
      }

      // Pricing flip cards — tap/keyboard support alongside the hover flip
      document.querySelectorAll('.price-card-flip').forEach(function (card) {
        card.addEventListener('click', function (e) {
          if (e.target.closest('a, button')) return;
          card.classList.toggle('flipped');
        });
        card.addEventListener('keydown', function (e) {
          if ((e.key === 'Enter' || e.key === ' ') && !e.target.closest('a, button')) {
            e.preventDefault();
            card.classList.toggle('flipped');
          }
        });
      });

      // Sync pricing card heights: measure the tallest front/back face content
      // (off-DOM, so the live flip transform is never disturbed) and set that
      // as an explicit px height on every card, so none of the three ever
      // clips its content and all three stay visually matched.
      function syncPriceCardHeights() {
        var flips = document.querySelectorAll('.price-card-flip');
        if (!flips.length) return;

        flips.forEach(function (f) { f.style.height = ''; });

        var measurer = document.createElement('div');
        measurer.style.position = 'absolute';
        measurer.style.visibility = 'hidden';
        measurer.style.pointerEvents = 'none';
        measurer.style.top = '0';
        measurer.style.left = '-9999px';
        document.body.appendChild(measurer);

        var maxH = 0;
        flips.forEach(function (flip) {
          var width = flip.getBoundingClientRect().width;
          flip.querySelectorAll('.price-card-face').forEach(function (face) {
            var clone = face.cloneNode(true);
            clone.style.position = 'static';
            clone.style.transform = 'none';
            clone.style.width = width + 'px';
            measurer.appendChild(clone);
            var h = clone.getBoundingClientRect().height;
            if (h > maxH) maxH = h;
            measurer.removeChild(clone);
          });
        });
        document.body.removeChild(measurer);

        if (maxH > 0) {
          var finalH = Math.ceil(maxH) + 'px';
          flips.forEach(function (f) { f.style.height = finalH; });
        }
      }

      function debounce(fn, wait) {
        var t;
        return function () {
          clearTimeout(t);
          t = setTimeout(fn, wait);
        };
      }

      window.addEventListener('load', syncPriceCardHeights);
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(syncPriceCardHeights);
      }
      window.addEventListener('resize', debounce(syncPriceCardHeights, 150));
    })();

      // ================= NAV PROFILE / ACCOUNT =================
      // Reads the same cookie-backed Supabase session "/app" uses (see the
      // `supabase` import at the top of this file) so a signed-in visitor
      // sees their profile here too -- this is also what a later
      // transaction system will use to know who's buying a package.
      (function () {
        var navAccount = document.getElementById('navAccount');
        var navAvatar = document.getElementById('navAvatar');
        var navAccountName = document.getElementById('navAccountName');
        var navAccountEmail = document.getElementById('navAccountEmail');
        var navAccountBtn = document.getElementById('navAccountBtn');
        var navAccountMenu = document.getElementById('navAccountMenu');
        var navSignOutBtn = document.getElementById('navSignOutBtn');
        if (!navAccount) return;

        function closeMenu() {
          if (!navAccountMenu) return;
          navAccountMenu.classList.remove('open');
          if (navAccountBtn) navAccountBtn.setAttribute('aria-expanded', 'false');
        }

        function renderUser(user) {
          if (!user) {
            navAccount.classList.remove('is-signed-in');
            closeMenu();
            return;
          }
          var meta = user.user_metadata || {};
          var displayName = meta.full_name || meta.name || user.email || 'Account';
          var avatarUrl = meta.avatar_url || meta.picture;
          if (navAccountName) navAccountName.textContent = displayName;
          if (navAccountEmail) navAccountEmail.textContent = user.email || '';
          if (navAvatar) {
            navAvatar.innerHTML = avatarUrl
              ? '<img src="' + avatarUrl + '" alt="" />'
              : '';
            if (!avatarUrl) navAvatar.textContent = (displayName || '?').charAt(0).toUpperCase();
          }
          navAccount.classList.add('is-signed-in');
        }

        supabase.auth.getUser().then(function (res) {
          renderUser(res && res.data ? res.data.user : null);
        }).catch(function () { /* not signed in / request failed — leave signed-out state */ });

        supabase.auth.onAuthStateChange(function (_event, session) {
          renderUser(session ? session.user : null);
        });

        if (navAccountBtn && navAccountMenu) {
          navAccountBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            var isOpen = navAccountMenu.classList.toggle('open');
            navAccountBtn.setAttribute('aria-expanded', String(isOpen));
          });
          document.addEventListener('click', function (e) {
            if (navAccountMenu.classList.contains('open') && !navAccount.contains(e.target)) closeMenu();
          });
          document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') closeMenu();
          });
        }

        if (navSignOutBtn) {
          navSignOutBtn.addEventListener('click', function () {
            closeMenu();
            supabase.auth.signOut().then(function () {
              clearLocalAppState();   // BUGFIX — see lib/clearLocalAppState.js
            });
          });
        }
      })();

      // ================= CHECKOUT (mock) =================
      // The pricing section is for buying, not for entering the app, so
      // "Start with <tier>" no longer links to "/app" -- it opens this
      // card instead. This is a layout-only preview: the submit button
      // stays disabled ("Not available yet") until real payments exist.
      (function () {
        var overlay = document.getElementById('checkoutOverlay');
        var closeBtn = document.getElementById('checkoutClose');
        var form = document.getElementById('checkoutForm');
        var emailInput = document.getElementById('checkoutEmail');
        var planNameEl = document.getElementById('checkoutPlanName');
        var summaryPlanEl = document.getElementById('checkoutSummaryPlan');
        var summaryBillingEl = document.getElementById('checkoutSummaryBilling');
        var summaryPriceEl = document.getElementById('checkoutSummaryPrice');
        if (!overlay) return;

        var lastFocused = null;

        function openCheckout(plan, price, billingLabel) {
          if (planNameEl) planNameEl.textContent = plan;
          if (summaryPlanEl) summaryPlanEl.textContent = plan;
          if (summaryBillingEl) summaryBillingEl.textContent = billingLabel;
          if (summaryPriceEl) summaryPriceEl.textContent = price;
          lastFocused = document.activeElement;
          overlay.classList.add('open');
          document.body.style.overflow = 'hidden';
          if (closeBtn) closeBtn.focus();
        }

        function closeCheckout() {
          overlay.classList.remove('open');
          document.body.style.overflow = '';
          if (lastFocused && lastFocused.focus) lastFocused.focus();
        }

        document.querySelectorAll('.js-buy-btn').forEach(function (btn) {
          btn.addEventListener('click', function (e) {
            e.preventDefault();
            var plan = btn.getAttribute('data-plan') || 'Plan';
            var card = btn.closest('.price-card-flip');
            var amountEl = card ? card.querySelector('.amt') : null;
            var price = amountEl ? amountEl.textContent : '';
            var toggleLabelEl = document.getElementById('toggleLabel');
            var billingLabel = toggleLabelEl ? toggleLabelEl.textContent : 'Billed monthly';
            openCheckout(plan, price, billingLabel);
          });
        });

        if (closeBtn) closeBtn.addEventListener('click', closeCheckout);
        overlay.addEventListener('click', function (e) {
          if (e.target === overlay) closeCheckout();
        });
        document.addEventListener('keydown', function (e) {
          if (e.key === 'Escape' && overlay.classList.contains('open')) closeCheckout();
        });
        if (form) {
          form.addEventListener('submit', function (e) {
            e.preventDefault(); // purchasing isn't wired up yet — see the disabled submit button
          });
        }

        // Prefill the signed-in visitor's email so checkout already knows
        // who's buying, same session the nav profile control above reads.
        supabase.auth.getUser().then(function (res) {
          var user = res && res.data ? res.data.user : null;
          if (user && user.email && emailInput && !emailInput.value) {
            emailInput.value = user.email;
          }
        }).catch(function () {});
      })();

      // ================= CAPABILITIES CAROUSEL =================
      (function () {
        var viewport = document.querySelector('.cap-track-viewport');
        var track = document.querySelector('.cap-track');
        var carousel = document.querySelector('.cap-carousel');
        if (!viewport || !track || !carousel) return;

        var realCards = Array.prototype.slice.call(track.children);
        var realCount = realCards.length;
        if (!realCount) return;

        var CLONES = Math.max(1, Math.min(5, Math.floor(realCount / 2)));
        var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        // ---- Build the extended, loop-friendly track: [tail clones][real x10][head clones]
        var extNodes = [];
        var frag = document.createDocumentFragment();

        function cloneCard(node, realIndex) {
          var c = node.cloneNode(true);
          c.setAttribute('aria-hidden', 'true');
          c.setAttribute('tabindex', '-1');
          c.dataset.real = String(realIndex);
          return c;
        }

        realCards.forEach(function (node, i) { node.dataset.real = String(i); });

        realCards.slice(realCount - CLONES).forEach(function (node, i) {
          var realIndex = realCount - CLONES + i;
          var clone = cloneCard(node, realIndex);
          frag.appendChild(clone);
          extNodes.push(clone);
        });
        realCards.forEach(function (node) {
          frag.appendChild(node);
          extNodes.push(node);
        });
        realCards.slice(0, CLONES).forEach(function (node, i) {
          var clone = cloneCard(node, i);
          frag.appendChild(clone);
          extNodes.push(clone);
        });

        track.innerHTML = '';
        track.appendChild(frag);

        // ---- Stabilize the stage's height so it never depends on which card
        // is currently centered. Each card's content length differs, and the
        // centered card also gets a larger heading — left alone, the flex row's
        // default align-items:stretch means the whole stage's height quietly
        // grows/shrinks as different cards rotate through center, which was
        // reflowing the entire page during autoplay. Measuring every real card
        // in both its normal AND is-center state (off-DOM, so nothing visible
        // is disturbed) gives the true worst-case height; pinning the viewport
        // to that fixed px height keeps it constant regardless of slide.
        function syncCarouselHeight() {
          var sample = realCards[0];
          if (!sample) return;
          var width = sample.getBoundingClientRect().width;
          if (!width) return;

          var measurer = document.createElement('div');
          measurer.style.position = 'absolute';
          measurer.style.visibility = 'hidden';
          measurer.style.pointerEvents = 'none';
          measurer.style.top = '0';
          measurer.style.left = '-9999px';
          measurer.style.width = width + 'px';
          document.body.appendChild(measurer);

          var maxH = 0;
          realCards.forEach(function (node) {
            [false, true].forEach(function (center) {
              var clone = node.cloneNode(true);
              clone.style.position = 'static';
              clone.style.width = width + 'px';
              clone.classList.toggle('is-center', center);
              measurer.appendChild(clone);
              var h = clone.getBoundingClientRect().height;
              if (h > maxH) maxH = h;
              measurer.removeChild(clone);
            });
          });
          document.body.removeChild(measurer);

          if (maxH > 0) viewport.style.height = Math.ceil(maxH) + 'px';
        }

        var cp = CLONES; // pointer into extNodes; starts centered on real index 0

        var tabs = Array.prototype.slice.call(document.querySelectorAll('.cap-tab'));
        var dotsWrap = document.querySelector('.cap-dots');
        var dots = dotsWrap ? Array.prototype.slice.call(dotsWrap.children) : [];
        var tabsStrip = document.querySelector('.cap-tabs');

        // Scrolls the active tab into view WITHIN the horizontal tab strip only —
        // never the page. tab.scrollIntoView() was walking up to the document and
        // yanking the whole page's vertical scroll position toward this section
        // (e.g. on every autoplay tick), which is the bug this replaces.
        function scrollTabIntoView(tab) {
          if (!tabsStrip) return;
          var stripRect = tabsStrip.getBoundingClientRect();
          var tabRect = tab.getBoundingClientRect();
          var offset = (tabRect.left + tabRect.right) / 2 - (stripRect.left + stripRect.right) / 2;
          if (Math.abs(offset) < 1) return;
          var targetLeft = tabsStrip.scrollLeft + offset;
          if (tabsStrip.scrollTo) {
            tabsStrip.scrollTo({ left: targetLeft, behavior: reduceMotion ? 'auto' : 'smooth' });
          } else {
            tabsStrip.scrollLeft = targetLeft;
          }
        }

        function visibleCount() { return window.innerWidth < 900 ? 1 : 3; }
        function centerOffset() { return Math.floor(visibleCount() / 2); }

        function stepSize() {
          var sample = extNodes[CLONES];
          if (!sample) return 0;
          var rect = sample.getBoundingClientRect();
          var cs = window.getComputedStyle(track);
          var gap = parseFloat(cs.columnGap || cs.gap || '0') || 0;
          return rect.width + gap;
        }

        function render(animate) {
          var s = stepSize();
          var x = -(cp - centerOffset()) * s;
          if (!animate) track.classList.add('no-anim');
          track.style.transform = 'translateX(' + x + 'px)';
          if (!animate) {
            void track.offsetHeight; // force reflow before re-enabling transitions
            track.classList.remove('no-anim');
          }

          extNodes.forEach(function (node, i) {
            node.classList.toggle('is-center', i === cp);
          });

          var realIdx = parseInt(extNodes[cp].dataset.real, 10);

          tabs.forEach(function (tab) {
            var active = parseInt(tab.dataset.index, 10) === realIdx;
            tab.classList.toggle('is-active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
            if (active) {
              scrollTabIntoView(tab);
            }
          });

          dots.forEach(function (dot, i) { dot.classList.toggle('is-active', i === realIdx); });
        }

        function settle() {
          if (cp >= CLONES + realCount) { cp -= realCount; render(false); }
          else if (cp < CLONES) { cp += realCount; render(false); }
        }

        function restartAutoplay() { if (!reduceMotion) startAutoplay(); }

        function goStep(delta) {
          cp += delta;
          render(true);
          restartAutoplay();
        }

        function goToReal(targetReal) {
          var best = null, bestDist = Infinity;
          extNodes.forEach(function (node, i) {
            if (parseInt(node.dataset.real, 10) === targetReal) {
              var d = Math.abs(i - cp);
              if (d < bestDist) { bestDist = d; best = i; }
            }
          });
          if (best === null || best === cp) return;
          cp = best;
          render(true);
          restartAutoplay();
        }

        track.addEventListener('transitionend', function (e) {
          if (e.propertyName === 'transform') settle();
        });

        var prevBtn = document.querySelector('.cap-nav-prev');
        var nextBtn = document.querySelector('.cap-nav-next');
        if (prevBtn) prevBtn.addEventListener('click', function () { goStep(-1); });
        if (nextBtn) nextBtn.addEventListener('click', function () { goStep(1); });

        track.addEventListener('click', function (e) {
          var card = e.target.closest ? e.target.closest('.cap-card') : null;
          if (!card) return;
          var idx = extNodes.indexOf(card);
          if (idx === cp - 1) goStep(-1);
          else if (idx === cp + 1) goStep(1);
        });

        tabs.forEach(function (tab) {
          tab.addEventListener('click', function () { goToReal(parseInt(tab.dataset.index, 10)); });
        });

        dots.forEach(function (dot, i) {
          dot.addEventListener('click', function () { goToReal(i); });
        });

        carousel.setAttribute('tabindex', '0');
        carousel.addEventListener('keydown', function (e) {
          if (e.key === 'ArrowLeft') { e.preventDefault(); goStep(-1); }
          else if (e.key === 'ArrowRight') { e.preventDefault(); goStep(1); }
        });

        var AUTOPLAY_MS = 4500;
        var timer = null;
        var isPaused = false;

        function startAutoplay() {
          stopAutoplay();
          if (reduceMotion) return;
          timer = setInterval(function () { if (!isPaused) goStep(1); }, AUTOPLAY_MS);
        }
        function stopAutoplay() { if (timer) { clearInterval(timer); timer = null; } }

        carousel.addEventListener('mouseenter', function () { isPaused = true; });
        carousel.addEventListener('mouseleave', function () { isPaused = false; });
        carousel.addEventListener('focusin', function () { isPaused = true; });
        carousel.addEventListener('focusout', function () { isPaused = false; });
        document.addEventListener('visibilitychange', function () {
          isPaused = document.hidden ? true : isPaused;
        });

        var resizeTimer;
        window.addEventListener('resize', function () {
          clearTimeout(resizeTimer);
          resizeTimer = setTimeout(function () { syncCarouselHeight(); render(false); }, 120);
        });

        window.addEventListener('load', syncCarouselHeight);
        if (document.fonts && document.fonts.ready) {
          document.fonts.ready.then(syncCarouselHeight);
        }

        syncCarouselHeight();
        render(false);
        startAutoplay();
      })();
  }, []);

  return (
    <>
      {/*
        eslint-disable-next-line @next/next/no-page-custom-font --
        this rule is aimed at the Pages Router's _document.js pattern;
        there's no App Router equivalent for a route-scoped font that's
        intentionally NOT shared via the root layout (see the file-level
        comment above on why this page doesn't touch globals.css/theme).
      */}
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
      <link
        rel="stylesheet"
        href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700;800&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap"
      />
      <style dangerouslySetInnerHTML={{ __html: LANDING_STYLES }} />
      <div ref={containerRef} dangerouslySetInnerHTML={{ __html: LANDING_BODY_HTML }} />
    </>
  );
}
