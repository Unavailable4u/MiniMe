# m2c: the M2C client

`m2c` is the local half of M2C (Minime to Code), a coding agent whose files,
history and settings live on your own machine. This directory is the client
package. The server side is built separately.

**Status: skeleton.** Today it can report whether your machine is ready
(`m2c doctor`). `login` and `logout` exist as stubs and exit with an error
saying they are not implemented yet.

## Requirements

- Linux, or Windows with **WSL2** (WSL1 and native Windows are not supported;
  macOS is planned for later).
- Python 3.11 or newer.
- `git` and `ripgrep` (`rg`) on `PATH`; `bubblewrap` (`bwrap`) for the
  sandbox, which `doctor` reports as a warning for now.
- Keep projects in the Linux filesystem (for example `~/code/app`), not under
  `/mnt/c`.
- Later releases require a signed-in account and a network connection: the
  reasoning loop runs on the server.

## Install for development

```sh
cd m2c
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
m2c --version
m2c doctor
```

The runtime has **no third-party dependencies** (standard library only);
`pytest` is the only dev dependency.

## Commands

| Command | What it does |
|---|---|
| `m2c --version` | Print the version. |
| `m2c doctor [--project PATH]` | Check OS (Linux/WSL2), Python, `git`, `rg`, `bwrap`, project location, config directory. |
| `m2c login`, `m2c logout` | Stubs: print "not implemented yet", exit status 3. |

`doctor` exit status: `0` when every hard requirement is met (warnings are
allowed), `1` when one is missing. Hard requirements are the OS, Python
version, `git`, `rg` and a writable config directory. A missing `bwrap` or a
project under `/mnt/c` is a warning for now.

## Where m2c keeps its files, and `M2C_HOME`

Everything the client stores lives in one directory, `~/.m2c`, created with
mode `0700` because it will hold your device credential.

Set **`M2C_HOME`** to use a different directory:

```sh
M2C_HOME=/tmp/m2c-test m2c doctor
```

- It names the directory itself (files go in `$M2C_HOME/...`, not
  `$M2C_HOME/.m2c/...`).
- It must be an absolute path (`~` is expanded). Empty means unset.
- The directory is created with mode `0700`. If it already exists with looser
  permissions and you own it, it is tightened to `0700`. If it is owned by
  someone else, or the filesystem refuses the mode (as `/mnt/c` does), m2c
  refuses to use it and `doctor` reports the failure.
- The test suite sets `M2C_HOME` (and `HOME`) to a temporary directory for
  every test, so tests never touch your real `~/.m2c`.

`M2C_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` (default), `ERROR`, `CRITICAL`)
controls logging on stderr. Logs never contain message content: nothing you
type, nothing the model writes, no tool output (see `src/m2c/logging_setup.py`).
