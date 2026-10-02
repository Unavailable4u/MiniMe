// Phase CO, CO2 (Master Guide v2, §5) — Pyodide Web Worker.
//
// Lives under public/ (not app/) and is loaded via `new Worker("/workers/
// pyodideWorker.js")` -- a plain static file served as-is, deliberately
// NOT run through Next.js's bundler/module worker syntax. That keeps this
// patch to "one script tag + worker wiring" (per the guide's own cost
// note) instead of also needing a next.config.js change to teach webpack
// about module workers.
//
// Runs entirely in the visitor's browser. Nothing here is ever sent to
// MiniMe's own backend -- same client-side-only boundary CO2's html/svg
// iframe artifacts already use, for the same reason: model-generated code
// should never execute server-side near real credentials.
//
// The Pyodide CORE runtime (pyodide.js, the .wasm, python_stdlib.zip) is
// self-hosted under /pyodide/ (copied there from the `pyodide` npm
// package by scripts/vendor-pyodide.mjs on every `npm install`), with the
// jsdelivr CDN as a fallback if the local copy is missing. Previously it
// came from the CDN only, so anyone whose network, ad-blocker or firewall
// couldn't reach cdn.jsdelivr.net got "pyodide.js failed to load" and
// could never run Python at all.
//
// The npm package contains ONLY the core -- not numpy/matplotlib/etc.
// Those wheels are still downloaded from the CDN (PACKAGE_BASE below), and
// only when a run actually imports them. To serve them locally too,
// extract the full release tarball from github.com/pyodide/pyodide/releases
// into public/pyodide/ and set PACKAGES_FROM_LOCAL = true.
//
// Pyodide itself is only loaded the first time run() is called, not on
// worker startup -- so creating the worker is instant, and the ~10 MB
// WASM runtime download only happens once someone actually clicks Run.
const PYODIDE_VERSION = "314.0.2"; // Python 3.14.x. Keep in lockstep with the "pyodide" dependency in package.json -- scripts/vendor-pyodide.mjs fails the install if they differ.
const LOCAL_BASE = "/pyodide/";
const CDN_BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const PACKAGES_FROM_LOCAL = false;
const PACKAGE_BASE = PACKAGES_FROM_LOCAL ? LOCAL_BASE : CDN_BASE;
const CORE_SOURCES = [
  { label: "self-hosted", base: LOCAL_BASE },
  { label: "jsdelivr CDN", base: CDN_BASE },
];

let pyodideReadyPromise = null;

function errText(err) {
  return (err && err.message) ? err.message : String(err);
}

// Try each core source in order; the first one that fully initialises wins.
// Every failure is collected so the final error says exactly what was tried
// (which is what the user sees in the Run panel).
async function loadCore() {
  const failures = [];
  for (const source of CORE_SOURCES) {
    try {
      self.importScripts(source.base + "pyodide.js");
      return await self.loadPyodide({ indexURL: source.base, packageBaseUrl: PACKAGE_BASE });
    } catch (err) {
      failures.push(`${source.label} (${source.base}pyodide.js): ${errText(err)}`);
    }
  }
  throw new Error(
    `Could not load the Python runtime (Pyodide ${PYODIDE_VERSION}). ${failures.join(" | ")}. ` +
      "If the self-hosted copy failed, run `npm install` in frontend/ to copy it into public/pyodide/; " +
      "if the CDN failed too, check that cdn.jsdelivr.net is reachable (firewall, VPN, ad-blocker)."
  );
}

// A failed load must not be cached: a rejected promise stored here would
// make every later Run fail instantly until the page is reloaded, even
// after the network comes back.
function getPyodide() {
  if (!pyodideReadyPromise) {
    pyodideReadyPromise = loadCore()
      .then((pyodide) => {
        // Must be set before matplotlib is ever imported (user code can
        // `import matplotlib.pyplot` itself); MPLBACKEND is read at import.
        pyodide.runPython('import os; os.environ["MPLBACKEND"] = "agg"');
        return pyodide;
      })
      .catch((err) => {
        pyodideReadyPromise = null;
        throw err;
      });
  }
  return pyodideReadyPromise;
}

// numpy/matplotlib are NOT preloaded any more: a plain `print("hi")` used
// to wait on a multi-MB matplotlib+numpy download. Packages the code
// imports are detected and loaded on demand (and stay loaded for later
// runs on this worker). `plt` is also injected into user code as a
// convenience (see wrapper below), so a bare mention of it needs
// matplotlib even without an import statement.
async function loadPackagesFor(pyodide, code) {
  // loadPackagesFromImports()/loadPackage() do NOT throw when a download
  // fails -- they only report through errorCallback and carry on, so the run
  // would proceed and surface as a misleading "ModuleNotFoundError: No
  // module named 'numpy'". Collect those reports and fail loudly instead.
  const problems = [];
  const options = { errorCallback: (msg) => problems.push(msg) };
  try {
    await pyodide.loadPackagesFromImports(code, options);
    if (!pyodide.loadedPackages.matplotlib && /\b(plt|pyplot|matplotlib)\b/.test(code)) {
      await pyodide.loadPackage("matplotlib", options);
    }
  } catch (err) {
    problems.push(errText(err));
  }
  if (problems.length) {
    throw new Error(`Could not download the Python packages this code imports from ${PACKAGE_BASE}: ${problems.join(" ")}`);
  }
}

self.onmessage = async (event) => {
  const { id, code } = event.data || {};
  try {
    const pyodide = await getPyodide();
    await loadPackagesFor(pyodide, code || "");

    // Capture stdout/stderr into per-run buffers rather than letting them
    // hit the browser console -- each run gets fresh buffers so output
    // from an earlier run on the same worker never leaks into this one.
    let stdoutBuf = "";
    let stderrBuf = "";
    pyodide.setStdout({ batched: (s) => { stdoutBuf += s + "\n"; } });
    pyodide.setStderr({ batched: (s) => { stderrBuf += s + "\n"; } });

    // The user's code is handed to Python as a real string value (via
    // pyodide.globals.set), not textually embedded into a wrapper
    // script -- avoids both the indentation-corrupts-triple-quoted-
    // strings problem of line-prefixing user code, and the quote-
    // escaping problem of interpolating it into a Python string literal.
    pyodide.globals.set("_artifact_code", code || "");
    pyodide.globals.set("_uses_plots", !!pyodide.loadedPackages.matplotlib);

    const wrapper = `
import traceback, base64, io, json as _json

_globals = {"__name__": "__main__"}
if _uses_plots:
    import matplotlib
    matplotlib.use("AGG")
    import matplotlib.pyplot as plt
    _globals["plt"] = plt

try:
    exec(_artifact_code, _globals)
except Exception:
    print(traceback.format_exc())

_images = []
if _uses_plots:
    for _num in plt.get_fignums():
        _fig = plt.figure(_num)
        _buf = io.BytesIO()
        _fig.savefig(_buf, format="png", bbox_inches="tight")
        _buf.seek(0)
        _images.append(base64.b64encode(_buf.read()).decode("ascii"))
    plt.close("all")
_json.dumps(_images)
`;
    const imagesJson = await pyodide.runPythonAsync(wrapper);
    const images = JSON.parse(imagesJson);

    self.postMessage({ id, status: "ok", stdout: stdoutBuf, stderr: stderrBuf, images });
  } catch (err) {
    self.postMessage({ id, status: "error", error: (err && err.message) ? err.message : String(err) });
  }
};
