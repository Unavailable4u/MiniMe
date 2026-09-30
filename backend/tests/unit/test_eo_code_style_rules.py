"""
tests/unit/test_eo_code_style_rules.py — W7.1a.

eo/code_style_rules.py: the pure CSS-rule finder behind "make this
button green" on a preview element. Pinned here: which selectors count
as styling an element's class (subject-only, whole-name, not inside
:not()), the tolerant scanner (comments, strings, url(//), Sass `#{}`,
nesting, at-rules, CRLF, broken input), `<style>` blocks inside
html/vue/svelte with correct line numbers, ranking/caps, and that nothing
here can raise on garbage.
"""
import pytest

from eo import code_style_rules as csr
from eo.code_style_rules import candidate_style_paths, find_style_rules, valid_class_name


def rules(css, classes, path="a.css", **kw):
    return find_style_rules({path: css}, classes, **kw)


def spans(found):
    return [(r.start_line, r.end_line) for r in found]


# ---------------------------------------------------------------------------
# which selectors count
# ---------------------------------------------------------------------------
class TestMatching:
    def test_simple_rule_and_its_line_span(self):
        css = ".btn-primary {\n  color: red;\n}\n"
        (r,) = rules(css, ["btn-primary"])
        assert (r.path, r.start_line, r.end_line, r.selector) == ("a.css", 1, 3, ".btn-primary")
        assert r.classes == ("btn-primary",) and r.at_rules == () and not r.truncated

    def test_a_class_is_matched_whole_not_as_a_prefix(self):
        css = ".btn-primary { a: b }\n.btn { c: d }\n"
        assert [r.selector for r in rules(css, ["btn"])] == [".btn"]
        assert [r.selector for r in rules(css, ["btn-primary"])] == [".btn-primary"]

    def test_a_class_is_matched_whole_not_as_a_suffix(self):
        assert rules(".my-btn { a: b }", ["btn"]) == []

    @pytest.mark.parametrize("selector", [
        ".btn:hover", ".btn::after", ".card .btn", ".card > .btn", ".a + .btn", "button.btn",
        ".btn.active", ".btn[disabled]", "a, .btn", ".x,\n.btn",
    ])
    def test_selectors_that_style_the_element_itself(self, selector):
        assert len(rules(selector + " { a: b }", ["btn"])) == 1

    @pytest.mark.parametrize("selector", [
        ".btn span", ".btn > span", ".btn .icon", ".btn:hover .icon", ":not(.btn)", ".x:not(.btn)",
    ])
    def test_selectors_where_the_class_is_only_context_or_negated(self, selector):
        assert rules(selector + " { a: b }", ["btn"]) == []

    def test_escaped_utility_selectors_are_not_matched_as_the_plain_class(self):
        assert rules(".btn\\:x { a: b }", ["btn"]) == []

    def test_only_the_asked_classes_are_reported_on_a_rule(self):
        (r,) = rules(".a.b.c { x: y }", ["a", "c", "zzz"])
        assert r.classes == ("a", "c")

    def test_non_identifier_classes_are_never_searched(self):
        css = ".md\\:px-4 { a: b }\n.w-1\\/2 { a: b }\n.px-4 { a: b }\n"
        assert rules(css, ["md:px-4", "w-[10px]", "w-1/2", "has space", "", None, 5]) == []
        assert len(rules(css, ["md:px-4", "px-4"])) == 1

    def test_no_classes_means_no_rules(self):
        assert rules(".a { x: y }", []) == []
        assert find_style_rules({"a.css": ".a{}"}, None) == []

    def test_class_matching_is_case_sensitive(self):
        assert rules(".Btn { a: b }", ["btn"]) == []

    def test_valid_class_name(self):
        assert valid_class_name("btn") and valid_class_name("-x") and valid_class_name("_a-1")
        assert not valid_class_name("md:px-4") and not valid_class_name("1a")
        assert not valid_class_name("") and not valid_class_name(None) and not valid_class_name("a" * 81)


