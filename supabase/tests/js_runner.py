"""Helper for the tests: run a little JavaScript with "jsc", the JavaScript
engine built into every Mac, and read back the JSON it prints.

The Edge Functions' logic lives in plain .js files (alert_rule.js,
alert_job.js, commands.js) precisely so it can be tested this way, with
nothing to install. On computers without jsc, the JavaScript tests are skipped.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

FUNCTIONS = Path(__file__).resolve().parent.parent / "functions"

JSC = next((p for p in (
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc",
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc",
) if Path(p).exists()), None) or shutil.which("jsc")


class RunsJavaScript:
    """Mix into a unittest.TestCase. Set IMPORTS = {"name": "folder/file.js"}."""

    IMPORTS = {}

    def run_js(self, body):
        imports = "".join(f'import * as {name} from "{(FUNCTIONS / path).as_posix()}";\n'
                          for name, path in self.IMPORTS.items())
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "check.mjs"
            script.write_text(imports + body + "\n", encoding="utf-8")
            result = subprocess.run([JSC, "-m", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)
