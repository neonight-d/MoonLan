"""The page's own files as text, for the tests that hold the interface
to what it says. The JavaScript is not run here; where a test needs to
run a function of it, it does so through `node` when there is one."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
APP = (WEB / "app.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
STYLE = (WEB / "style.css").read_text(encoding="utf-8")


def strings(lang):
    """key -> text of one language, the "…" + "…" pieces joined"""
    body = re.search(rf"^  {lang}: {{$(.*?)^  }},$", I18N, re.S | re.M).group(1)
    parts = re.split(r'^    "?([\w+.-]+)"?:', body, flags=re.M)
    return {
        key: "".join(
            json.loads('"' + piece + '"')
            for piece in re.findall(r'"((?:[^"\\]|\\.)*)"', expr)
        )
        for key, expr in zip(parts[1::2], parts[2::2])
    }


def function(name):
    """The source of a top-level function of app.js, braces and all"""
    body = APP[APP.index(f"function {name}("):]
    return body[:body.index("\n}\n") + 2]


# GitHub's runners have it; where it is missing, the tests that run the
# page's functions are skipped
NODE = shutil.which("node")


def js_script(names, expression):
    """The named functions of app.js, then the JSON of `expression`"""
    return (
        "\n".join(function(name) for name in names)
        + "\nconsole.log(JSON.stringify(" + expression + "));"
    )


def run_js(names, expression):
    """Runs the named functions of app.js through node and gives back
    what `expression` comes to"""
    done = subprocess.run([NODE, "-e", js_script(names, expression)],
                          capture_output=True, text=True, timeout=30,
                          check=True)
    return json.loads(done.stdout)


# ---------- the whole page, without a browser (v0.8.1) ----------

# Every element, the DOM and vis are stubs that take any call and give
# back another stub; fetch never answers and a repeating timer never
# starts. What runs is the page's own code — i18n.js and app.js as the
# browser loads them — on data the test hands it as __DATA, and the
# test's code ends with __done(value). The same script runs in a Web
# Worker (no DOM either), which is how it was tried in a browser.
PAGE_PRELUDE = r"""
const __stub = () => new Proxy(function () {}, {
  get(target, key) {
    if (key === Symbol.toPrimitive) return () => 0;
    if (key === Symbol.iterator) return function* () {};
    if (key === "then") return undefined;
    return __stub();
  },
  apply: () => __stub(),
  construct: () => __stub(),
  set: () => true,
});
const __define = (name, value) => {
  try {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  } catch (e) { /* kept as it is */ }
};
__define("window", globalThis);
__define("document", __stub());
__define("vis", __stub());
__define("localStorage", (() => {
  const kept = new Map();
  return {
    getItem: (key) => (kept.has(key) ? kept.get(key) : null),
    setItem: (key, value) => kept.set(key, String(value)),
    removeItem: (key) => kept.delete(key),
  };
})());
__define("fetch", () => new Promise(() => {}));
__define("setInterval", () => 0);
__define("requestAnimationFrame", () => 0);
if (typeof globalThis.addEventListener !== "function") __define("addEventListener", () => {});
if (typeof navigator === "undefined") __define("navigator", { language: "en", userAgent: "node" });
if (typeof location === "undefined") {
  __define("location", { protocol: "http:", host: "localhost", hostname: "localhost",
                         href: "http://localhost/", origin: "http://localhost", reload() {} });
}
const __done = (value) => {
  if (typeof process !== "undefined" && process.stdout) {
    // out once it is written: the page has left a timer or two behind
    process.stdout.write(JSON.stringify(value) + "\n", () => process.exit(0));
    return;
  }
  postMessage(JSON.parse(JSON.stringify(value)));
};
"""


def page_script(test_code, data=None):
    """The page, the data as __DATA, then the test's code"""
    return (
        PAGE_PRELUDE
        + "\nconst __DATA = " + json.dumps(data) + ";\n"
        + I18N + "\n" + APP + "\n"
        + test_code + "\n"
    )


def run_page(test_code, data=None):
    """Runs the whole page through node with the test's code after it,
    and gives back what that code passed to __done"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.js"
        path.write_text(page_script(test_code, data), encoding="utf-8")
        done = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                              timeout=60, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


# One scan of the demo network in a process of its own — the service
# reads its configuration once, at import, and the tests' own is not a
# demo — and /api/topology as the page receives it
DEMO_SCAN = r"""
import asyncio, json, logging
logging.disable(logging.CRITICAL)
from moonlan import demo, server
from asgi_client import call


async def scan():
    await asyncio.to_thread(demo.seed_notes, server.db)
    await server.run_scan()

asyncio.run(scan())
print(json.dumps(call(server.app, "GET", "/api/topology").json()))
"""


def demo_topology():
    root = WEB.parent
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text(f"db_path: {Path(tmp) / 'moonlan.db'}\n", encoding="utf-8")
        env = {**os.environ, "MOONLAN_DEMO": "1", "MOONLAN_CONFIG": str(config),
               "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")])}
        done = subprocess.run([sys.executable, "-c", DEMO_SCAN], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])
