import unittest

from broker.terminal_output import render_output


class TerminalOutputTests(unittest.TestCase):
    def test_single_progress_bar_updates_in_place(self):
        text = "Starting simulation\n\rSimulating: 35%|███▌| 341/968"
        self.assertEqual(render_output(text), "Starting simulation\nSimulating: 35%|███▌| 341/968")
        text += "\rSimulating: 38%|███▊| 367/968\x1b[K"
        self.assertEqual(render_output(text), "Starting simulation\nSimulating: 38%|███▊| 367/968")
        self.assertEqual(render_output(text + "\rDone\x1b[K\n"), "Starting simulation\nDone\n")

    def test_nested_tqdm_cursor_up_keeps_independent_rows(self):
        text = "\rOuter: 10%\n\rInner: 20%\x1b[A"
        text += "\rOuter: 30%\n\rInner: 40%\x1b[A"
        self.assertEqual(render_output(text), "Outer: 30%\nInner: 40%")
        self.assertEqual(render_output(text + "\rOuter: 100%\n\rInner: 100%\n"), "Outer: 100%\nInner: 100%\n")

    def test_plain_logs_crlf_styles_and_incomplete_controls(self):
        self.assertEqual(render_output("normal\r\n日志\n"), "normal\n日志\n")
        self.assertEqual(render_output("\x1b[31mred\x1b[0m\n"), "red\n")
        self.assertEqual(render_output("old\rnew\x1b["), "new")
        self.assertEqual(render_output("old text\rnew\x1b[K"), "new")
        self.assertEqual(render_output("ab\bc"), "ac")
        self.assertEqual(render_output("hello\x1b]52;c;secret\x07world"), "helloworld")

    def test_wide_and_combining_characters_use_terminal_columns(self):
        self.assertEqual(render_output("进度 10%\r进度 20%"), "进度 20%")
        self.assertEqual(render_output("中\rx"), "x")
        self.assertEqual(render_output("e\u0301\rX"), "X")

    def test_carriage_return_does_not_erase_without_explicit_clear(self):
        self.assertEqual(render_output("long line\rshort"), "shortline")
        self.assertEqual(render_output("long line\rshort\x1b[K"), "short")

    def test_incremental_split_controls_and_crlf(self):
        state = {}
        self.assertEqual(render_output("outer\ninner\x1b[", state), "outer\ninner")
        self.assertEqual(render_output("A\rOUTER\r", state), "OUTER\ninner")
        self.assertEqual(render_output("\nINNER\x1b[K", state), "OUTER\nINNER")
        self.assertEqual(render_output("\n", state), "OUTER\nINNER\n")


if __name__ == "__main__":
    unittest.main()
