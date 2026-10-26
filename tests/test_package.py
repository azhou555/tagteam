import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import tests.helpers  # noqa: F401 (sets TAGTEAM_HOME)
from tagteam import resources

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Package(unittest.TestCase):
    def test_resources_load_from_any_cwd(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(tempfile.mkdtemp())
        self.assertIn("review", tomllib.loads(resources.text("config.default.toml")))
        self.assertIn("preamble", tomllib.loads(resources.text("rubrics.toml")))
        self.assertTrue(resources.path("findings.schema.json").is_file())

    @unittest.skipUnless(shutil.which("uv"), "uv not installed")
    def test_installed_entry_point_runs(self):
        tmp = tempfile.mkdtemp()
        env = {**os.environ, "UV_TOOL_DIR": os.path.join(tmp, "tools"), "UV_TOOL_BIN_DIR": os.path.join(tmp, "bin"),
               "TAGTEAM_HOME": os.path.join(tmp, "home")}
        subprocess.run(["uv", "tool", "install", "--force", "-e", REPO], env=env, check=True, capture_output=True)
        r = subprocess.run([os.path.join(tmp, "bin", "tagteam"), "status"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
