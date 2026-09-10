// Lightweight, dependency-free syntax highlighter for the fenced ```code
// blocks rendered in agent/chat output (see Markdown.jsx). These used to
// render as flat, single-color text on a black background — no token
// coloring at all. Rather than pull in react-syntax-highlighter/prismjs/
// highlight.js (a real bundle-size and dependency-tree cost for what's
// otherwise a zero-dependency markdown renderer), this is a single
// generic regex tokenizer shared across languages, styled to match VS
// Code's "Dark+" theme via the .tok-* classes in globals.css.
//
// This intentionally isn't a real per-language grammar/parser — it's a
// heuristic good enough to make code blocks look colorful and readable
// (comments, strings, numbers, keywords, function names) without the
// maintenance cost of one grammar per language. Language quirks it
// doesn't handle (e.g. Rust lifetimes, regex literals) just fall through
// as plain text, which is a safe failure mode — never wrong syntax, just
// occasionally under-highlighted.

const CONTROL_KEYWORDS = [
  "if", "else", "elif", "for", "foreach", "while", "do", "done", "then", "fi",
  "switch", "case", "default", "break", "continue", "return", "yield",
  "throw", "raise", "try", "except", "catch", "finally", "with", "as", "in",
  "is", "of", "match", "when", "goto",
];

const DECLARATION_KEYWORDS = [
  "const", "let", "var", "function", "def", "class", "struct", "enum",
  "interface", "trait", "impl", "namespace", "module", "package", "import",
  "export", "from", "using", "include", "require", "async", "await",
  "static", "public", "private", "protected", "readonly", "abstract",
  "final", "extends", "implements", "new", "delete", "typeof", "instanceof",
  "void", "fn", "pub", "mut", "type", "lambda", "global", "nonlocal",
  "self", "this", "super",
];

const CONSTANT_KEYWORDS = [
  "true", "false", "True", "False", "null", "None", "nil", "undefined",
  "NULL", "NaN", "Infinity",
];

const KEYWORD_RE = `\\b(?:${CONTROL_KEYWORDS.join("|")})\\b`;
const DECL_RE = `\\b(?:${DECLARATION_KEYWORDS.join("|")})\\b`;
const CONST_RE = `\\b(?:${CONSTANT_KEYWORDS.join("|")})\\b`;

// Comment syntax varies enough by language (//, #, --, <!-- -->) that
// getting it wrong actively breaks things — e.g. treating "//" as a
// comment inside a CSS `url(http://...)` would swallow the rest of the
// line. So comment style is chosen per-language rather than globally.
const XML_COMMENT_LANGS = new Set(["html", "xml", "svg", "vue"]);
const DOUBLE_SLASH_LANGS = new Set([
  "javascript", "js", "jsx", "typescript", "ts", "tsx", "java", "c", "cpp",
  "c++", "csharp", "cs", "go", "golang", "rust", "rs", "php", "swift",
  "kotlin", "kt", "scala", "dart", "groovy",
]);
const DASH_DASH_LANGS = new Set(["sql"]);
const HASH_COMMENT_LANGS = new Set([
  "python", "py", "sh", "bash", "shell", "zsh", "yaml", "yml", "ruby", "rb",
  "r", "perl", "dockerfile", "docker", "toml", "makefile", "cmake",
  "elixir", "ex", "exs",
]);
const BLOCK_COMMENT_LANGS = new Set([...DOUBLE_SLASH_LANGS, "css", "less", "scss", "sql"]);

