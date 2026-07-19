#!/usr/bin/env python3
"""kraken.py note: a free-form worker comment lands with the attribution
disclaimer as its first line and NO hidden marker, and it touches neither the
label nor the claim ref — the task stays exactly where it was. The leading
disclaimer is what makes requeue-check read it as a worker comment, so a note
posted on a held task never bounces it back into the queue."""
import os
import unittest

from harness import KrakenConformanceTest


class NoteTests(KrakenConformanceTest):
    def test_note_posts_disclaimer_no_marker_no_state_change(self):
        self.mk_issue(7, "in-flight task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(7, "w1")

        n = os.path.join(self.state, "assumptions.md")
        self._write(n, "Assuming the API is cursor-paginated.\n\n- verified in code\n")

        r = self.kraken("note", "OWNER/tasks", 7, "w1", n)
        self.assertEqual(r.rc, 0, "note exit")
        self.assertEqual(r.out, "note: posted issue=7 worker=w1", "machine line")

        # Disclaimer heads the comment; the prose is present; no marker at all.
        self.assert_disclaimer(7, "w1")
        self.assertIn("Assuming the API is cursor-paginated.",
                      self.last_comment(7), "note prose missing")
        self.assertEqual(self.marker_count(7), 0, "a free-form note must carry no marker")

        # Pure comment: label and claim ref are untouched.
        self.assertTrue(self.has_label(7, "in-progress"), "note changed the label")
        self.assertTrue(self.claim_ref_exists(7), "note touched the claim ref")

        # requeue-check must treat this worker note as a no-op, never an
        # operator reply — the disclaimer's first-line contract is why.
        r = self.kraken(
            "requeue-check", "OWNER/tasks", 7,
            env={"COMMENT_BODY": self.last_comment(7), "COMMENT_AUTHOR_TYPE": "User"},
        )
        self.assertEqual(r.rc, 0, "requeue-check exit on a worker note")
        self.assertIn("worker comment", r.out, "note was not recognized as a worker comment")

    def test_note_bad_invocation(self):
        self.mk_issue(8, "task", "kraken-task", "project:app", "in-progress")

        r = self.kraken("note", "OWNER/tasks", 8, "w1", "/nonexistent/note.md")
        self.assertEqual(r.rc, 2, "missing body file exit")

        empty = os.path.join(self.state, "empty.md")
        self._write(empty, "\n\n")
        r = self.kraken("note", "OWNER/tasks", 8, "w1", empty)
        self.assertEqual(r.rc, 2, "empty body exit")
        self.assertEqual(self.comment_count(8), 0, "a rejected note must post nothing")


if __name__ == "__main__":
    unittest.main()
