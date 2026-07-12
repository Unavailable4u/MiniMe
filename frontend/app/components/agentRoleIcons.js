// frontend/app/components/agentRoleIcons.js
//
// Role -> {icon, color} lookup used by RoutingTraceGraph.jsx (and any
// other place that wants a consistent glyph per agent role). Pulled out
// of RoutingTraceGraph.jsx into its own file because this list is meant
// to grow: agents in this system are not just coding roles, they can be
// spun up for almost any kind of task, so the icon set needs to cover
// far more ground than "implementer/reviewer/tester".
//
// MATCHING RULES:
// - `categorize(label)` returns the FIRST entry whose `test` regex
//   matches the role/label string. Order matters -- more specific
//   patterns must come before broader ones that could also match them.
// - Keep one glyph per concept where possible so users build a mental
//   map (e.g. 🔍 always means "review/inspect text", 🛡️ always means
//   "security/guard-type role").
// - Prefer emoji that render consistently cross-platform (avoid rare
//   ZWJ sequences) since these are drawn directly on a <canvas>.

export const ROLE_CATEGORIES = [
  // ---- Meta / orchestration-specific (system's own role names) ----
  { key: "briefing", test: /^writing a new role brief/i, icon: "\u{1F4DC}", color: "#fbbf24" }, // 📜
  { key: "inspector", test: /\binspector\b/i, icon: "\u{1F46E}", color: "#60a5fa" }, // 👮 police-officer -- dedicated, no longer shares "dispatch"
  { key: "sga", test: /\bsga\b/i, icon: "\u{1F6E1}\uFE0F", color: "#facc15" }, // 🛡️ dedicated shield for the SGA pre-filter
  { key: "dispatch", test: /dispatcher|panel|responder/i, icon: "\u2699\uFE0F", color: "#9ca3af" }, // ⚙️

  // ---- Simulate & Test (persona role-play) ----
  // Part 1 §1.6 — these must sit ABOVE any broader pattern that could
  // also match a persona's literal role name (per this file's own
  // matching rule): critic_reviewer would otherwise fall into
  // "reviewing" below (it contains "reviewer"), and
  // support_ticket_predictor would otherwise fall into
  // "customer-support" (it contains "support"). Matched on the exact
  // role-name strings from eo/structure.py's STRUCTURE_TEMPLATES["simulate"].
  { key: "persona-customer", test: /persona_customer/i, icon: "\u{1F6CD}\uFE0F", color: "#fb923c" }, // 🛍️
  { key: "persona-skeptic", test: /persona_skeptic/i, icon: "\u{1F928}", color: "#a1a1aa" }, // 🤨
  { key: "critic-reviewer", test: /critic_reviewer/i, icon: "\u2B50", color: "#facc15" }, // ⭐
  { key: "usability-walkthrough", test: /usability_walkthrough/i, icon: "\u{1F6B6}", color: "#60a5fa" }, // 🚶
  { key: "red-team", test: /red_team/i, icon: "\u{1F977}", color: "#f87171" }, // 🥷
  { key: "pricing-sensitivity", test: /pricing_sensitivity/i, icon: "\u{1F3F7}\uFE0F", color: "#4ade80" }, // 🏷️
  { key: "support-ticket-predictor", test: /support_ticket_predictor/i, icon: "\u{1F3AB}", color: "#67e8f9" }, // 🎫
  { key: "competitor-response", test: /competitor_response/i, icon: "\u{1F94A}", color: "#fb7185" }, // 🥊
  { key: "simulation-synthesizer", test: /simulation_synthesizer/i, icon: "\u{1F9F5}", color: "#c4b5fd" }, // 🧵
  { key: "marketplace-review-batch", test: /marketplace_review_batch/i, icon: "\u{1F5E3}\uFE0F", color: "#f0abfc" }, // 🗣️

  // ---- Planning / ideation ----
  { key: "planning", test: /idea_planner|prompt_writer|planner|brainstorm/i, icon: "\u{1F9ED}", color: "#f59e0b" }, // 🧭
  { key: "strategy", test: /strategist|roadmap/i, icon: "\u265F\uFE0F", color: "#c4b5fd" }, // ♟️
  { key: "project-mgmt", test: /project_manager|scrum_master|coordinator/i, icon: "\u{1F4CB}", color: "#93c5fd" }, // 📋

  // ---- Software / engineering ----
  { key: "coding", test: /implementer|code.?writer|frontend|backend|full.?stack/i, icon: "\u{1F4BB}", color: "#38bdf8" }, // 💻
  { key: "fixing", test: /\bfixer\b|debugg/i, icon: "\u{1F527}", color: "#fb7185" }, // 🔧
  { key: "devops", test: /devops|infrastructure|deploy|ci_cd|sre\b/i, icon: "\u2601\uFE0F", color: "#7dd3fc" }, // ☁️
  { key: "database", test: /database|\bsql\b|db_admin/i, icon: "\u{1F5C4}\uFE0F", color: "#5eead4" }, // 🗄️
  { key: "networking", test: /network|protocol|dns\b|routing_engineer/i, icon: "\u{1F4E1}", color: "#67e8f9" }, // 📡
  { key: "cybersecurity", test: /security|pentest|vulnerab|threat_model/i, icon: "\u{1F510}", color: "#f87171" }, // 🔐
  { key: "ai-ml", test: /machine_learning|neural|model_train|\bml_\b/i, icon: "\u{1F916}", color: "#a78bfa" }, // 🤖
  { key: "robotics", test: /robot|automation_engineer/i, icon: "\u{1F9BE}", color: "#94a3b8" }, // 🦾
  { key: "blockchain", test: /blockchain|crypto|smart_contract/i, icon: "\u26D3\uFE0F", color: "#fbbf24" }, // ⛓️

  // ---- QA / review ----
  { key: "reviewing", test: /verifier|reviewer/i, icon: "\u{1F50D}", color: "#a78bfa" }, // 🔍
  { key: "testing", test: /test_writer|sandbox_tester|\btester\b|qa\b/i, icon: "\u{1F9EA}", color: "#34d399" }, // 🧪
  { key: "fact-check", test: /fact.?check/i, icon: "\u2705", color: "#4ade80" }, // ✅

  // ---- Notes domain (Part 4) ----
  // Sit above the broader "files"/"docs"/"planning" patterns just below
  // (same matching-order rule this file's header documents) — several of
  // these role names would otherwise be swallowed by those first (e.g.
  // "mapper" doesn't collide, but "study_guide_writer" would fall into
  // no existing bucket and land on DEFAULT_CATEGORY without an explicit
  // entry here).
  { key: "source-ingestor", test: /source_ingestor/i, icon: "\u{1F4E5}", color: "#38bdf8" }, // 📥
  { key: "mapper", test: /^mapper$/i, icon: "\u{1F9E0}", color: "#a78bfa" }, // 🧠 (mind map)
  { key: "podcast-scriptwriter", test: /podcast_scriptwriter/i, icon: "\u{1F3D9}\uFE0F", color: "#f472b6" }, // 🏙️ (two-host banter, closest free glyph to "podcast")
  { key: "slide-planner", test: /slide_planner/i, icon: "\u{1F4FA}", color: "#fbbf24" }, // 📺
  { key: "flashcard-writer", test: /flashcard_writer/i, icon: "\u{1F0CF}", color: "#4ade80" }, // 🃏
  { key: "quiz-writer", test: /quiz_writer/i, icon: "\u2753", color: "#fb923c" }, // ❓
  { key: "study-guide-writer", test: /study_guide_writer/i, icon: "\u{1F4D3}", color: "#93c5fd" }, // 📓

  // ---- Files / docs ----
  { key: "files", test: /file_manager|structure_architect/i, icon: "\u{1F5C2}\uFE0F", color: "#22d3ee" }, // 🗂️
  { key: "docs", test: /documentation|report_writer/i, icon: "\u{1F4C4}", color: "#a3e635" }, // 📄
  { key: "admin", test: /\badmin\b|data_entry|clerical/i, icon: "\u{1F5C3}\uFE0F", color: "#d4d4d8" }, // 🗃️
  { key: "personal-assistant", test: /assistant|scheduler|calendar/i, icon: "\u{1F5D3}\uFE0F", color: "#fda4af" }, // 🗓️

  // ---- Research / data / science ----
  // Part 3 — these must sit ABOVE the broader "research" pattern just
  // below (same matching rule as the Simulate & Test block above):
  // dataset_analyst would otherwise fall into "research" itself (it
  // contains "analyst"), and every other role here would otherwise fall
  // to DEFAULT_CATEGORY (none of these role names contain "research",
  // "analyst", or "investigat"). Matched on the exact role-name strings
  // from eo/registry.py's REAL_ACTION_ROLES / eo/structure.py's
  // STRUCTURE_TEMPLATES["research"].
  { key: "academic-search", test: /academic_search/i, icon: "\u{1F4DA}", color: "#a5b4fc" }, // 📚
  { key: "source-quality", test: /source_quality_flagger/i, icon: "\u{1F6A9}", color: "#fbbf24" }, // 🚩
  { key: "citation-graph", test: /citation_graph_builder/i, icon: "\u{1F578}\uFE0F", color: "#818cf8" }, // 🕸️
  { key: "extraction-table", test: /extraction_table_builder/i, icon: "\u{1F4D1}", color: "#38bdf8" }, // 📑
  { key: "contradiction-prefilter", test: /contradiction_prefilter/i, icon: "\u{1F500}", color: "#f87171" }, // 🔀
  { key: "contradiction-detector", test: /contradiction_detector/i, icon: "\u2694\uFE0F", color: "#ef4444" }, // ⚔️
  { key: "consensus-meter", test: /consensus_meter/i, icon: "\u{1F3AF}", color: "#4ade80" }, // 🎯
  { key: "dataset-analyst", test: /dataset_analyst/i, icon: "\u{1F4C8}", color: "#34d399" }, // 📈

  { key: "research", test: /research|analyst|investigat/i, icon: "\u{1F52C}", color: "#818cf8" }, // 🔬
  { key: "data-science", test: /data_scientist|statistic/i, icon: "\u{1F4CA}", color: "#60a5fa" }, // 📊
  { key: "science", test: /scientist|physic|chemist|biolog/i, icon: "\u{1F52D}", color: "#38bdf8" }, // 🔭
  { key: "math", test: /mathematic|calculus/i, icon: "\u{1F4D0}", color: "#fcd34d" }, // 📐
  { key: "astronomy", test: /astronom/i, icon: "\u{1F30C}", color: "#818cf8" }, // 🌌
  { key: "environment", test: /environment|sustainab|climate/i, icon: "\u{1F331}", color: "#4ade80" }, // 🌱
  { key: "weather", test: /weather|meteorolog/i, icon: "\u26C5", color: "#93c5fd" }, // ⛅

  // ---- Writing / creative ----
  { key: "lyrics", test: /lyricist|songwriter/i, icon: "\u{1F3A4}", color: "#f472b6" }, // 🎤
  { key: "music", test: /composer|arranger/i, icon: "\u{1F3B5}", color: "#c084fc" }, // 🎵
  { key: "storytelling", test: /storyteller|novelist|narrative/i, icon: "\u{1F4D6}", color: "#fb923c" }, // 📖
  { key: "copywriting", test: /copywriter|blog|essay|ghostwrit/i, icon: "\u270D\uFE0F", color: "#f0abfc" }, // ✍️
  { key: "journalism", test: /journalist|reporter\b/i, icon: "\u{1F4F0}", color: "#fca5a5" }, // 📰
  { key: "translation", test: /translat|localiz/i, icon: "\u{1F310}", color: "#5eead4" }, // 🌐
  { key: "design", test: /designer|\bux\b|\bui\b|graphic_design/i, icon: "\u{1F3A8}", color: "#f9a8d4" }, // 🎨
  { key: "art", test: /illustrat|painter|\bartist\b/i, icon: "\u{1F58C}\uFE0F", color: "#e879f9" }, // 🖌️
  { key: "video", test: /video_edit|filmmak|videograph/i, icon: "\u{1F3AC}", color: "#fca5a5" }, // 🎬
  { key: "photo", test: /photo|image_edit/i, icon: "\u{1F4F7}", color: "#fdba74" }, // 📷
  { key: "gaming", test: /game_design|gaming|esport/i, icon: "\u{1F3AE}", color: "#a78bfa" }, // 🎮
  { key: "social-media", test: /social_media|community_manager/i, icon: "\u{1F4F1}", color: "#7dd3fc" }, // 📱

  // ---- Business / finance / legal ----
  { key: "finance", test: /finance|accountant|bookkeep|budget/i, icon: "\u{1F4B0}", color: "#facc15" }, // 💰
  { key: "legal", test: /legal|lawyer|attorney|compliance|contract/i, icon: "\u2696\uFE0F", color: "#cbd5e1" }, // ⚖️
  { key: "sales", test: /\bsales\b|negotiat/i, icon: "\u{1F91D}", color: "#fb923c" }, // 🤝
  { key: "marketing", test: /marketing|advertis|campaign/i, icon: "\u{1F4E3}", color: "#f472b6" }, // 📣
  { key: "customer-support", test: /support|helpdesk|customer_service/i, icon: "\u{1F3A7}", color: "#67e8f9" }, // 🎧
  { key: "hr", test: /\bhr\b|recruit|hiring|talent/i, icon: "\u{1F9D1}\u200D\u{1F4BC}", color: "#a3e635" }, // 🧑‍💼
  { key: "logistics", test: /logistics|supply_chain|inventory|shipping/i, icon: "\u{1F4E6}", color: "#fdba74" }, // 📦
  { key: "retail", test: /retail|merchandis/i, icon: "\u{1F6D2}", color: "#f9a8d4" }, // 🛒
  { key: "real-estate", test: /real_estate|property_manag|realtor/i, icon: "\u{1F3E0}", color: "#86efac" }, // 🏠

  // ---- Health / lifestyle ----
  { key: "medical", test: /medical|doctor|clinician|diagnos|health/i, icon: "\u{1FA7A}", color: "#f87171" }, // 🩺
  { key: "therapy", test: /therapist|counselor|psycholog/i, icon: "\u{1F9E0}", color: "#c4b5fd" }, // 🧠
  { key: "nutrition", test: /nutrition|\bdiet\b|meal_plan/i, icon: "\u{1F957}", color: "#84cc16" }, // 🥗
  { key: "fitness", test: /fitness|trainer|workout|coach/i, icon: "\u{1F3CB}\uFE0F", color: "#fb7185" }, // 🏋️
  { key: "cooking", test: /\bchef\b|recipe|cook/i, icon: "\u{1F468}\u200D\u{1F373}", color: "#fdba74" }, // 👨‍🍳

  // ---- Education / other domains ----
  { key: "education", test: /teacher|tutor|instructor|professor/i, icon: "\u{1F393}", color: "#93c5fd" }, // 🎓
  { key: "event-planning", test: /event_plan|wedding_plan|party_plan/i, icon: "\u{1F389}", color: "#f472b6" }, // 🎉
  { key: "travel", test: /travel|itinerary|trip_plan/i, icon: "\u2708\uFE0F", color: "#38bdf8" }, // ✈️
  { key: "transportation", test: /transport|route_plan|vehicle/i, icon: "\u{1F697}", color: "#94a3b8" }, // 🚗
  { key: "agriculture", test: /\bfarm|agricult/i, icon: "\u{1F33E}", color: "#a3e635" }, // 🌾
  { key: "energy", test: /energy|power_grid|renewable/i, icon: "\u26A1", color: "#facc15" }, // ⚡
  { key: "architecture", test: /building_architect|architectural_design/i, icon: "\u{1F3DB}\uFE0F", color: "#d4d4d8" }, // 🏛️
  { key: "engineering", test: /mechanical_eng|electrical_eng|civil_eng/i, icon: "\u{1F6E0}\uFE0F", color: "#fb923c" }, // 🛠️
  { key: "military", test: /military|tactical/i, icon: "\u{1F396}\uFE0F", color: "#a8a29e" }, // 🎖️
  { key: "law-enforcement", test: /\bpolice\b|detective/i, icon: "\u{1F694}", color: "#60a5fa" }, // 🚔
  { key: "sports", test: /\bsports\b|athlete/i, icon: "\u{1F3C6}", color: "#fbbf24" }, // 🏆
];

// Fallback + fixed endpoint categories -- unchanged behavior from before.
export const DEFAULT_CATEGORY = { key: "generic", icon: "\u{1F9E9}", color: "#e879f9" }; // 🧩
export const INPUT_CATEGORY = { key: "input", icon: "\u{1F4E5}", color: "#38bdf8" }; // 📥
export const OUTPUT_CATEGORY = { key: "output", icon: "\u{1F4E4}", color: "#34d399" }; // 📤

export function categorize(label) {
  if (!label) return DEFAULT_CATEGORY;
  return ROLE_CATEGORIES.find((c) => c.test.test(label)) || DEFAULT_CATEGORY;
}