# ---------------------------------------------------------------------------
# the scanner
# ---------------------------------------------------------------------------
class TestScanner:
    def test_selector_in_a_comment_is_ignored(self):
        assert rules("/* .btn { a: b } */\n.other { c: d }", ["btn"]) == []

    def test_line_numbers_survive_a_multiline_comment(self):
        css = "/* one\ntwo\nthree */\n.btn {\n  a: b;\n}\n"
        assert spans(rules(css, ["btn"])) == [(4, 6)]

    def test_line_comment_is_ignored_but_url_double_slash_is_not_a_comment(self):
        css = ("// .btn { a: b }\n"
               ".x { background: url(http://example.com/a.png); }\n"
               ".btn { color: red; }\n")
        assert spans(rules(css, ["btn"], path="a.scss")) == [(3, 3)]

    def test_braces_inside_strings_do_not_open_blocks(self):
        css = '.x::after { content: "{ .btn {"; }\n.btn { a: b; }\n'
        assert spans(rules(css, ["btn"])) == [(2, 2)]

    def test_escaped_quote_inside_a_string(self):
        css = '.x::after { content: "a \\" { b"; }\n.btn { a: b; }\n'
        assert spans(rules(css, ["btn"])) == [(2, 2)]

    def test_sass_interpolation_is_part_of_the_selector_not_a_block(self):
        css = ".icon-#{$name} { a: b }\n.btn { c: d }\n"
        assert spans(rules(css, ["btn"], path="a.scss")) == [(2, 2)]

    def test_at_rule_chain_is_recorded(self):
        css = "@media (max-width: 600px) {\n  @supports (display: grid) {\n    .btn { a: b }\n  }\n}\n"
        (r,) = rules(css, ["btn"])
        assert r.at_rules == ("@media (max-width: 600px)", "@supports (display: grid)")
        assert (r.start_line, r.end_line) == (3, 3)
        assert "inside @media (max-width: 600px) > @supports (display: grid)" in r.label()

    def test_at_rule_blocks_themselves_are_never_rules(self):
        assert rules("@font-face { font-family: btn; }\n@keyframes btn { from { a: b } }", ["btn"]) == []

    def test_multiline_selector_list_starts_at_its_first_line(self):
        css = "\n\n.a,\n.b,\n.btn {\n  x: y;\n}\n"
        assert spans(rules(css, ["btn"])) == [(3, 7)]

    def test_nested_child_rule_is_found_when_only_it_matches(self):
        css = ".parent {\n  color: red;\n  .btn { c: d }\n}\n"
        (r,) = rules(css, ["btn"], path="a.scss")
        assert (r.selector, r.start_line, r.end_line) == (".btn", 3, 3)

    def test_nested_rule_inside_a_matching_rule_is_dropped(self):
        css = ".btn {\n  color: red;\n  &.active { c: d }\n  .btn { e: f }\n}\n"
        assert spans(rules(css, ["btn"], path="a.scss")) == [(1, 5)]

    def test_crlf_and_lone_cr_each_count_as_one_line(self):
        assert spans(rules("a{}\r\n\r\n.btn {\r\n a: b;\r\n}\r\n", ["btn"])) == [(3, 5)]
        assert spans(rules("a{}\r\r.btn {\r a: b;\r}\r", ["btn"])) == [(3, 5)]

    @pytest.mark.parametrize("junk", [
        "", "}}}{{{", "{", "}", ".btn {", ".btn { a: b", "/* unterminated .btn { a: b }",
        '.btn { content: "unterminated }', ".btn { a: url( }", "@media {", "#{", ";;;;",
        "\x00\x01.btn{}\x02",
    ])
    def test_garbage_never_raises(self, junk):
        find_style_rules({"a.css": junk, "b.html": "<style>" + junk}, ["btn"])

    def test_unbalanced_input_closes_at_the_last_line(self):
        (r,) = rules(".btn {\n  a: b;\n", ["btn"])
        assert (r.start_line, r.end_line) == (1, 3)


# ---------------------------------------------------------------------------
# <style> blocks inside other files
# ---------------------------------------------------------------------------
class TestEmbeddedStyle:
    HTML = ("<!doctype html>\n"            # 1
            "<html>\n"                      # 2
            "<head>\n"                      # 3
            "<style>\n"                     # 4
            "  .btn {\n"                    # 5
            "    color: red;\n"             # 6
            "  }\n"                         # 7
            "</style>\n"                    # 8
            "</head>\n"                     # 9
            "<body><button class=\"btn\">Go</button>\n"  # 10
            "<style media=\"print\">.btn { display: none }</style>\n")  # 11

    def test_line_numbers_are_those_of_the_whole_file(self):
        found = find_style_rules({"index.html": self.HTML}, ["btn"])
        assert sorted(spans(found)) == [(5, 7), (11, 11)]

    def test_markup_outside_style_blocks_is_not_scanned(self):
        html = "<div class=\".btn { color: red }\">x</div>\n<script>const s = '.btn { a: b }'</script>\n"
        assert find_style_rules({"index.html": html}, ["btn"]) == []

    def test_vue_and_svelte_style_blocks(self):
        vue = "<template><b class=\"btn\"/></template>\n<style scoped lang=\"scss\">\n.btn { a: b }\n</style>\n"
        svelte = "<button class=\"btn\">x</button>\n<style>\n  .btn { a: b }\n</style>\n"
        found = find_style_rules({"A.vue": vue, "B.svelte": svelte}, ["btn"])
        assert {(r.path, r.start_line) for r in found} == {("A.vue", 3), ("B.svelte", 3)}

    def test_a_js_file_is_ignored_even_if_it_looks_like_css(self):
        assert find_style_rules({"a.js": ".btn { a: b }"}, ["btn"]) == []

    def test_crlf_html_offsets(self):
        html = "<html>\r\n<style>\r\n.btn { a: b }\r\n</style>\r\n"
        assert spans(find_style_rules({"i.html": html}, ["btn"])) == [(3, 3)]


