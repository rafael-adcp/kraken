#!/usr/bin/env python3
"""Queue-entry quality gate conformance. Drives kraken.py validate against the
gh-stub: a new kraken-task missing its project:<name> label, or an empty/absent
Goal or Acceptance section, gets ONE actionable comment; a compliant task gets
none; an edit-after-fix neither re-flags nor piles up duplicates."""
import unittest

from harness import KrakenConformanceTest

GOOD_BODY = ("### Goal\n\nEndpoint /v2/things returns cursor-paginated results.\n\n"
             "### Acceptance\n\n`npm test -- things.spec` passes.\n\n"
             "### Notes\n\n_No response_")


class ValidateTaskTests(KrakenConformanceTest):
    def run_case(self, issue):
        return self.kraken("validate", "OWNER/tasks", issue, env={"REPO": "OWNER/tasks"})

    def test_validate(self):
        # --- #1: missing project label -> one comment naming the label -------
        self.mk_issue(1, "compliant body but no project label", "kraken-task")
        self.mk_body(1, GOOD_BODY)
        r = self.run_case(1)
        self.assertEqual(r.rc, 0, "#1 run")
        self.assertEqual(self.comment_count(1), 1, "#1 a missing project label must post exactly one comment")
        self.assertIn("project:<name>", self.last_comment(1), "#1 comment does not name the missing project label")
        self.assertIn('<!-- kraken {"type":"validation"} -->', self.last_comment(1),
                      "#1 comment missing the validation marker")

        # --- #2: missing Acceptance section -> one comment naming Acceptance --
        self.mk_issue(2, "has project + Goal but empty Acceptance", "kraken-task", "project:app")
        self.mk_body(2, "### Goal\n\nShip the thing.\n\n### Acceptance\n\n_No response_\n\n### Notes\n\n_No response_")
        r = self.run_case(2)
        self.assertEqual(r.rc, 0, "#2 run")
        self.assertEqual(self.comment_count(2), 1, "#2 an empty Acceptance must post exactly one comment")
        self.assertIn("Acceptance", self.last_comment(2), "#2 comment does not name the missing Acceptance section")
        self.assertNotIn("project:<name>", self.last_comment(2), "#2 wrongly flagged the present project label")

        # --- #2b: hand-written issue with no headings -> Goal+Acceptance flagged
        self.mk_issue(20, "hand-written, no issue-form headings", "kraken-task", "project:app")
        self.mk_body(20, "just do the thing, you know what I mean")
        r = self.run_case(20)
        self.assertEqual(r.rc, 0, "#2b run")
        self.assertEqual(self.comment_count(20), 1, "#2b a heading-less body must post exactly one comment")
        self.assertIn("Goal", self.last_comment(20), "#2b comment does not name the missing Goal")
        self.assertIn("Acceptance", self.last_comment(20), "#2b comment does not name the missing Acceptance")

        # --- #3: compliant task -> no comment ---------------------------------
        self.mk_issue(3, "fully compliant task", "kraken-task", "project:app")
        self.mk_body(3, GOOD_BODY)
        r = self.run_case(3)
        self.assertEqual(r.rc, 0, "#3 run")
        self.assertEqual(self.comment_count(3), 0, "#3 a compliant task must get no comment")

        # --- #4: non-kraken-task issue -> no-op -------------------------------
        self.mk_issue(4, "not a kraken task", "project:app")
        self.mk_body(4, "whatever")
        r = self.run_case(4)
        self.assertEqual(r.rc, 0, "#4 run")
        self.assertEqual(self.comment_count(4), 0, "#4 a non-kraken-task issue must be a no-op")

        # --- #5: debounce -> a re-run with the same missing set adds no dup ----
        self.mk_issue(5, "missing label, edited twice", "kraken-task")
        self.mk_body(5, GOOD_BODY)
        r = self.run_case(5)
        self.assertEqual(r.rc, 0, "#5 first run")
        self.assertEqual(self.comment_count(5), 1, "#5 first run posts one comment")
        r = self.run_case(5)
        self.assertEqual(r.rc, 0, "#5 second run")
        self.assertEqual(self.comment_count(5), 1, "#5 an identical re-flag must not post a duplicate")

        # --- #6: edit-after-fix -> fixing the task stops the flag path --------
        self.set_labels(5, self.labels(5) + ["project:app"])
        r = self.run_case(5)
        self.assertEqual(r.rc, 0, "#6 run after fix")
        self.assertEqual(self.comment_count(5), 1, "#6 a fixed task must not get a new comment")


if __name__ == "__main__":
    unittest.main()
