"""
Caddyfile Parser — shared parsing and logging-configuration utilities.

This module centralises the Caddyfile brace-matching and directive-block
manipulation logic that was previously duplicated across app.py and
docker/entrypoint.sh (heredoc Python).  The JavaScript equivalent lives
separately in static/script.js (different language).

Functions provided:
    find_matching_brace(content, start)
    remove_directive_block(content, directive_name)
    add_log_to_site_blocks(content)
    configure_caddyfile_logging(caddyfile_path_str)
"""

import re
import subprocess
from pathlib import Path


def find_matching_brace(content, start):
    """Find the position of the closing brace matching the opening brace at
    *start*.  Handles nested braces, quoted strings, and comments.

    Returns the index of the matching ``'}'`` or -1 if not found.
    """
    if start >= len(content) or content[start] != '{':
        return -1
    depth = 0
    i = start
    in_string = False
    in_comment = False
    while i < len(content):
        char = content[i]
        if in_comment:
            if char == '\n':
                in_comment = False
            i += 1
            continue
        if in_string:
            if char == '\\':
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '#':
            in_comment = True
            i += 1
            continue
        if char == '"':
            in_string = True
            i += 1
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def remove_directive_block(content, directive_name):
    """Remove a directive from *content*: the block form ``log { ... }``
    (handling nested braces) or, when no block form is present, the
    single-line non-block form ``log <args>`` (e.g. ``log /var/log/x``).

    Returns ``(new_content, found)``.
    """
    pattern = re.compile(r'^(\s*)' + re.escape(directive_name) + r'\s*\{', re.MULTILINE)
    match = pattern.search(content)
    if match:
        brace_start = match.end() - 1
        brace_end = find_matching_brace(content, brace_start)
        if brace_end == -1:
            return content, False
        line_start = content.rfind('\n', 0, match.start()) + 1 if match.start() > 0 else 0
        line_end = content.find('\n', brace_end)
        if line_end == -1:
            line_end = len(content)
        else:
            line_end += 1
        return content[:line_start] + content[line_end:], True

    # No block form: remove the single-line (non-block) directive instead.
    # `[ \t]*` (not `\s*`) keeps the match anchored at the actual line start
    # so a preceding newline is not consumed by the leading whitespace.
    line_pattern = re.compile(r'^[ \t]*' + re.escape(directive_name) + r'(?:\s+.*)?$', re.MULTILINE)
    match = line_pattern.search(content)
    if match:
        line_start = match.start()
        line_end = content.find('\n', line_start)
        if line_end == -1:
            line_end = len(content)
        else:
            line_end += 1
        return content[:line_start] + content[line_end:], True

    return content, False


def _body_has_top_level_directive(body, directive_name):
    """Return True if *body* (the inner content of a block) contains
    *directive_name* at brace depth 0, ignoring directives nested inside
    sub-blocks (e.g. ``handle { log { ... } }``).  Braces inside quoted
    strings and comments are ignored.
    """
    depth = 0
    in_string = False
    in_comment = False
    i = 0
    n = len(body)
    while i < n:
        char = body[i]
        if in_comment:
            if char == '\n':
                in_comment = False
            i += 1
            continue
        if in_string:
            if char == '\\':
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '#':
            in_comment = True
            i += 1
            continue
        if char == '"':
            in_string = True
            i += 1
            continue
        if char == '{':
            depth += 1
            i += 1
            continue
        if char == '}':
            depth -= 1
            i += 1
            continue
        # At depth 0 and at the start of a line, check for the directive.
        if depth == 0 and (i == 0 or body[i - 1] == '\n'):
            j = i
            while j < n and body[j] in ' \t':
                j += 1
            if body.startswith(directive_name, j):
                after = j + len(directive_name)
                if after >= n or (not body[after].isalnum() and body[after] != '_'):
                    return True
        i += 1
    return False