# ---------------------------------------------------------------------------
# ranking, caps
# ---------------------------------------------------------------------------
class TestRankingAndCaps:
    def test_rules_matching_more_asked_classes_come_first(self):
        css = ".a { x: y }\n.a.b { x: y }\n.b { x: y }\n"
        assert [r.selector for r in rules(css, ["a", "b"])] == [".a.b", ".a", ".b"]

    def test_order_is_path_then_line_for_equal_rules(self):
        files = {"z.css": ".btn{}", "a.css": ".btn{}\n.btn:hover{}"}
        found = find_style_rules(files, ["btn"])
        assert [(r.path, r.start_line) for r in found] == [("a.css", 1), ("a.css", 2), ("z.css", 1)]

    def test_max_rules(self):
        css = "\n".join(f".btn:nth-child({i}) {{ a: b }}" for i in range(30))
        assert len(rules(css, ["btn"])) == csr.MAX_RULES
        assert len(rules(css, ["btn"], max_rules=3)) == 3
        assert rules(css, ["btn"], max_rules=0) == []

    def test_a_long_rule_is_cut_and_flagged(self):
        css = ".btn {\n" + "  a: b;\n" * 200 + "}\n"
        (r,) = rules(css, ["btn"], max_rule_lines=10)
        assert (r.start_line, r.end_line, r.truncated) == (1, 10, True)
        assert "rule continues past what is shown" in r.label()

    def test_a_very_long_selector_is_capped(self):
        sel = ".btn" + ", .x" * 200
        (r,) = rules(sel + " { a: b }", ["btn"])
        assert len(r.selector) <= csr.MAX_SELECTOR_CHARS + 1 and r.selector.endswith("…")

    def test_oversized_files_are_skipped(self):
        big = ".btn { a: b }\n" + "/* " + "x" * csr.MAX_STYLE_FILE_CHARS + " */"
        assert find_style_rules({"big.css": big, "ok.css": ".btn{}"}, ["btn"])[0].path == "ok.css"

    def test_non_string_content_is_ignored(self):
        assert find_style_rules({"a.css": None, "b.css": 5}, ["btn"]) == []

    def test_label_single_line(self):
        (r,) = rules(".btn { a: b }", ["btn"])
        assert r.label() == "a.css line 1: .btn"


# ---------------------------------------------------------------------------
# which workspace paths are worth reading
# ---------------------------------------------------------------------------
class TestCandidatePaths:
    def test_stylesheets_first_then_embedders_each_sorted(self):
        paths = ["src/b.css", "index.html", "a.scss", "src/App.vue", "x.less", "app.js", "a.sass"]
        assert candidate_style_paths(paths) == [
            "a.sass", "a.scss", "src/b.css", "x.less", "index.html", "src/App.vue"]

    def test_build_output_vendored_and_minified_are_skipped(self):
        paths = ["node_modules/x/a.css", "dist/a.css", "build/a.css", ".next/a.css",
                 "src/vendor/a.css", "src/a.min.css", "src/ok.css", "public/out/a.css"]
        assert candidate_style_paths(paths) == ["src/ok.css"]

    def test_a_file_named_like_a_skipped_dir_is_not_skipped(self):
        assert candidate_style_paths(["dist.css", "build.html"]) == ["dist.css", "build.html"]

    def test_limit(self):
        paths = [f"s{i:02}.css" for i in range(50)]
        assert len(candidate_style_paths(paths)) == csr.MAX_STYLE_FILES
        assert candidate_style_paths(paths, limit=2) == ["s00.css", "s01.css"]

    def test_junk_entries_are_ignored(self):
        assert candidate_style_paths([None, 5, "a.css"]) == ["a.css"]


