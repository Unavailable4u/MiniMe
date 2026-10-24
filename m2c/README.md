# m2c: the M2C client

`m2c` is the local half of M2C (Minime to Code), a coding agent whose files,
history and settings live on your own machine. This directory is the client
package. The server side is built separately.

**Status: early.** It can report whether your machine is ready (`m2c doctor`)
and sign this device in to your MiniMe account (`m2c login`) and out again
(`m2c logout`).

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
| `m2c login [--device-name NAME]` | Sign in with a code you approve in the web app (needs `M2C_SERVER_URL`). |
| `m2c logout` | Delete this device's local credential. Revoke the device on the web Devices page too. |

## Signing in

```sh
export M2C_SERVER_URL=https://api.example.com   # your MiniMe server (see below)
m2c login
```

`m2c login` prints a short code and a web address. Open the address in a
browser (on any device, it never tries to open one for you, which fails in
WSL2), sign in and enter the code. The credential is then stored on this
machine:

- in your **OS keyring**, if the optional `keyring` package is installed
  (`pip install 'm2c[keyring]'`) and has a usable backend; otherwise
- in `credentials.json` inside the config directory, mode `0600`, written
  atomically. m2c refuses to read the file if its permissions are looser and
  tells you the `chmod 600` command to fix it. WSL2 normally has no keyring,
  so the file is the usual place there.

`M2C_SERVER_URL` must be `https://...` (plain `http://` is accepted only for
`localhost`), with no password in it. After a login the address is remembered
with the credential, which is only ever sent to the server that issued it.

`m2c logout` deletes the local credential and makes no network call. The
server does not yet offer a way for a device to revoke itself, so also revoke
the device on the Devices page of the web app.

The credential, the sign-in code and access tokens are never printed or
logged. Exit status `4` means the sign-in failed (denied, expired, server
unreachable) and the message says which.
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

`M2C_SERVER_URL` is described under "Signing in" above.

`M2C_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` (default), `ERROR`, `CRITICAL`)
controls logging on stderr. Logs never contain message content: nothing you
type, nothing the model writes, no tool output (see `src/m2c/logging_setup.py`).