function commentPattern(lang) {
  const parts = [];
  if (XML_COMMENT_LANGS.has(lang)) parts.push("<!--[\\s\\S]*?-->");
  if (DOUBLE_SLASH_LANGS.has(lang)) parts.push("//[^\\n]*");
  if (DASH_DASH_LANGS.has(lang)) parts.push("--[^\\n]*");
  if (HASH_COMMENT_LANGS.has(lang)) parts.push("#[^\\n]*");
  if (BLOCK_COMMENT_LANGS.has(lang)) parts.push("/\\*[\\s\\S]*?\\*/");
  if (!parts.length) {
    // No/unrecognized language tag on the fence — fall back to a
    // permissive set covering the most common comment styles instead of
    // skipping comment highlighting entirely.
    parts.push("//[^\\n]*", "#[^\\n]*", "/\\*[\\s\\S]*?\\*/", "<!--[\\s\\S]*?-->");
  }
  return `(${parts.join("|")})`;
}

// Triple-quoted variants MUST come before the plain single/double-quote
// alternatives below — regex alternation picks the first alternative
// that matches at a given start position, and a bare `"..."` pattern
// would otherwise match just the first empty `""` pair inside `"""`.
const STRING_RE =
  '("""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|"(?:[^"\\\\\\n]|\\\\.)*"|\'(?:[^\'\\\\\\n]|\\\\.)*\'|`(?:[^`\\\\]|\\\\.)*`)';
const NUMBER_RE = "(\\b0[xX][0-9a-fA-F]+\\b|\\b\\d+\\.?\\d*(?:[eE][+-]?\\d+)?\\b)";
const FUNC_RE = "([A-Za-z_$][\\w$]*)(?=\\s*\\()";
const TYPE_RE = "(\\b[A-Z][A-Za-z0-9_$]*\\b)";

const GROUP_TYPE = {
  1: "comment",
  2: "string",
  3: "number",
  4: "keyword",
  5: "decl",
  6: "const",
  7: "func",
  8: "type",
};

function buildMasterRegex(lang) {
  return new RegExp(
    [commentPattern(lang), STRING_RE, NUMBER_RE, `(${KEYWORD_RE})`, `(${DECL_RE})`, `(${CONST_RE})`, FUNC_RE, TYPE_RE].join("|"),
    "g"
  );
}

// JSON gets its own tiny pass — object keys need their own color (VS
// Code shows keys in light blue, distinct from string values), which
// the generic tokenizer above has no notion of since it doesn't parse
// structure, just tokens.
function tokenizeJson(code) {
  const re = /("(?:[^"\\]|\\.)*")(\s*:)?|(\btrue\b|\bfalse\b|\bnull\b)|(-?\b\d+\.?\d*(?:[eE][+-]?\d+)?\b)/g;
  const tokens = [];
  let lastIndex = 0;
  let match;
  while ((match = re.exec(code))) {
    if (match.index > lastIndex) tokens.push({ text: code.slice(lastIndex, match.index) });
    if (match[1] !== undefined) {
      tokens.push({ text: match[1], type: match[2] ? "prop" : "string" });
      if (match[2]) tokens.push({ text: match[2] });
    } else if (match[3] !== undefined) {
      tokens.push({ text: match[3], type: "const" });
    } else if (match[4] !== undefined) {
      tokens.push({ text: match[4], type: "number" });
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < code.length) tokens.push({ text: code.slice(lastIndex) });
  return tokens;
}

// Returns an array of { text, type? } tokens for the given code string.
// `type` (when present) maps to a `.tok-<type>` class in globals.css;
// tokens without a `type` are plain text rendered as-is.
export function tokenizeCode(code, lang) {
  if (!code) return [];
  const normalizedLang = (lang || "").toLowerCase();
  if (normalizedLang === "json") return tokenizeJson(code);

  const re = buildMasterRegex(normalizedLang);
  const tokens = [];
  let lastIndex = 0;
  let match;
  while ((match = re.exec(code))) {
    if (match.index > lastIndex) tokens.push({ text: code.slice(lastIndex, match.index) });
    const groupIndex = match.slice(1).findIndex((g) => g !== undefined) + 1;
    tokens.push({ text: match[0], type: GROUP_TYPE[groupIndex] });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < code.length) tokens.push({ text: code.slice(lastIndex) });
  return tokens;
}