# ---------------------------------------------------------------------------
# hostile / degenerate input must terminate quickly, not just "not raise"
# ---------------------------------------------------------------------------
class TestBoundedWork:
    """A rule lookup runs inside a request, over files the person (or the
    pipeline) wrote. Before these bounds, 200 KB of `{` or 28k unclosed
    `<style>` tags each took longer than 20 s (quadratic: the at-rule chain
    was rebuilt from the whole stack per `{`, the nested-rule check compared
    every rule with every other, and the `<style>` regex re-scanned to the
    end of the file per opening tag). The deadline is generous; a regression
    back to quadratic misses it by orders of magnitude."""

    N = csr.MAX_STYLE_FILE_CHARS
    DEADLINE = 5.0

    def timed(self, files, classes=("btn",)):
        import time
        t0 = time.perf_counter()
        out = find_style_rules(files, list(classes))
        assert time.perf_counter() - t0 < self.DEADLINE
        return out

    def test_an_open_brace_flood(self):
        found = self.timed({"a.css": (".btn" + "{" * self.N)[: self.N]})
        assert isinstance(found, list)

    def test_tens_of_thousands_of_nested_btn_blocks(self):
        assert self.timed({"a.css": ".btn{" * (self.N // 5)})

    def test_tens_of_thousands_of_sibling_rules(self):
        found = self.timed({"a.css": ".btn{}" * (self.N // 6)})
        assert len(found) <= csr.MAX_RULES

    def test_unclosed_style_tags_repeated(self):
        assert self.timed({"a.html": (".btn " + "<style>" * (self.N // 7))[: self.N]}) == []

    def test_style_tags_without_a_closing_bracket(self):
        assert self.timed({"a.html": (".btn " + "<style" * (self.N // 6))[: self.N]}) == []

    def test_sass_interpolation_flood(self):
        self.timed({"a.scss": (".btn " + "#{" * (self.N // 2))[: self.N]})

    def test_nesting_to_a_realistic_depth_is_still_matched(self):
        css = "".join(f".l{i}{{" for i in range(30)) + ".btn{color:red}" + "}" * 30
        found = rules(css, ["btn"], path="a.scss")
        assert [r.selector for r in found] == [".btn"]

    def test_nesting_past_the_limit_stops_scanning_instead_of_tracking_it(self):
        depth = csr.MAX_NEST_DEPTH + 10
        css = ".btn{color:red}\n" + "".join(f".l{i}{{" for i in range(depth)) + ".btn{x:y}" + "}" * depth
        found = rules(css, ["btn"], path="a.scss")
        assert found and found[0].start_line == 1          # what came before the flood is kept

    def test_a_file_that_cannot_contain_the_class_is_not_scanned_at_all(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("scanned a file with no `.btn` in it")
        monkeypatch.setattr(csr, "_scan_blocks", boom)
        assert find_style_rules({"a.css": ".other { a: b }", "b.html": "<style>.x{}</style>"}, ["btn"]) == []


class TestStyleBlocksFinder:
    def test_finds_each_block_with_attributes_and_any_case(self):
        t = '<STYLE media="print">a</STYLE>x<style>b</style >'
        assert [t[a:b] for a, b in csr._style_blocks(t)] == ["a", "b"]

    def test_stops_at_a_trailing_unclosed_block(self):
        t = "<style>a</style><style>never closed"
        assert [t[a:b] for a, b in csr._style_blocks(t)] == ["a"]

    def test_style_content_is_raw_text_up_to_the_next_closing_tag(self):
        # Like a browser: a second `<style>` inside one is not a new block.
        t = "<style>a <style>b</style>"
        assert [t[a:b] for a, b in csr._style_blocks(t)] == ["a <style>b"]

    def test_no_blocks(self):
        assert list(csr._style_blocks("<p>hi</p>")) == []


class TestNestedRuleDrop:
    """The linear containment sweep must agree with the obvious O(n^2) rule."""

    @staticmethod
    def brute(found):
        def contained(r):
            return any(o is not r and o.path == r.path and o.start_line <= r.start_line
                       and r.end_line <= o.end_line
                       and (o.start_line, o.end_line) != (r.start_line, r.end_line)
                       for o in found)
        return {(r.path, r.start_line, r.end_line) for r in found if not contained(r)}

    def test_matches_the_brute_force_definition_on_random_nesting(self):
        import random
        rnd = random.Random(7)
        for _ in range(60):
            lines, depth, n = [], 0, 0
            for _ in range(rnd.randint(5, 40)):
                if depth and rnd.random() < 0.45:
                    lines.append("}")
                    depth -= 1
                elif depth < 6:
                    lines.append((".btn" if rnd.random() < 0.6 else ".other") + "{")
                    depth += 1
                else:
                    lines.append("a:b;")
                n += 1
            lines += ["}"] * depth
            css = "\n".join(lines)
            every = find_style_rules({"a.scss": css}, ["btn"], max_rules=10_000)
            # brute force over the UNFILTERED matches
            raw = csr._rules_in_text("a.scss", css, {"btn": csr._class_regex("btn")}, 0, csr.MAX_RULE_LINES)
            assert {(r.path, r.start_line, r.end_line) for r in every} == self.brute(raw)
