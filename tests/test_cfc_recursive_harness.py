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


if __name__ == "__main__":
    unittest.main()
