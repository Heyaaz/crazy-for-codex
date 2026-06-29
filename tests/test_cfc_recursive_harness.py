import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        res = run(["done", "--root", str(root)])
        self.assertEqual(res.returncode, 0, res.stderr)

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
        res = run(["classify-review", "--root", str(root), "--review-file", str(review)])
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = json.loads(res.stdout)
        self.assertEqual(parsed["verdict"], "REVIEW_BLOCKED")
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

    def test_no_args_opens_chat_mode(self):
        res = subprocess.run([sys.executable, str(SCRIPT)], input="/exit\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CfC chat mode", res.stdout)

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


if __name__ == "__main__":
    unittest.main()
