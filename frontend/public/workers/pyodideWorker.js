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
// Pyodide is loaded from its own CDN (jsdelivr) the first time run() is
// called, not on worker startup -- so creating the worker is instant, and
// the ~10-20s / several-MB WASM runtime download only happens once
// someone actually clicks Run on a python artifact.
const PYODIDE_VERSION = "314.0.2"; // Python 3.14.2 -- bump this string if a newer Pyodide release is needed later.
const PYODIDE_CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/pyodide.js`;

let pyodideReadyPromise = null;

function loadPyodideAndPackages() {
  if (!pyodideReadyPromise) {
    self.importScripts(PYODIDE_CDN);
    pyodideReadyPromise = self.loadPyodide().then(async (pyodide) => {
      // matplotlib/numpy cover the "plots/animations render fully
      // client-side" promise from the guide's own CO2 table; loaded once
      // here rather than per-run.
      await pyodide.loadPackage(["matplotlib", "numpy"]);
      return pyodide;
    });
  }
  return pyodideReadyPromise;
}

self.onmessage = async (event) => {
  const { id, code } = event.data || {};
  try {
    const pyodide = await loadPyodideAndPackages();

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

    const wrapper = `
import traceback, base64, io, json as _json
import matplotlib
matplotlib.use("AGG")
import matplotlib.pyplot as plt

try:
    exec(_artifact_code, {"plt": plt, "__name__": "__main__"})
except Exception:
    print(traceback.format_exc())

_images = []
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
