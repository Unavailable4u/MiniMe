"""
m2c/tests/sandbox/test_netpolicy_rules.py -- Patch C11b: the pure rules of m2c.sandbox.netpolicy.

Covers: host normalization (case, trailing dot, IDNA, IPv6 brackets, junk), allowlist entries (exact, wildcard, IP),
wildcard semantics (never the bare name, any depth, no ``*.com``), IP literals and odd spellings, ``localhost``, ports,
the label shown in a prompt, and the saved file (permissions, atomic write, malformed entries ignored, protected
from the model like the other ~/.m2c files).  No network, no sandbox.

Place this file at: m2c/tests/sandbox/test_netpolicy_rules.py
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from m2c.sandbox import netpolicy
from m2c.sandbox.netpolicy import ALLOW, ASK, DENY, AllowFile, Allowlist, NetworkPolicy, normalize_host, parse_entry


# --------------------------------------------------------------------------
# normalize_host
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("PyPI.org", "pypi.org"),
    ("pypi.org.", "pypi.org"),
    ("  pypi.org  ", "pypi.org"),
    ("a-b.example.com", "a-b.example.com"),
    ("under_score.example.com", "under_score.example.com"),
    ("bücher.example", "xn--bcher-kva.example"),
    ("BÜCHER.example.", "xn--bcher-kva.example"),
    ("[::1]", "::1"),
    ("2001:DB8::1", "2001:db8::1"),
    ("93.184.216.34", "93.184.216.34"),
])
def test_normalize_accepts_and_canonicalizes(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize("raw", [
    "", " ", ".", "..", "a..b", "-a.example", "a-.example", "a b.example", "example.com:443", "example.com/path",
    "user@example.com", "*.example.com", "exa mple", "ex\x00ample.com", "a" * 64 + ".example", ("a" * 60 + ".") * 5 + "com",
    "pypi.org..", "[1.2.3.4]", "[fe80::1%eth0]", 5, None, b"pypi.org", ["pypi.org"],
])
def test_normalize_rejects(raw):
    assert normalize_host(raw) is None


def test_lookalike_script_is_shown_as_punycode_never_as_the_real_name():
    cyrillic_a = "а"  # looks like Latin "a"
    fake = f"p{cyrillic_a}ypal.com"
    shown = normalize_host(fake)
    assert shown is not None and shown.startswith("xn--") and shown != "paypal.com"
    policy = NetworkPolicy(allow=["paypal.com"])
    assert policy.check(fake, 443).action == ASK        # not the allowed name, and never matched to it


# --------------------------------------------------------------------------
# entries and wildcards
# --------------------------------------------------------------------------
def test_wildcard_matches_below_but_never_the_bare_name():
    allow = Allowlist(["*.example.com"])
    assert allow.matches("a.example.com")
    assert allow.matches("a.b.example.com")
    assert not allow.matches("example.com")
    assert not allow.matches("badexample.com")
    assert not allow.matches("example.com.evil.net")
    assert not allow.matches("xexample.com")


def test_exact_entry_matches_only_itself():
    allow = Allowlist(["example.com"])
    assert allow.matches("example.com")
    assert not allow.matches("a.example.com")
    assert not allow.matches("example.org")


def test_wildcard_and_exact_together():
    allow = Allowlist(["example.com", "*.example.com"])
    assert allow.matches("example.com") and allow.matches("x.example.com")


@pytest.mark.parametrize("raw", ["*.com", "*", "*.", "a*.example.com", "*.*.example.com", "ex*ample.com", "*example.com",
                                 "*.1.2.3.4", "*.0x7f", "*.127.1", "", "  ", 5, None, "localhost:80"])
def test_bad_entries_are_ignored(raw):
    assert parse_entry(raw) is None
    assert len(Allowlist([raw])) == 0


def test_entries_are_normalized_when_added():
    allow = Allowlist(["PyPI.ORG.", "*.Files.PythonHosted.org", "bücher.example"])
    assert allow.matches("pypi.org")
    assert allow.matches("a.files.pythonhosted.org")
    assert allow.matches("xn--bcher-kva.example")
    assert sorted(allow.entries()) == sorted(["pypi.org", "*.files.pythonhosted.org", "xn--bcher-kva.example"])


# --------------------------------------------------------------------------
# the decision table
# --------------------------------------------------------------------------
def policy(**kwargs):
    return NetworkPolicy(allow=kwargs.pop("allow", ["pypi.org", "*.example.com"]), **kwargs)


def test_listed_allowed_unlisted_asks():
    p = policy()
    assert p.check("pypi.org", 443) == netpolicy.Decision(ALLOW, "listed")
    assert p.check("PYPI.ORG.", 443).action == ALLOW
    assert p.check("a.example.com", 443).action == ALLOW
    assert p.check("example.com", 443).action == ASK
    assert p.check("unknown.test", 443) == netpolicy.Decision(ASK, "not_listed")


@pytest.mark.parametrize("port", [80, 8080, 0, 65536, -1, "443", None, True, 443.0])
def test_only_the_allowed_ports_pass(port):
    p = policy()
    assert p.check("pypi.org", port) == netpolicy.Decision(DENY, "port")


def test_ports_are_configurable_and_default_is_443_only():
    assert policy().ports == frozenset({443})
    p = policy(ports=(443, 8443))
    assert p.check("pypi.org", 8443).action == ALLOW
    assert p.check("pypi.org", 80).action == DENY
    assert NetworkPolicy(ports=(True, "x", 0, 70000, 22)).ports == frozenset({22})


def test_port_is_checked_before_asking():
    assert policy().check("unknown.test", 80) == netpolicy.Decision(DENY, "port")


@pytest.mark.parametrize("host", [
    "93.184.216.34", "127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "[::1]", "2001:db8::1", "::ffff:127.0.0.1",
])
def test_ip_literals_are_denied_unless_listed(host):
    assert policy().check(host, 443) == netpolicy.Decision(DENY, "ip_literal")


def test_a_listed_ip_literal_is_allowed_by_the_policy():
    p = policy(allow=["93.184.216.34", "[2001:db8::5]"])
    assert p.check("93.184.216.34", 443).action == ALLOW
    assert p.check("2001:db8::5", 443).action == ALLOW
    assert p.check("93.184.216.35", 443).action == DENY


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "127.1", "0x7f000001", "1.2.3.0x4", "012345", "a.b.123"])
def test_odd_spellings_of_addresses_are_denied(host):
    assert policy().check(host, 443).action == DENY


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "a.localhost", "x.y.localhost"])
def test_localhost_names_are_denied_outright(host):
    assert policy(allow=["localhost", "*.localhost"]).check(host, 443) == netpolicy.Decision(DENY, "local_name")


@pytest.mark.parametrize("host", ["", "a..b", "exa mple", "user@host", "host:443", "*.example.com", None, 5])
def test_invalid_hosts_are_denied(host):
    assert policy().check(host, 443) == netpolicy.Decision(DENY, "invalid_host")


def test_empty_policy_asks_about_everything_valid_and_allows_nothing():
    p = NetworkPolicy()
    assert p.check("pypi.org", 443).action == ASK


# --------------------------------------------------------------------------
# the label shown in a prompt
# --------------------------------------------------------------------------
def test_label_is_first_line_cut_and_cleaned():
    label = netpolicy.command_label("pip install x\nrm -rf /")
    assert label == "pip install x"
    assert len(netpolicy.command_label("echo " + "a" * 500)) <= 60
    assert "\x1b" not in netpolicy.command_label("echo \x1b[31mred\x1b[0m")
    assert netpolicy.command_label("") == "a command"
    assert netpolicy.command_label(None) in ("None", "a command")


def test_label_redacts_secrets():
    fake = "ghp_" + "x" * 36                       # built at run time: never a secret-shaped literal in the source
    label = netpolicy.command_label(f"curl -H 'Authorization: token {fake}' https://example.com")
    assert fake not in label


def test_label_shows_a_generic_text_when_redaction_cannot_run(monkeypatch):
    import m2c.secrets.redact as redact

    def boom(text):
        raise RuntimeError("no")

    monkeypatch.setattr(redact, "redact", boom)
    assert netpolicy.command_label("echo hi") == "a command"


# --------------------------------------------------------------------------
# the saved file
# --------------------------------------------------------------------------
def test_default_path_is_in_the_user_config_directory(tmp_path):
    from m2c import paths

    assert AllowFile().path == paths.config_dir() / "network.json"


def test_add_creates_a_private_file_and_reload_sees_it(tmp_path):
    f = AllowFile(tmp_path / "cfg" / "network.json")
    assert f.load() == []
    assert f.add("PyPI.org") is True
    assert f.load() == ["pypi.org"]
    mode = stat.S_IMODE(os.stat(f.path).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(f.path.parent).st_mode) == 0o700
    assert json.loads(f.path.read_text()) == {"version": 1, "allow": ["pypi.org"]}
    assert not [p for p in f.path.parent.iterdir() if p.name != "network.json"]      # no temp file left behind


def test_add_merges_with_what_another_process_wrote(tmp_path):
    path = tmp_path / "network.json"
    first, second = AllowFile(path), AllowFile(path)
    first.add("a.example")
    second.add("b.example")
    assert sorted(AllowFile(path).load()) == ["a.example", "b.example"]
    assert second.add("a.example") is True and AllowFile(path).load().count("a.example") == 1


@pytest.mark.parametrize("host", ["*.example.com", "", "a b", "localhost:1", "[fe80::1%eth0]"])
def test_add_refuses_what_is_not_an_exact_host(tmp_path, host):
    f = AllowFile(tmp_path / "network.json")
    assert f.add(host) is False
    assert not f.path.exists()


def test_malformed_entries_are_ignored_valid_ones_kept(tmp_path):
    path = tmp_path / "network.json"
    path.write_text(json.dumps({"version": 1, "allow": ["ok.example", 5, None, "*.com", "bad host", "*.good.example", ["x"], "UP.Example."]}))
    os.chmod(path, 0o600)
    assert AllowFile(path).load() == ["ok.example", "*.good.example", "up.example"]


@pytest.mark.parametrize("content", ["", "not json", "[]", "{}", '{"allow": "x"}', '{"allow": {"a": 1}}', "null", "\x00\x01"])
def test_garbage_files_load_as_empty(tmp_path, content):
    path = tmp_path / "network.json"
    path.write_text(content)
    os.chmod(path, 0o600)
    assert AllowFile(path).load() == []


def test_a_symlink_a_group_writable_file_and_a_huge_file_are_ignored(tmp_path):
    good = tmp_path / "real.json"
    good.write_text(json.dumps({"allow": ["ok.example"]}))
    os.chmod(good, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(good)
    assert AllowFile(link).load() == []
    os.chmod(good, 0o620)
    assert AllowFile(good).load() == []
    os.chmod(good, 0o600)
    assert AllowFile(good).load() == ["ok.example"]
    huge = tmp_path / "huge.json"
    huge.write_text(json.dumps({"allow": ["a.example"] * 100000}))
    os.chmod(huge, 0o600)
    assert AllowFile(huge).load() == []


def test_a_directory_in_place_of_the_file_is_ignored(tmp_path):
    (tmp_path / "network.json").mkdir()
    assert AllowFile(tmp_path / "network.json").load() == []
    assert AllowFile(tmp_path / "network.json").add("a.example") is False


def test_saved_entries_are_part_of_the_policy_and_pre_allowed_ones_add_to_them(tmp_path):
    f = AllowFile(tmp_path / "network.json")
    f.add("saved.example")
    p = NetworkPolicy(allow=["given.example"], store=f)
    assert p.check("saved.example", 443).action == ALLOW
    assert p.check("given.example", 443).action == ALLOW
    assert p.check("other.example", 443).action == ASK


def test_project_files_are_never_a_source_of_allowed_domains(tmp_path, monkeypatch):
    """The only file read is the user-level one: a project that ships its own network.json is ignored."""
    project = tmp_path / "proj"
    (project / ".m2c").mkdir(parents=True)
    for place in (project / "network.json", project / ".m2c" / "network.json"):
        place.write_text(json.dumps({"allow": ["evil.example"]}))
        os.chmod(place, 0o600)
    monkeypatch.chdir(project)
    p = NetworkPolicy(store=AllowFile())
    assert p.check("evil.example", 443).action == ASK


def test_the_saved_file_is_protected_from_the_model_like_other_m2c_files(tmp_path):
    """network.json lives in the config directory that the file tools' guards already refuse: it cannot be a project
    root, and a link to it from inside a project is outside the root, so the tools do not follow it."""
    from pathlib import Path

    from m2c import paths
    from m2c.tools import ToolContext
    from m2c.tools.paths import Denylist, PathGuardError, resolve_for_read

    target = AllowFile().path
    assert target.parent == paths.config_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(PathGuardError):
        ToolContext(target.parent)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "link.json").symlink_to(target)
    target.write_text("{}")
    with pytest.raises(PathGuardError):
        root = Path(os.path.realpath(project))
        resolve_for_read(root, "link.json", Denylist(root=root))
