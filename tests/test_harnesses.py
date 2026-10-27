import unittest

import tests.helpers  # noqa: F401 (sets TAGTEAM_HOME)
from tagteam import resources
from tagteam.harnesses import ALL, detected, get

SCHEMA = str(resources.path("findings.schema.json"))
AGY_P = ("P\nIMPORTANT: shell/terminal commands are disabled in this session and any attempt aborts the review. "
         "Do not run commands (no git, cat, ls, grep). The diff above has what you need; use only file-viewing "
         "tools if you must read more.")


class Registry(unittest.TestCase):
    def test_registry_roles(self):
        self.assertEqual(set(ALL), {"claude", "codex", "copilot", "agy"})
        for name, h in ALL.items():
            self.assertEqual(h.name, name)
            self.assertIsNotNone(h.author, name)
            self.assertIsNotNone(h.reviewer, name)
            self.assertEqual(h.author.has_plan_gate, name == "claude", name)
        self.assertIs(get("agy"), ALL["agy"])
        self.assertTrue(set(detected()) <= set(ALL))

    def test_argv_unchanged(self):
        opts = {"model": "m"}
        want = {
            "claude": (["claude", "-p", "--output-format", "json", "--json-schema", resources.text("findings.schema.json"),
                        "--permission-mode", "plan", "--disallowedTools", "Write", "Edit", "NotebookEdit",
                        "--model", "m"], "P", None),
            "codex": (["codex", "exec", "-s", "read-only", "-c", 'approval_policy="never"', "--skip-git-repo-check",
                       "--output-schema", SCHEMA, "-o", "/w/out.json", "-m", "m", "-"], "P", "/w/out.json"),
            "copilot": (["copilot", "-p", "P", "-s", "--no-ask-user", "--allow-all-tools", "--deny-tool", "write",
                         "--deny-tool", "shell", "--usage-output-file", "/w/usage.json", "--model", "m"], None, None),
            "agy": (["agy", "-p", AGY_P, "--mode", "plan", "--output-format", "json", "--json-schema", SCHEMA,
                     "--print-timeout", "100s", "--model", "m"], None, None),
        }
        for a, expected in want.items():
            self.assertEqual(get(a).reviewer.argv("P", opts, "/w", 100), expected, a)
        self.assertEqual(get("agy").reviewer.argv("P", {"effort": "high"}, "/w", 100)[0][-2:], ["--effort", "high"])

    def test_shipped_fixtures_parse_as_turn_end(self):
        for a, h in ALL.items():
            ev = h.author.parse("turn-end", resources_fixture(a))
            self.assertEqual((ev.agent, ev.phase), (a, "turn-end"), a)


def resources_fixture(agent):
    import json
    return json.loads(resources.text(f"fixtures/{agent}-stop.json"))


if __name__ == "__main__":
    unittest.main()
