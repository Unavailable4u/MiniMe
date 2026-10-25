"""S04b: the device socket's runtime needs are provided by every deployment path.

backend/codeagent/ws.py validates every message with protocol/validate.py, which needs

  * jsonschema (with `referencing`, `jsonschema-specifications` and `rpds-py`), and
  * the protocol/ directory itself (protocol/v1/*.json), found by
    codeagent.ws.load_protocol() at <backend>/protocol or <repo root>/protocol.

Without either, load_protocol() raises ProtocolUnavailable and the socket fails closed
(`internal`, close 1011) -- safe, but the feature would be dead on arrival in a container.
This file reads the deployment files as TEXT, so it holds where Docker and pip are not
available, and fails on exactly the three ways that regression can come back:

  1. the pins the two requirements files share drift apart (protocol/requirements.txt is
     what the `protocol` CI job installs, backend/requirements.txt is what ships; a
     version that differs between them means the contract is tested against one
     jsonschema and served by another);
  2. backend.Dockerfile stops copying protocol/ to /app/protocol (the image's layout:
     WORKDIR /app holds backend/, so <backend>/protocol is /app/protocol);
  3. docker-compose.dev.yml stops mounting ./protocol at /app/protocol (its
     `./backend:/app` bind mount hides everything the image copied under /app, so the
     directory has to be mounted back in; read-only because nothing writes there).

That load_protocol() really succeeds in a backend install is tests/unit/
test_codeagent_protocol_runtime.py (tests/static is not part of CI's
`pytest tests/unit tests/integration`, tests/unit is).
"""
import pathlib
import posixpath
import re
import shlex

import pytest
import yaml

BACKEND = pathlib.Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
BACKEND_REQUIREMENTS = BACKEND / "requirements.txt"
PROTOCOL_REQUIREMENTS = ROOT / "protocol" / "requirements.txt"
DOCKERFILE = ROOT / "backend.Dockerfile"
DEV_OVERLAY = ROOT / "docker-compose.dev.yml"

# What S04b added to backend/requirements.txt: protocol/validate.py's own imports
# (jsonschema, referencing) and the two packages jsonschema itself cannot run without.
RUNTIME_PINS = ("jsonschema", "jsonschema-specifications", "referencing", "rpds-py")

APP_DIR = "/app"
PROTOCOL_IN_IMAGE = "/app/protocol"

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)")


# ---------------------------------------------------------------------
# requirements files
# ---------------------------------------------------------------------

