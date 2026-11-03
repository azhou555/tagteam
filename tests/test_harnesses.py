import json
import unittest

from tests.helpers import FIX, fx  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import resources
from tagteam.harnesses import ALL, detected, get

SCHEMA = str(resources.path("findings.schema.json"))
AGY_P = ("P\nIMPORTANT: shell/terminal commands are disabled in this session and any attempt aborts the review. "
         "Do not run commands (no git, cat, ls, grep). The diff above has what you need; use only file-viewing "
         "tools if you must read more.")


class Registry(unittest.TestCase):
    def test_registry_roles(self):
        self.assertEqual(set(ALL), {"claude", "codex", "copilot", "agy", "opencode"})
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
                        "--setting-sources", "user", "--model", "m"], "P", None),
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


class OpenCode(unittest.TestCase):
    """Payloads are what tagteam's plugin sends, captured live from OpenCode 1.19.5."""

    def test_parse(self):
        oc = get("opencode").author
        ev = oc.parse("turn-start", fx("opencode-prompt"))
        self.assertEqual((ev.agent, ev.session_id, ev.cwd), ("opencode", "ses_e6b0d6decffekHf2z3qfod1Rg8",
                                                             "/tmp/spike/ws-opencode"))
        self.assertEqual(ev.prompt, "Add a function mul(a, b) to m.py with the edit tool, then create todo.txt "
                                    "containing the word later with the write tool. Be brief.")   # run's quotes gone
        follow_up = oc.parse("turn-start", {"sessionID": "s", "cwd": "/r", "prompt": "[tagteam] Cross-review by x"})
        self.assertEqual(follow_up.prompt, "[tagteam] Cross-review by x")   # so turn-start keeps the round count
        self.assertEqual(oc.parse("touch", fx("opencode-touch-edit")).paths, ["/tmp/spike/ws-opencode/m.py"])
        self.assertEqual(oc.parse("touch", fx("opencode-touch-write")).paths, ["/tmp/spike/ws-opencode/todo.txt"])
        self.assertEqual(oc.parse("touch", {"sessionID": "s", "cwd": "/r", "tool": "apply_patch",
                                            "args": {"patchText": "*** Update File: a/b.py\n"}}).paths, ["/r/a/b.py"])
        self.assertEqual(oc.parse("turn-end", fx("opencode-stop")).phase, "turn-end")

    def test_encode_is_what_the_plugin_reads(self):
        enc = get("opencode").author.encode
        self.assertEqual(json.loads(enc("turn-end", {"action": "block", "message": "fix"})),
                         {"decision": "block", "reason": "fix"})
        self.assertEqual(json.loads(enc("turn-start", {"action": "context", "message": "m", "notice": "n"})),
                         {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "m"},
                          "systemMessage": "n"})
        self.assertEqual(json.loads(enc("turn-end", {"action": "allow", "message": "bg"})), {"systemMessage": "bg"})

    def test_plugin_carries_each_hook_command(self):
        js = get("opencode").author.install("/opt/bin/tagteam", 540, True)[2]
        self.assertNotIn("__", js.split("\n", 3)[3].split("function call")[0])     # no placeholder left
        self.assertIn('const HOOKS = {"turn-start": ": tagteam-hook-form=1; /opt/bin/tagteam hook opencode turn-start", '
                      '"touch": ": tagteam-hook-form=1; /opt/bin/tagteam hook opencode touch", '
                      '"turn-end": ": tagteam-hook-form=1; /opt/bin/tagteam hook opencode turn-end"}', js)
        self.assertIn('const TIMEOUT_S = {"turn-start": 30, "touch": 30, "turn-end": 540}', js)

    def test_reviewer(self):
        rv = get("opencode").reviewer
        argv, stdin, out = rv.argv("P", {"model": "opencode/m", "effort": "high"}, "/w", 100)
        self.assertEqual(argv[:5], ["env", "-u", "PWD", "OPENCODE_DISABLE_PROJECT_CONFIG=1", "OPENCODE_CONFIG_CONTENT=" + json.dumps(
            {"snapshot": False, "permissions": READ_ONLY, "agents": {"tagteam-review": {"mode": "primary", "description": "tagteam's "
                                                                 "read-only reviewer", "permissions": READ_ONLY}}})])
        self.assertEqual(argv[5:], ["opencode", "run", "--standalone", "--agent", "tagteam-review", "--title",
                                    "tagteam review", "--format", "json", "-m", "opencode/m#high", "--", "P"])
        self.assertEqual(rv.argv("P", {"effort": "high"}, "/w", 100)[0][-2:], ["--", "P"])     # a variant needs a model
        self.assertEqual((stdin, out), (None, None))
        stdout = (FIX / "opencode-review.jsonl").read_text()
        result = rv.parse(stdout, None)
        self.assertEqual((result["verdict"], [(f["severity"], f["file"], f["line"]) for f in result["findings"]]),
                         ("changes_requested", [("blocker", "a.py", 3)]))
        self.assertEqual(rv.usage(stdout, "/w"), {"input_tokens": 7171, "output_tokens": 245, "cost_usd": 0.0})
        split = "\n".join(json.dumps({"type": "text", "part": {"text": t}})
                          for t in ('Looking at the diff. {"findings": [', '], "verdict": "approve"}'))
        self.assertEqual(rv.parse(split, None), {"verdict": "approve", "summary": "", "findings": []})
        self.assertIsNone(rv.parse("not json\n" + json.dumps({"type": "text", "part": {"text": "no findings"}}), None))
        self.assertEqual(rv.usage("", "/w"), {})


READ_ONLY = [{"action": "*", "resource": "*", "effect": "deny"}, {"action": "read", "resource": "*", "effect": "allow"},
             {"action": "glob", "resource": "*", "effect": "allow"}, {"action": "grep", "resource": "*", "effect": "allow"}]


def resources_fixture(agent):
    import json
    return json.loads(resources.text(f"fixtures/{agent}-stop.json"))


if __name__ == "__main__":
    unittest.main()