def add_log_to_site_blocks(content):
    """Add a ``log`` directive to every site block that doesn't already have
    one.  A site block is a top-level block that is NOT the global block (the
    one starting at the very beginning of the file after optional whitespace).
    """
    if not content.strip():
        return content

    # Find global block boundaries to skip it (ignoring leading whitespace
    # and comments: a comment above the global block must not hide it).
    global_start = None
    global_end = None
    i = 0
    while i < len(content):
        ch = content[i]
        if ch in (' ', '\t', '\n', '\r'):
            i += 1
            continue
        if ch == '#':
            while i < len(content) and content[i] != '\n':
                i += 1
            continue
        if ch == '{':
            global_start = i
            global_end = find_matching_brace(content, i)
        break

    # Find all top-level blocks (opening braces NOT inside the global block)
    blocks = []
    pos = 0
    while pos < len(content):
        idx = content.find('{', pos)
        if idx == -1:
            break
        # Skip if inside global block
        if global_start is not None and global_end is not None:
            if global_start <= idx <= global_end:
                pos = idx + 1
                continue
        # Verify this is a site block opener (has an address before the {).
        # Snippets `(name) { ... }` are reusable templates, not site blocks,
        # so they are skipped as well (their opening line starts with '(').
        line_start = content.rfind('\n', 0, idx) + 1
        line_before = content[line_start:idx].strip()
        if not line_before or line_before.startswith('#') or line_before.startswith('('):
            pos = idx + 1
            continue
        close = find_matching_brace(content, idx)
        if close == -1:
            pos = idx + 1
            continue
        blocks.append((idx, close))
        pos = close + 1

    # Process blocks in reverse order so offsets stay valid
    for block_open, block_close in reversed(blocks):
        block_body = content[block_open + 1:block_close]
        # Only a top-level (site-level) `log` directive counts; a `log` nested
        # inside a sub-block (e.g. `handle { ... }`) does not suppress the
        # injection of the site-level access-log directive.
        if _body_has_top_level_directive(block_body, 'log'):
            continue  # already has log
        # Determine indentation from existing directives
        indent_match = re.search(r'\n(\s+)\S', block_body)
        indent = indent_match.group(1) if indent_match else '\t'
        # Insert 'log' right after the opening brace
        insert = '\n' + indent + 'log'
        content = content[:block_open + 1] + insert + content[block_open + 1:]

    return content


def configure_caddyfile_logging(caddyfile_path_str):
    """Add or modify the global log configuration in the Caddyfile for JSON
    stdout logging, and ensure every site block has a ``log`` directive so
    access logs are emitted.

    This function only modifies the file on disk — it does **not** reload
    Caddy.  Callers that need a live reload (e.g. app.py) should do so
    separately after a successful return.

    Args:
        caddyfile_path_str: Path to the Caddyfile (str or Path).

    Returns:
        dict with ``status`` and ``message`` keys:
            - ``{"status": "success", ...}`` — file updated.
            - ``{"status": "error", ...}``    — file not found or malformed.
    """
    caddyfile_path = Path(caddyfile_path_str)
    desired_log_config = "\tlog {\n\t\toutput stdout\n\t\tformat json\n\t\tlevel INFO\n\t}"

    if not caddyfile_path.exists():
        return {"status": "error", "message": f"Caddyfile not found at {caddyfile_path}."}

    content = caddyfile_path.read_text(encoding='utf-8')

    # --- Step 1: Ensure global block has proper log config ---
    # Skip leading whitespace and comments; only a `{` starting the first
    # directive (i.e. the global options block) is treated as the global block.
    global_open = None
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if ch in (' ', '\t', '\n', '\r'):
            i += 1
            continue
        if ch == '#':
            # Skip the comment up to (and including) its newline.
            while i < n and content[i] != '\n':
                i += 1
            continue
        if ch == '{':
            global_open = i
        break

    if global_open is not None:
        global_close = find_matching_brace(content, global_open)
        if global_close == -1:
            return {"status": "error", "message": "Malformed Caddyfile: unmatched opening brace in global block."}

        inner_content = content[global_open + 1:global_close]
        inner_content, _ = remove_directive_block(inner_content, 'log')
        new_inner = inner_content.rstrip() + '\n' + desired_log_config + '\n'
        content = content[:global_open + 1] + new_inner + content[global_close:]
    else:
        content = "{\n" + desired_log_config + "\n}\n\n" + content

    # --- Step 2: Add 'log' directive to every site block that lacks one ---
    content = add_log_to_site_blocks(content)

    caddyfile_path.write_text(content, encoding='utf-8')

    return {"status": "success", "message": "Caddyfile updated for JSON logging."}