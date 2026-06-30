import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cfc.py"


def run(args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class CfCTest(unittest.TestCase):
    def make_repo(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return td, root

    def test_init_start_check_pass_done(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        self.assertEqual(run(["init", "--root", str(root)]).returncode, 0)
        res = run(["start", "--root", str(root), "Edit app", "--allow", "src/app.py", "--verify", "python3 -m py_compile src/app.py"])
        self.assertEqual(res.returncode, 0, res.stderr)
        (root / "src" / "app.py").write_text("print('hello')\n")
        res = run(["check", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("**PASS**", res.stdout)
        res = run(["learn", "--root", str(root)])
        self.assertEqual(res.returncode, 0)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        review = rd / "REVIEW.iteration-1.md"
        review.write_text("Verdict: PASS\n\n## BLOCKERS\n- none\n")
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run(["done", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_done_refuses_changed_run_without_independent_review(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Needs review", "--allow", "src/app.py"])
        (root / "src" / "app.py").write_text("print('changed')\n")
        res = run(["check", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run(["done", "--root", str(root)])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Independent review result missing", res.stderr)

    def test_done_refuses_while_awaiting_external_agent(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Awaiting run", "--allow", "src/app.py"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        run_data = json.loads((rd / "RUN.json").read_text())
        run_data["awaiting"] = {"phase": "reviewer", "target": "review:0.0"}
        (rd / "RUN.json").write_text(json.dumps(run_data))
        (rd / "CHECK.md").write_text("# check\n")
        res = run(["done", "--root", str(root)])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("still awaiting external agent", res.stderr)

    def test_forbidden_file_fails_and_learn_applies(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Bad edit", "--allow", "src/app.py", "--forbid", "AGENTS.md"])
        (root / "AGENTS.md").write_text("oops\n")
        res = run(["check", "--root", str(root)])
        self.assertIn("**FAIL**", res.stdout)
        res = run(["learn", "--root", str(root), "--apply"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((root / ".cfc" / "wiki" / "guardrails" / "no-surprise-files-outside-task-scope.md").exists())

    def test_root_done_md_is_forbidden_by_default(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "No root done", "--allow", "src/app.py"])
        (root / "DONE.md").write_text("# DONE\n")
        res = run(["check", "--root", str(root)])
        self.assertIn("**FAIL**", res.stdout)
        self.assertIn("DONE.md", res.stdout)
        self.assertIn("repository report artifact created outside .cfc", res.stdout)

    def test_nested_done_md_is_not_globally_forbidden(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Nested done allowed", "--allow", "src/**"])
        (root / "src" / "DONE.md").write_text("# Legit nested doc\n")
        res = run(["check", "--root", str(root)])
        self.assertNotIn("repository report artifact created outside .cfc", res.stdout)
        self.assertNotIn("forbidden files changed", res.stdout)

    def test_start_refuses_preexisting_root_done_even_with_allow_dirty(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        (root / "DONE.md").write_text("# stale report\n")
        res = run(["start", "--root", str(root), "Blocked by stale report", "--allow-dirty"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("repository report artifacts", res.stderr)
        self.assertIn("DONE.md", res.stderr)

    def test_explicit_learn_apply_loads_next_prompt_wiki(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Bad edit", "--allow", "src/app.py", "--forbid", "AGENTS.md"])
        (root / "AGENTS.md").write_text("oops\n")
        res = run(["check", "--root", str(root)])
        self.assertIn("**FAIL**", res.stdout)
        res = run(["learn", "--root", str(root), "--apply"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("No surprise files outside task scope", res.stdout)
        wiki_page = root / ".cfc" / "wiki" / "guardrails" / "no-surprise-files-outside-task-scope.md"
        self.assertTrue(wiki_page.exists())
        res = run(["done", "--root", str(root), "--force", "--no-auto-learn"])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        self.assertIsNone(cur["run_id"])
        res = run(["start", "--root", str(root), "Next prompt", "--allow-dirty", "--allow", "src/app.py"])
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run(["gjc", "--root", str(root), "do it"])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        prompt = (root / ".cfc" / "runs" / cur["run_id"] / "PROMPT.iteration-1.md").read_text()
        self.assertIn("Applicable CfC Wiki Knowledge", prompt)
        self.assertIn("No surprise files outside task scope", prompt)

    def test_force_done_does_not_auto_apply_high_severity_learn(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Bad edit", "--allow", "src/app.py", "--forbid", "AGENTS.md"])
        (root / "AGENTS.md").write_text("oops\n")
        res = run(["check", "--root", str(root)])
        self.assertIn("**FAIL**", res.stdout)
        res = run(["done", "--root", str(root), "--force"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Wrote LEARN.md", res.stdout)
        self.assertNotIn("Applied 1 high-confidence learn candidate", res.stdout)
        self.assertFalse((root / ".cfc" / "wiki" / "guardrails" / "no-surprise-files-outside-task-scope.md").exists())

    def test_prompt_includes_active_wiki(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        gd = root / ".cfc" / "wiki" / "guardrails"
        gd.mkdir(parents=True, exist_ok=True)
        (gd / "preserve-payload.md").write_text("---\ntype: Guardrail\ntitle: Preserve payload\nstatus: active\n---\n# Prompt Patch\nKeep payload stable.\n")
        run(["start", "--root", str(root), "Prompt test"])
        res = run(["gjc", "--root", str(root), "do it"])
        self.assertEqual(res.returncode, 0)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        prompt = (root / ".cfc" / "runs" / cur["run_id"] / "PROMPT.iteration-1.md").read_text()
        self.assertIn("Preserve payload", prompt)
        self.assertIn("Keep payload stable", prompt)
        self.assertIn("Pre-Edit Minimality Gate", prompt)
        self.assertIn("Does the requested behavior already exist?", prompt)
        self.assertIn("Can this be a one-line or tiny localized change?", prompt)
        self.assertIn("Prefer deletion or wiring over addition", prompt)
        self.assertIn("Do not create AGENTS.md, DONE.md", prompt)
        self.assertIn("Report these items in the GJC chat/pane only", prompt)

    def test_dirty_start_refuses_without_allow_dirty(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        (root / "src" / "app.py").write_text("print('dirty')\n")
        res = run(["start", "--root", str(root), "Dirty run"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Refusing to start on a dirty worktree", res.stderr)
        res = run(["start", "--root", str(root), "Dirty run", "--allow-dirty"])
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_active_run_requires_replace(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        self.assertEqual(run(["start", "--root", str(root), "One"]).returncode, 0)
        res = run(["start", "--root", str(root), "Two"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Active CfC run already exists", res.stderr)
        res = run(["start", "--root", str(root), "Two", "--replace"])
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_rename_checks_old_and_new_paths(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "secret.txt").write_text("secret\n")
        subprocess.run(["git", "add", "secret.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "secret"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Rename", "--allow", "allowed.txt", "--forbid", "secret.txt"])
        subprocess.run(["git", "mv", "secret.txt", "allowed.txt"], cwd=root, check=True)
        res = run(["check", "--root", str(root)])
        self.assertIn("**FAIL**", res.stdout)
        self.assertIn("secret.txt", res.stdout)

    def test_pass_review_with_empty_blocker_list_does_not_create_repair_runbook(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Review learn"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        (rd / "REVIEW.iteration-1.md").write_text("Verdict: PASS\n\nBLOCKER list: none\n\nMAJOR: none\n")
        res = run(["learn", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("Blocker repair loop", res.stdout)

    def test_empty_review_result_is_blocked(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Empty review"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        review = rd / "REVIEW.iteration-1.md"
        review.write_text("")
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = json.loads(res.stdout)
        self.assertEqual(parsed["verdict"], "REVIEW_BLOCKED")
        self.assertIn("review produced no output", parsed["blockers"])

    def test_blocked_review_classification_generates_learn_without_auto_applying_failure(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Reviewer incomplete"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        review = rd / "REVIEW.iteration-1.md"
        review.write_text(
            "Verdict: REVIEW_BLOCKED\n\n"
            "## BLOCKERS\n"
            "- Independent tmux reviewer did not produce a final verdict after repeated waits. CfC check passed, but independent review evidence is incomplete.\n"
        )
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((rd / "LEARN.md").exists())
        learn = (rd / "LEARN.md").read_text()
        self.assertIn("Wait for reviewer verdict before classifying", learn)
        wiki_page = root / ".cfc" / "wiki" / "failures" / "wait-for-reviewer-verdict-before-classifying.md"
        self.assertFalse(wiki_page.exists())
        parsed = json.loads(res.stdout)
        self.assertEqual(parsed["verdict"], "REVIEW_BLOCKED")

    def test_review_blocked_without_bullets_still_blocks_done(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import sys\nsys.stdin.read()\nprint('ok')\n")
        (root / "reviewer.py").write_text("import sys\nsys.stdin.read()\nprint('Verdict: REVIEW_BLOCKED')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Blocked verdict",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
            "--max-iterations", "1",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        self.assertNotIn("last_run_id", cur)
        rd = next((root / ".cfc" / "runs").iterdir())
        self.assertFalse((rd / "DONE.md").exists())
        self.assertIn("review returned REVIEW_BLOCKED", (rd / "BLOCKERS.md").read_text())

    def test_unsupported_review_verdict_is_blocked(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Unsupported review verdict"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        review = rd / "REVIEW.iteration-1.md"
        review.write_text("Verdict: FAIL\n\n## BLOCKERS\n- none\n")
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = json.loads(res.stdout)
        self.assertEqual(parsed["verdict"], "REVIEW_BLOCKED")
        self.assertIn("unsupported Verdict: FAIL", parsed["blockers"][0])

    def test_learn_ignores_review_prompt_iteration_files(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Learn prompt ignore"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        (rd / "REVIEW_PROMPT.iteration-1.md").write_text("Verdict: REVIEW_BLOCKED\n\n## BLOCKERS\n- prompt text only\n")
        (rd / "REVIEW.iteration-1.md").write_text("Verdict: PASS\n\n## BLOCKERS\n- none\n")
        res = run(["learn", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("Blocker repair loop", res.stdout)

    def test_classify_review_extracts_blockers(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Review blocked"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        review = rd / "REVIEW.iteration-1.md"
        review.write_text("Verdict: REVIEW_BLOCKED\n\n## BLOCKERS\n- verification missing\n")
        run_data = json.loads((rd / "RUN.json").read_text())
        run_data["awaiting"] = {"phase": "reviewer", "target": "review:0.0"}
        (rd / "RUN.json").write_text(json.dumps(run_data))
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = json.loads(res.stdout)
        self.assertEqual(parsed["verdict"], "REVIEW_BLOCKED")
        run_after = json.loads((rd / "RUN.json").read_text())
        self.assertNotIn("awaiting", run_after)
        self.assertEqual(parsed["blockers"], ["verification missing"])
        self.assertIn("verification missing", (rd / "BLOCKERS.md").read_text())

    def test_repair_generates_prompt_from_review_blockers(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Repair"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        (rd / "REVIEW.iteration-1.md").write_text("Verdict: REVIEW_BLOCKED\n\n## BLOCKERS\n- fix syntax\n")
        res = run(["repair", "--root", str(root), "--iteration", "1"])
        self.assertEqual(res.returncode, 0, res.stderr)
        prompt = (rd / "REPAIR_PROMPT.iteration-1.md").read_text()
        self.assertIn("fix syntax", prompt)
        self.assertIn("Pre-Edit Minimality Gate", prompt)
        self.assertIn("Before editing, repeat the Pre-Edit Minimality Gate specifically for each blocker", prompt)
        self.assertIn("what is the smallest repair", prompt)

    def test_loop_with_command_agents_finishes_and_learns(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        executor = Path(root) / "executor.py"
        reviewer = Path(root) / "reviewer.py"
        executor.write_text("import sys\n_ = sys.stdin.read()\nprint('executor done')\n")
        reviewer.write_text("import sys\n_ = sys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Command loop",
            "--allow", "src/app.py",
            "--verify", "python3 -m py_compile src/app.py",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        self.assertIsNone(cur["run_id"])
        last = cur["last_run_id"]
        rd = root / ".cfc" / "runs" / last
        self.assertTrue((rd / "DONE.md").exists())
        self.assertTrue((rd / "REVIEW.iteration-1.md").exists())

    def test_no_args_prints_headless_plugin_help(self):
        res = subprocess.run([sys.executable, str(SCRIPT)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("headless recursive agent controller", res.stdout)
        self.assertIn("cfc plugin run", res.stdout)
        self.assertNotIn("❯", res.stdout)

    def test_scripts_do_not_embed_private_absolute_paths(self):
        script_root = SCRIPT.parent
        files = [SCRIPT, *sorted((script_root / "cfc_lib").glob("*.py"))]
        private_prefix = "/" + "Users" + "/"
        offenders = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            if private_prefix in text:
                offenders.append(str(path.relative_to(SCRIPT.parents[1])))
        self.assertEqual(offenders, [])

    def test_plugin_manifest_is_machine_readable(self):
        res = run(["plugin", "manifest"])
        self.assertEqual(res.returncode, 0, res.stderr)
        manifest = json.loads(res.stdout)
        self.assertEqual(manifest["name"], "cfc")
        self.assertIn("run", manifest["commands"])
        self.assertIn("status", manifest["commands"])

    def test_plugin_status_reports_active_run_json(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Existing"])
        res = run(["plugin", "status", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["is_git_repo"])
        self.assertEqual(payload["active_run"]["title"], "Existing")
        self.assertEqual(payload["active_run"]["status"], "active")

    def test_plugin_cancel_clears_awaiting_state(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Awaiting cancel"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        run_data = json.loads((rd / "RUN.json").read_text())
        run_data["awaiting"] = {"phase": "reviewer", "target": "review:0.0"}
        (rd / "RUN.json").write_text(json.dumps(run_data))
        res = run(["plugin", "cancel", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)
        run_after = json.loads((rd / "RUN.json").read_text())
        self.assertEqual(run_after["status"], "cancelled")
        self.assertIn("cancelled_at", run_after)
        self.assertNotIn("awaiting", run_after)
        status = json.loads(run(["plugin", "status", "--root", str(root)]).stdout)
        self.assertIsNone(status["active_run"])

    def test_plugin_status_reports_nested_repos_for_workspace_root(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        workspace = Path(td.name)
        app = workspace / "app"
        app.mkdir()
        subprocess.run(["git", "init"], cwd=app, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run(["plugin", "status", "--root", str(workspace)])
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertFalse(payload["is_git_repo"])
        self.assertEqual(payload["error"], "not_a_git_repository")
        self.assertIn(str(app.resolve()), payload["nested_git_roots"])

    def test_plugin_run_non_git_workspace_refuses_before_init(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        workspace = Path(td.name)
        app = workspace / "app"
        app.mkdir()
        subprocess.run(["git", "init"], cwd=app, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run(["plugin", "run", "--root", str(workspace), "Do work", "--no-send"])
        self.assertNotEqual(res.returncode, 0)
        payload = json.loads(res.stderr)
        self.assertEqual(payload["error"], "not_a_git_repository")
        self.assertIn(str(app.resolve()), payload["nested_git_roots"])
        self.assertFalse((workspace / ".cfc").exists())

    def test_plugin_run_replace_supersedes_active_run(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import sys\nsys.stdin.read()\nprint('executor done')\n")
        (root / "reviewer.py").write_text("import sys\nsys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Existing"])
        res = run([
            "plugin", "run", "--root", str(root), "New task", "--replace",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Started CfC run", res.stdout)
        self.assertNotIn("Active CfC run already exists", res.stdout)
        payload = json.loads(res.stdout[res.stdout.rfind('\n{') + 1:])
        self.assertIsNone(payload["active_run"])

    def test_tracked_config_command_profiles_drive_plugin_run(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        marker = root / ".cfc" / "cheap_called.txt"
        (root / "executor.py").write_text(f"import pathlib, sys\nsys.stdin.read()\npathlib.Path({str(marker)!r}).write_text('cheap\\n')\nprint('executor done')\n")
        (root / "reviewer.py").write_text("import sys\nsys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        (root / "cfc.config.json").write_text(json.dumps({
            "version": 1,
            "adapters": {
                "mode": "command",
                "executor_profile": "cheap",
                "reviewer_profile": "codex",
                "profiles": {
                    "cheap": {"command": f"{sys.executable} executor.py"},
                    "codex": {"command": f"{sys.executable} reviewer.py"},
                },
            },
            "verification": {"commands": ["git diff --check"]},
        }))
        subprocess.run(["git", "add", "executor.py", "reviewer.py", "cfc.config.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "configured agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run(["plugin", "run", "--root", str(root), "Configured command profile"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(marker.exists())
        payload = json.loads(res.stdout[res.stdout.rfind('\n{') + 1:])
        self.assertIsNone(payload["active_run"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["last_run_id"]
        self.assertTrue((rd / "DONE.md").exists())

    def test_auto_executor_profile_escalates_complex_tasks_to_glm_profile(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        cheap_marker = root / ".cfc" / "cheap_called.txt"
        complex_marker = root / ".cfc" / "complex_called.txt"
        (root / "cheap.py").write_text(f"import pathlib, sys\nsys.stdin.read()\npathlib.Path({str(cheap_marker)!r}).write_text('cheap\\n')\n")
        (root / "complex.py").write_text(f"import pathlib, sys\nsys.stdin.read()\npathlib.Path({str(complex_marker)!r}).write_text('complex\\n')\n")
        (root / "reviewer.py").write_text("import sys\nsys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        (root / "cfc.config.json").write_text(json.dumps({
            "adapters": {
                "mode": "command",
                "executor_profile": "auto",
                "reviewer_profile": "codex",
                "profiles": {
                    "cheap": {"command": f"{sys.executable} cheap.py"},
                    "complex": {"command": f"{sys.executable} complex.py"},
                    "codex": {"command": f"{sys.executable} reviewer.py"},
                },
                "auto": {
                    "default_executor_profile": "cheap",
                    "complex_executor_profile": "complex",
                    "complex_keywords": ["async", "state"],
                },
            },
            "verification": {"commands": ["git diff --check"]},
        }))
        subprocess.run(["git", "add", "cheap.py", "complex.py", "reviewer.py", "cfc.config.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "auto profiles"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run(["plugin", "run", "--root", str(root), "Fix async state machine"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(complex_marker.exists())
        self.assertFalse(cheap_marker.exists())

    def test_cli_executor_profile_overrides_config_auto(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        cheap_marker = root / ".cfc" / "cheap_override.txt"
        complex_marker = root / ".cfc" / "complex_override.txt"
        (root / "cheap.py").write_text(f"import pathlib, sys\nsys.stdin.read()\npathlib.Path({str(cheap_marker)!r}).write_text('cheap\\n')\n")
        (root / "complex.py").write_text(f"import pathlib, sys\nsys.stdin.read()\npathlib.Path({str(complex_marker)!r}).write_text('complex\\n')\n")
        (root / "reviewer.py").write_text("import sys\nsys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        (root / "cfc.config.json").write_text(json.dumps({
            "adapters": {
                "mode": "command",
                "executor_profile": "auto",
                "reviewer_profile": "codex",
                "profiles": {
                    "cheap": {"command": f"{sys.executable} cheap.py"},
                    "complex": {"command": f"{sys.executable} complex.py"},
                    "codex": {"command": f"{sys.executable} reviewer.py"},
                },
                "auto": {"complex_keywords": ["async"]},
            },
            "verification": {"commands": ["git diff --check"]},
        }))
        subprocess.run(["git", "add", "cheap.py", "complex.py", "reviewer.py", "cfc.config.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "profile override"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run(["plugin", "run", "--root", str(root), "Fix async bug", "--executor-profile", "cheap"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(cheap_marker.exists())
        self.assertFalse(complex_marker.exists())

    def test_loop_requires_reviewer_adapter(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import pathlib, sys\nsys.stdin.read()\npathlib.Path('src/app.py').write_text('mutated\\n')\n")
        subprocess.run(["git", "add", "executor.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "executor"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        run(["init", "--root", str(root)])
        res = run(["loop", "--root", str(root), "No reviewer", "--executor-command", f"{sys.executable} executor.py", "--replace"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("requires an independent reviewer", res.stderr)
        self.assertEqual((root / "src" / "app.py").read_text(), "print('hi')\n")

    def test_reviewer_command_failure_blocks_done(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import sys\nsys.stdin.read()\nprint('ok')\n")
        (root / "bad_reviewer.py").write_text("import sys\nsys.stdin.read()\nsys.exit(2)\n")
        subprocess.run(["git", "add", "executor.py", "bad_reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Reviewer fails",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} bad_reviewer.py",
            "--max-iterations", "1",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        self.assertNotIn("last_run_id", cur)
        rd = next((root / ".cfc" / "runs").iterdir())
        self.assertFalse((rd / "DONE.md").exists())
        self.assertIn("reviewer command failed", (rd / "REVIEW.iteration-1.md").read_text())

    def test_reviewer_command_timeout_blocks_with_artifact_not_traceback(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import sys\nsys.stdin.read()\nprint('ok')\n")
        (root / "slow_reviewer.py").write_text("import sys, time\nsys.stdin.read()\ntime.sleep(5)\nprint('Verdict: PASS')\n")
        subprocess.run(["git", "add", "executor.py", "slow_reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Reviewer times out",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} slow_reviewer.py",
            "--timeout", "1",
            "--max-iterations", "1",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        rd = next((root / ".cfc" / "runs").iterdir())
        review = (rd / "REVIEW.iteration-1.md").read_text()
        self.assertIn("Verdict: REVIEW_BLOCKED", review)
        self.assertIn("reviewer command failed with exit 124", review)
        self.assertIn("command timed out after 1 seconds", review)
        self.assertFalse((rd / "DONE.md").exists())

    def test_no_review_on_check_fail_skips_reviewer(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        marker = root / "reviewer-called.txt"
        (root / "executor.py").write_text("import pathlib, sys\nsys.stdin.read()\npathlib.Path('AGENTS.md').write_text('oops\\n')\n")
        (root / "reviewer.py").write_text(f"import pathlib, sys\nsys.stdin.read()\npathlib.Path({str(marker)!r}).write_text('called\\n')\nprint('Verdict: PASS')\nprint('## BLOCKERS')\nprint('- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Skip review on failed check",
            "--allow", "src/app.py",
            "--forbid", "AGENTS.md",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
            "--no-review-on-check-fail",
            "--max-iterations", "1",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(marker.exists())
        rd = next((root / ".cfc" / "runs").iterdir())
        self.assertFalse(any(rd.glob("REVIEW.iteration-*.md")))
        self.assertFalse((rd / "DONE.md").exists())

    def test_review_prompt_includes_untracked_file_contents(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import pathlib, sys\nsys.stdin.read()\npathlib.Path('src/new.py').write_text('VALUE = 42\\n')\n")
        (root / "reviewer.py").write_text("import sys\np=sys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Untracked review",
            "--allow", "src/**",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["last_run_id"]
        prompt = (rd / "REVIEW_PROMPT.iteration-1.md").read_text()
        self.assertIn("src/new.py", prompt)
        self.assertIn("VALUE = 42", prompt)

    def test_no_diff_review_prompt_forbids_repo_reaudit_and_tests(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        (root / "executor.py").write_text("import sys\nsys.stdin.read()\nprint('read-only audit done')\n")
        (root / "reviewer.py").write_text("import sys\nsys.stdin.read()\nprint('Verdict: PASS')\nprint('')\nprint('## BLOCKERS')\nprint('- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Read-only audit",
            "--verify", "git diff --check",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["last_run_id"]
        prompt = (rd / "REVIEW_PROMPT.iteration-1.md").read_text()
        self.assertIn("Fast gate for no-diff runs", prompt)
        self.assertIn("Do not inspect the repository", prompt)
        self.assertIn("run tests, or run verification commands", prompt)
        self.assertIn("Executor Report Excerpt", prompt)

    def test_blocked_loop_executes_repair_once_per_blocker_cycle(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        counter = root.parent / (root.name + "_calls.txt")
        (root / "executor.py").write_text(f"import pathlib, sys\nsys.stdin.read()\np=pathlib.Path({str(counter)!r})\np.write_text((p.read_text() if p.exists() else '') + 'call\\n')\n")
        (root / "reviewer.py").write_text("import re, sys\np=sys.stdin.read()\nit=int(re.search(r'Iteration: (\\d+)', p).group(1))\nprint('Verdict: ' + ('REVIEW_BLOCKED' if it == 1 else 'PASS'))\nprint('')\nprint('## BLOCKERS')\nprint('- fix it' if it == 1 else '- none')\n")
        subprocess.run(["git", "add", "executor.py", "reviewer.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "agents"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = run([
            "loop", "--root", str(root), "Repair once",
            "--executor-command", f"{sys.executable} executor.py",
            "--reviewer-command", f"{sys.executable} reviewer.py",
            "--max-iterations", "2",
        ])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(counter.read_text().count("call"), 2)

    def test_tmux_send_uses_stdin_buffer_to_avoid_arg_max(self):
        spec = importlib.util.spec_from_file_location("cfc_module", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        long_prompt = "x" * 350000
        with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            module.tmux_send("target:0.0", long_prompt)

        self.assertEqual(calls[0][0], ["tmux", "load-buffer", "-"])
        self.assertEqual(calls[0][1]["input"], long_prompt)
        self.assertTrue(calls[0][1]["text"])
        self.assertNotIn(long_prompt, calls[0][0])
        self.assertEqual(calls[1][0], ["tmux", "paste-buffer", "-t", "target:0.0"])
        self.assertEqual(calls[2][0], ["tmux", "send-keys", "-t", "target:0.0", "Enter"])

    def test_capture_waits_for_reviewer_verdict_and_classifies(self):
        spec = importlib.util.spec_from_file_location("cfc_module_capture", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        module.cmd_init(argparse.Namespace(root=str(root)))
        module.cmd_start(argparse.Namespace(root=str(root), title="Await reviewer", allow=["src/app.py"], forbid=None, verify=[], tmux_target="exec:0.0", allow_dirty=False, replace=False))
        run_data, rd = module.active_run(root)
        run_data["awaiting"] = {"phase": "reviewer", "iteration": 1, "target": "review:0.0", "since": module.now_iso()}
        module.write_json(rd / "RUN.json", run_data)
        captures = [
            subprocess.CompletedProcess(["tmux"], 0, "still working\n", ""),
            subprocess.CompletedProcess(["tmux"], 0, "Verdict: PASS\n\n## BLOCKERS\n- none\n", ""),
        ]
        ns = argparse.Namespace(root=str(root), tmux_target=None, lines=100, wait_verdict=False, no_wait_verdict=False, poll_seconds=0.01, timeout_seconds=0, iteration=None)
        with mock.patch.object(module, "tmux_capture", side_effect=captures), mock.patch.object(module.time, "sleep", return_value=None):
            module.cmd_capture(ns)
        self.assertTrue((rd / "REVIEW.iteration-1.md").exists())
        run_after = json.loads((rd / "RUN.json").read_text())
        self.assertNotIn("awaiting", run_after)
        self.assertEqual(run_after["review"]["verdict"], "PASS")

    def test_capture_reviewer_timeout_writes_blocked_review_and_clears_awaiting(self):
        spec = importlib.util.spec_from_file_location("cfc_module_timeout", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        module.cmd_init(argparse.Namespace(root=str(root)))
        module.cmd_start(argparse.Namespace(root=str(root), title="Reviewer timeout", allow=["src/app.py"], forbid=None, verify=[], tmux_target="exec:0.0", allow_dirty=False, replace=False))
        run_data, rd = module.active_run(root)
        run_data["awaiting"] = {"phase": "reviewer", "iteration": 1, "target": "review:0.0", "since": module.now_iso()}
        module.write_json(rd / "RUN.json", run_data)
        ns = argparse.Namespace(root=str(root), tmux_target=None, lines=100, wait_verdict=False, no_wait_verdict=False, poll_seconds=0.01, timeout_seconds=1, iteration=None)
        with mock.patch.object(module, "wait_for_tmux_verdict", side_effect=TimeoutError("timeout")), mock.patch.object(module, "tmux_capture", return_value=subprocess.CompletedProcess(["tmux"], 0, "still reading repo\n", "")):
            module.cmd_capture(ns)
        review = (rd / "REVIEW.iteration-1.md").read_text()
        self.assertIn("Verdict: REVIEW_BLOCKED", review)
        self.assertIn("reviewer did not complete", review)
        run_after = json.loads((rd / "RUN.json").read_text())
        self.assertNotIn("awaiting", run_after)
        self.assertEqual(run_after["status"], "review_blocked")
        self.assertEqual(run_after["review"]["verdict"], "REVIEW_BLOCKED")

    def test_send_mode_without_wait_dispatches_once_before_check_review(self):
        spec = importlib.util.spec_from_file_location("cfc_module_send", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        sends = []

        def fake_send(target, text):
            sends.append((target, text))

        ns = module.default_loop_namespace("Dispatch only", root=str(root), replace=False, allow_dirty=False)
        ns.send = True
        ns.tmux_wait_seconds = 0
        ns.executor_command = None
        ns.reviewer_command = None
        ns.executor_target = "gjc:0.0"
        ns.reviewer_target = "cfc-review:0.0"
        ns.isolated_tmux = False
        with mock.patch.object(module, "tmux_send", side_effect=fake_send):
            module.cmd_loop(ns)

        self.assertEqual(len(sends), 1)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        run_data = json.loads((rd / "RUN.json").read_text())
        self.assertEqual(run_data.get("awaiting", {}).get("phase"), "executor")
        self.assertFalse((rd / "CHECK.md").exists())
        self.assertFalse(any(rd.glob("REVIEW_PROMPT.iteration-*.md")))
        ledger = (rd / "ledger.jsonl").read_text()
        self.assertIn("waiting_for_executor", ledger)

    def test_tmux_send_failure_records_send_failed_without_awaiting(self):
        spec = importlib.util.spec_from_file_location("cfc_module_send_fail", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        ns = module.default_loop_namespace("Dispatch fails", root=str(root), replace=False, allow_dirty=False)
        ns.send = True
        ns.tmux_wait_seconds = 0
        ns.executor_command = None
        ns.reviewer_command = None
        ns.executor_target = "missing:0.0"
        ns.reviewer_target = "review:0.0"
        ns.isolated_tmux = False
        with mock.patch.object(module, "tmux_send", side_effect=RuntimeError("tmux missing")):
            with self.assertRaises(SystemExit):
                module.cmd_loop(ns)
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        run_data = json.loads((rd / "RUN.json").read_text())
        self.assertEqual(run_data["status"], "send_failed")
        self.assertNotIn("awaiting", run_data)
        self.assertEqual(run_data["send_error"]["phase"], "execute_send")

    def test_isolated_tmux_creates_run_specific_targets_before_dispatch(self):
        spec = importlib.util.spec_from_file_location("cfc_module_isolated", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        sends = []
        sessions = []

        def fake_ensure(session, session_root, title):
            sessions.append((session, session_root, title))
            return f"{session}:0.0"

        def fake_send(target, text):
            sends.append((target, text))

        ns = module.default_loop_namespace("Isolated dispatch", root=str(root), replace=False, allow_dirty=False)
        ns.send = True
        ns.tmux_wait_seconds = 0
        ns.executor_command = None
        ns.reviewer_command = None
        ns.isolated_tmux = True
        with mock.patch.object(module, "ensure_gjc_tmux_session", side_effect=fake_ensure), mock.patch.object(module, "tmux_send", side_effect=fake_send):
            module.cmd_loop(ns)

        self.assertEqual(len(sessions), 2)
        self.assertTrue(sessions[0][0].startswith("cfc-"))
        self.assertTrue(sessions[0][0].endswith("-exec"))
        self.assertTrue(sessions[1][0].endswith("-review"))
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], f"{sessions[0][0]}:0.0")
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        run_data = json.loads((rd / "RUN.json").read_text())
        self.assertTrue(run_data["runner"].get("isolated_tmux"))
        self.assertEqual(run_data["runner"].get("target"), f"{sessions[0][0]}:0.0")
        self.assertEqual(run_data["runner"].get("reviewer_target"), f"{sessions[1][0]}:0.0")

    def test_review_without_required_verdict_is_blocked(self):
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        run(["init", "--root", str(root)])
        run(["start", "--root", str(root), "Missing verdict review"])
        cur = json.loads((root / ".cfc" / "current.json").read_text())
        rd = root / ".cfc" / "runs" / cur["run_id"]
        review = rd / "REVIEW.iteration-1.md"
        review.write_text("## BLOCKERS\n- none\n\nLooks fine but no required final line.\n")
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = json.loads(res.stdout)
        self.assertEqual(parsed["verdict"], "REVIEW_BLOCKED")
        self.assertIn("review missing required final Verdict line", parsed["blockers"])

    def test_async_review_blocked_sends_blockers_back_to_executor_for_repair(self):
        spec = importlib.util.spec_from_file_location("cfc_module_async_blocked", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        module.cmd_init(argparse.Namespace(root=str(root)))
        module.cmd_start(argparse.Namespace(root=str(root), title="Async blocked", allow=["src/app.py"], forbid=None, verify=[], tmux_target="exec:0.0", allow_dirty=False, replace=False))
        run_data, rd = module.active_run(root)
        run_data.setdefault("runner", {})["target"] = "exec:0.0"
        run_data["runner"]["reviewer_target"] = "review:0.0"
        run_data["loop"] = {"max_iterations": 3}
        run_data["awaiting"] = {"phase": "reviewer", "iteration": 1, "target": "review:0.0", "since": module.now_iso()}
        module.write_json(rd / "RUN.json", run_data)
        review_text = "Verdict: REVIEW_BLOCKED\n\n## BLOCKERS\n- fix coupon total mismatch\n"
        sends = []
        ns = argparse.Namespace(root=str(root), tmux_target=None, lines=100, wait_verdict=False, no_wait_verdict=False, poll_seconds=0.01, timeout_seconds=0, iteration=None)
        with mock.patch.object(module, "wait_for_tmux_verdict", return_value=review_text), mock.patch.object(module, "tmux_send", side_effect=lambda target, prompt: sends.append((target, prompt))):
            module.cmd_capture(ns)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "exec:0.0")
        self.assertIn("fix coupon total mismatch", sends[0][1])
        self.assertTrue((rd / "REPAIR_PROMPT.iteration-1.md").exists())
        run_after = json.loads((rd / "RUN.json").read_text())
        self.assertEqual(run_after["awaiting"]["phase"], "executor")
        self.assertEqual(run_after["awaiting"]["iteration"], 2)

    def test_async_executor_capture_runs_check_and_sends_review(self):
        spec = importlib.util.spec_from_file_location("cfc_module_async_executor", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("could not load cfc.py module spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        td, root = self.make_repo()
        self.addCleanup(td.cleanup)
        module.cmd_init(argparse.Namespace(root=str(root)))
        module.cmd_start(argparse.Namespace(root=str(root), title="Async repair", allow=["src/app.py"], forbid=None, verify=[], tmux_target="exec:0.0", allow_dirty=False, replace=False))
        run_data, rd = module.active_run(root)
        run_data.setdefault("runner", {})["target"] = "exec:0.0"
        run_data["runner"]["reviewer_target"] = "review:0.0"
        run_data["awaiting"] = {"phase": "executor", "iteration": 2, "target": "exec:0.0", "since": module.now_iso()}
        module.write_json(rd / "RUN.json", run_data)
        sends = []
        ns = argparse.Namespace(root=str(root), tmux_target=None, lines=100, wait_verdict=False, no_wait_verdict=False, poll_seconds=0.01, timeout_seconds=0, iteration=None)
        with mock.patch.object(module, "tmux_capture", return_value=subprocess.CompletedProcess(["tmux"], 0, "executor done\n", "")), mock.patch.object(module, "tmux_send", side_effect=lambda target, prompt: sends.append((target, prompt))):
            module.cmd_capture(ns)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "review:0.0")
        self.assertIn("CfC Independent Review Prompt", sends[0][1])
        self.assertTrue((rd / "CHECK.md").exists())
        self.assertTrue((rd / "REVIEW_PROMPT.iteration-2.md").exists())
        run_after = json.loads((rd / "RUN.json").read_text())
        self.assertEqual(run_after["awaiting"]["phase"], "reviewer")
        self.assertEqual(run_after["awaiting"]["iteration"], 2)


if __name__ == "__main__":
    unittest.main()
