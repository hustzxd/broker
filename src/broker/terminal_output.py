"""Render common progress-bar cursor controls as selectable terminal text."""

import re

from rich.cells import get_character_cell_size


CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")
MAX_COLUMNS = 4096
MAX_ROWS = 10000


def render_output(text, state=None):
    """Replay retained output, interpreting CR and common ANSI cursor/erase codes.

    This is a log view, not a full terminal emulator. Styling and unsupported
    escape sequences are ignored and never forwarded to the user's terminal.
    Cursor positions are bounded so remote output can't allocate arbitrary space.
    """
    if state is None:
        state = {}
    text = state.get("pending", "") + text
    rows = state.get("rows", [[]])
    row = state.get("row", 0)
    column = state.get("column", 0)
    index = 0

    def move_to(target):
        nonlocal row
        row = max(0, min(MAX_ROWS - 1, target))
        while len(rows) <= row:
            rows.append([])

    def erase_cell(line, position):
        if position >= len(line):
            return
        # Wide glyphs occupy a leading character and an empty continuation cell.
        if line[position] == "" and position:
            line[position - 1] = " "
        elif position + 1 < len(line) and line[position + 1] == "":
            line[position + 1] = " "
        line[position] = " "

    while index < len(text):
        char = text[index]
        if char == "\x1b":
            if text.startswith("\x1b[", index):
                match = CSI.match(text, index)
                if match is None:
                    # A partially received escape must wait for the next refresh.
                    if all(c in "0123456789;? " for c in text[index + 2:]):
                        break
                    index += 2
                    continue
                params, _, code = match.groups()
                index = match.end()
                if params.startswith("?"):
                    continue
                # Avoid unbounded integer parsing in malformed remote controls.
                values = [min(int(value), MAX_ROWS) if value and len(value) < 6 else 0
                          for value in params.split(";")]
                amount = values[0] or 1
                if code == "A":
                    move_to(row - amount)
                elif code == "B":
                    move_to(row + amount)
                elif code == "C":
                    column = min(MAX_COLUMNS - 1, column + amount)
                elif code == "D":
                    column = max(0, column - amount)
                elif code == "G":
                    column = min(MAX_COLUMNS - 1, amount - 1)
                elif code in {"H", "f"}:
                    move_to(amount - 1)
                    column = min(MAX_COLUMNS - 1, (values[1] or 1) - 1) if len(values) > 1 else 0
                elif code == "K":
                    mode = values[0]
                    if mode == 0:
                        if column < len(rows[row]) and rows[row][column] == "" and column:
                            rows[row][column - 1] = " "
                        del rows[row][column:]
                    elif mode == 1:
                        for position in range(min(column + 1, len(rows[row]))):
                            erase_cell(rows[row], position)
                    elif mode == 2:
                        rows[row] = []
                elif code == "J" and values[0] in {0, 2}:
                    if values[0] == 2:
                        rows = [[] for _ in range(row + 1)]
                    else:
                        del rows[row][column:]
                        del rows[row + 1:]
                continue
            if text.startswith("\x1b]", index):
                end = re.search(r"\x07|\x1b\\", text[index + 2:])
                if end is None:
                    break
                index += 2 + end.end()
                continue
            if index + 1 == len(text):
                break
            # Other two-character terminal commands are not rendered.
            index += 2
            continue
        index += 1
        if char == "\r":
            column = 0
        elif char == "\n":
            if row == MAX_ROWS - 1:
                rows.pop(0)
                rows.append([])
            else:
                move_to(row + 1)
            column = 0
        elif char == "\b":
            column = max(0, column - 1)
        elif char == "\t":
            column = min(MAX_COLUMNS - 1, (column // 8 + 1) * 8)
        elif char.isprintable():
            width = get_character_cell_size(char)
            if width == 0:
                if column and column <= len(rows[row]):
                    previous = column - 1
                    if rows[row][previous] == "" and previous:
                        previous -= 1
                    rows[row][previous] += char
                continue
            if column + width > MAX_COLUMNS:
                continue
            line = rows[row]
            while len(line) < column + width:
                line.append(" ")
            for position in range(column, column + width):
                erase_cell(line, position)
            line[column] = char
            if width == 2:
                line[column + 1] = ""
            column += width

    # Bound retained rendered history as well as incoming raw output.
    size = sum(len("".join(line).encode("utf-8")) for line in rows)
    removed = 0
    while size > 1024 * 1024 and len(rows) - removed > 1:
        size -= len("".join(rows[removed]).encode("utf-8"))
        removed += 1
    if removed:
        del rows[:removed]
        row = max(0, row - removed)
    pending = text[index:]
    state.update(rows=rows, row=row, column=column, pending=pending if len(pending) <= 4096 else "")
    # Keep trailing newlines for ordinary logs, but omit rows cleared by cursors.
    last = max(row, max((n for n, line in enumerate(rows) if line), default=0))
    return "\n".join("".join(line).rstrip() for line in rows[:last + 1])