def _normalise(name: str) -> str:
    """PEP 503: pip treats `typing_extensions` and `typing-extensions` as one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path: pathlib.Path) -> dict:
    """{normalised project name: version} for every `name==version` line.
    A project pinned twice, or a requirement that is not an exact pin, is an error:
    both files are pip freeze-style by convention, and a loose line here would let the
    two files agree while resolving differently."""
    pins = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match, f"{path.name}:{number}: not an exact `name==version` pin: {raw!r}"
        name = _normalise(match.group(1))
        assert name not in pins, f"{path.name}:{number}: {name} is pinned twice"
        pins[name] = match.group(2)
    return pins


def test_the_runtime_pins_are_in_the_backend_requirements():
    backend = _pins(BACKEND_REQUIREMENTS)
    missing = [name for name in RUNTIME_PINS if name not in backend]
    assert not missing, (
        f"backend/requirements.txt must pin {missing}: codeagent/ws.py needs jsonschema at "
        "runtime and a backend install should not rely on guardrails-ai or litellm to bring it")


def test_every_pin_the_two_requirements_files_share_is_identical():
    backend = _pins(BACKEND_REQUIREMENTS)
    protocol = _pins(PROTOCOL_REQUIREMENTS)
    for name in RUNTIME_PINS:
        assert name in protocol, f"protocol/requirements.txt lost {name}"
    shared = sorted(set(backend) & set(protocol))
    assert set(RUNTIME_PINS) <= set(shared)         # the loop below must really cover them
    drifted = {name: (backend[name], protocol[name])
               for name in shared if backend[name] != protocol[name]}
    assert not drifted, (
        "backend/requirements.txt and protocol/requirements.txt disagree "
        f"(backend, protocol): {drifted}. Change both files in the same patch.")


# ---------------------------------------------------------------------
# backend.Dockerfile
# ---------------------------------------------------------------------

def _instructions(path: pathlib.Path) -> list:
    """[(KEYWORD, argument text)] with comments dropped and `\\` continuations joined."""
    out, pending = [], ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not pending and (not raw.strip() or raw.lstrip().startswith("#")):
            continue
        if raw.lstrip().startswith("#"):            # a comment line inside a continuation
            continue
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        text = (pending + stripped).strip()
        pending = ""
        keyword, _, rest = text.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    assert not pending, "Dockerfile ends inside a line continuation"
    return out


def _copy(rest: str):
    """(sources, destination) of a COPY instruction; `--chown=` and friends are ignored."""
    words = [w for w in shlex.split(rest) if not w.startswith("--")]
    return words[:-1], words[-1]


def _in_image(destination: str, workdir: str) -> str:
    return posixpath.normpath(destination if destination.startswith("/")
                              else posixpath.join(workdir, destination))


def _dockerfile_steps():
    """Walk the Dockerfile once. Returns (index of the backend/ copy, index of the
    protocol/ copy or None, index of the chown RUN, index of the last USER, the USER)."""
    workdir = "/"
    backend_copy = protocol_copy = chown = last_user = None
    user = None
    for index, (keyword, rest) in enumerate(_instructions(DOCKERFILE)):
        if keyword == "WORKDIR":
            workdir = _in_image(rest, workdir)
        elif keyword == "COPY":
            sources, destination = _copy(rest)
            target = _in_image(destination, workdir)
            if sources == ["backend/"] and target == APP_DIR and backend_copy is None:
                backend_copy = index
            if (sources in (["protocol/"], ["protocol"]) and target == PROTOCOL_IN_IMAGE
                    and protocol_copy is None):
                protocol_copy = index
        elif keyword == "RUN" and re.search(r"\bchown\s+-R\s+app:app\s+/app\b", rest):
            chown = index
        elif keyword == "USER":
            last_user, user = index, rest
    return backend_copy, protocol_copy, chown, last_user, user


def test_the_image_copies_protocol_to_app_protocol():
    backend_copy, protocol_copy, *_ = _dockerfile_steps()
    assert backend_copy is not None, "backend.Dockerfile no longer copies backend/ to /app"
    assert protocol_copy is not None, (
        "backend.Dockerfile must `COPY protocol/ /app/protocol/`: codeagent.ws.load_protocol() "
        "looks in <backend>/protocol, which is /app/protocol in the image")
    assert protocol_copy > backend_copy, (
        "copy protocol/ AFTER `COPY backend/ .`, so a stray backend/protocol/ can never win")


def test_the_protocol_copy_keeps_the_existing_ownership_and_non_root_user():
    _, protocol_copy, chown, last_user, user = _dockerfile_steps()
    assert protocol_copy is not None
    assert chown is not None, "the `chown -R app:app /app` step is gone"
    assert protocol_copy < chown, "copy protocol/ BEFORE the chown so the app user owns it too"
    assert user == "app" and last_user > chown, "the image must still end as the non-root `app` user"


def test_protocol_is_not_excluded_from_the_build_context():
    ignored = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    for line in ignored:
        assert not re.match(r"^/?\*{0,2}/?protocol(/.*)?$", line), (
            f".dockerignore line {line!r} would keep protocol/ out of the image")


# ---------------------------------------------------------------------
# docker-compose.dev.yml
# ---------------------------------------------------------------------

class _ComposeLoader(yaml.SafeLoader):
    """Compose's custom tags (`!override`, `!reset`) are not YAML 1.1 core: read the
    node as if the tag were not there."""


def _untagged(loader, tag_suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _backend_volumes() -> list:
    doc = yaml.load(DEV_OVERLAY.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    return doc["services"]["backend"]["volumes"]


def _bind(volume):
    """(source, target, read_only) for the short and long volume syntaxes."""
    if isinstance(volume, dict):
        return (volume.get("source"), volume.get("target"), bool(volume.get("read_only")))
    parts = str(volume).split(":")
    options = parts[2].split(",") if len(parts) > 2 else []
    return (parts[0], parts[1], "ro" in options)


def test_the_dev_overlay_still_bind_mounts_backend_over_app():
    binds = [_bind(v) for v in _backend_volumes()]
    assert ("./backend", APP_DIR, False) in binds, (
        "the overlay's ./backend:/app mount changed: this test (and ws.py's "
        "<backend>/protocol lookup) assume it")


def test_the_dev_overlay_mounts_protocol_back_in_read_only():
    binds = [_bind(v) for v in _backend_volumes()]
    protocol = [b for b in binds if b[1] == PROTOCOL_IN_IMAGE]
    assert protocol, (
        "docker-compose.dev.yml must mount ./protocol at /app/protocol: its ./backend:/app "
        "bind mount hides the directory the image copied there")
    assert len(protocol) == 1
    source, _, read_only = protocol[0]
    assert posixpath.normpath(source) == "protocol", f"mounts {source!r}, not the repo's ./protocol"
    assert read_only, "nothing writes to protocol/ at runtime: mount it read-only"


def test_the_dev_overlay_keeps_overriding_the_base_volume_list():
    # Without `!override`, Compose would merge the base file's backend_data/backend_apps
    # named volumes in underneath ./backend (see the overlay's own comment).
    text = DEV_OVERLAY.read_text(encoding="utf-8")
    assert re.search(r"^\s+volumes:\s*!override\s*$", text, re.M)


@pytest.mark.parametrize("path", [BACKEND_REQUIREMENTS, PROTOCOL_REQUIREMENTS, DOCKERFILE, DEV_OVERLAY])
def test_the_deployment_files_this_test_reads_exist(path):
    assert path.is_file(), f"{path} not found (this test reads the repo root's deployment files)"
