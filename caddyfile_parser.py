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
    ensure_global_servers_options(content, idle_timeout, keepalive_interval)
    harden_reverse_proxy_in_site(content, upstream, flush, ka_idle, ka_interval)
"""

import re
import subprocess
from pathlib import Path

from fs_utils import atomic_write


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

    if not atomic_write(caddyfile_path, content):
        return {"status": "error",
                "message": f"Failed to write the updated Caddyfile to {caddyfile_path}. Check permissions and server logs."}

    return {"status": "success", "message": "Caddyfile updated for JSON logging."}


# ---------------------------------------------------------------------------
# Hardened servers options (global block) & reverse_proxy transport hardening
# ---------------------------------------------------------------------------

def _depth_before(body, pos):
    """Brace depth immediately *before* offset *pos* within *body*, ignoring
    braces nested inside quoted strings and comments."""
    depth = 0
    i = 0
    end = min(pos, len(body))
    in_string = False
    in_comment = False
    while i < end:
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
        elif char == '}':
            depth -= 1
        i += 1
    return depth


def _find_top_level_block(body, name):
    """Find a block directive ``name [args] { ... }`` at brace depth 0 of
    *body* (e.g. ``servers { ... }`` or ``transport http { ... }``).

    Returns ``(start, open_brace, close_brace)`` offsets into *body*, or None.
    Directives with the same name nested in sub-blocks are ignored.
    """
    pattern = re.compile(r'^[ \t]*' + re.escape(name)
                         + r'\b[^{\n#]*\{', re.MULTILINE)
    for match in pattern.finditer(body):
        brace = match.end() - 1
        close = find_matching_brace(body, brace)
        if close == -1:
            continue
        if _depth_before(body, match.start()) == 0:
            return (match.start(), brace, close)
    return None


def _get_depth0_directive_value(body, name):
    """Return the argument of the first single-line directive ``name <value>``
    found at brace depth 0 of *body*, or None when absent."""
    pattern = re.compile(r'^[ \t]*' + re.escape(name) + r'[ \t]+([^\s#]+)', re.MULTILINE)
    for match in pattern.finditer(body):
        if _depth_before(body, match.start()) == 0:
            return match.group(1)
    return None


def _indent_of(body, fallback='\t'):
    """Indentation unit used inside *body* (first indented line), or
    *fallback* when the body has no indented line yet."""
    match = re.search(r'(?:\A|\r?\n)([ \t]+)\S', body)
    return match.group(1) if match else fallback


def _line_start_before(text, offset):
    """Start offset of the line containing *offset* (typically a closing
    brace): new lines can be inserted exactly there."""
    nl = text.rfind('\n', 0, offset)
    return nl + 1 if nl != -1 else 0


def _child_indent(indent):
    """Indentation for a directive one level below a line indented with
    *indent*: one more tab, or four more spaces in space-indented files."""
    return indent + ('\t' if '\t' in indent else '    ')


def _apply_insertions(text, edits):
    """Apply ``(offset, line_text)`` insertions to *text*. Insertions sharing
    the same offset are concatenated in list order and applied as one block;
    offsets are processed from the highest down so they stay valid."""
    merged = {}
    for pos, line in edits:
        merged[pos] = merged.get(pos, '') + line
    for pos in sorted(merged, reverse=True):
        text = text[:pos] + merged[pos] + text[pos:]
    return text


def ensure_global_servers_options(content, idle_timeout='10m', keepalive_interval='30s'):
    """Ensure the global options block carries hardened server timeouts::

        {
            servers {
                timeouts {
                    idle <idle_timeout>
                }
                keepalive_interval <keepalive_interval>
            }
        }

    Pure, idempotent string transformation (the Caddyfile on disk is NOT
    touched; callers persist the result via fs_utils.atomic_write).
    Existing options are never overwritten:
      - identical values -> no-op;
      - different values -> left untouched and flagged as a conflict.

    Returns:
        (new_content, status) with status one of:
            'created'   - no global block existed; one was prepended.
            'updated'   - missing options were merged into the global block.
            'unchanged' - options already present with exactly these values.
            'conflict'  - an option exists with a different value; untouched.
            'error'     - malformed Caddyfile (unmatched brace); content intact.
    """
    # Locate the global options block: after leading whitespace/comments the
    # very first significant character must be '{' (same rule as
    # configure_caddyfile_logging / add_log_to_site_blocks).
    global_open = None
    i, n = 0, len(content)
    while i < n:
        ch = content[i]
        if ch in (' ', '\t', '\n', '\r'):
            i += 1
            continue
        if ch == '#':
            while i < n and content[i] != '\n':
                i += 1
            continue
        if ch == '{':
            global_open = i
        break

    if global_open is None:
        block = (
            "{\n"
            "\tservers {\n"
            "\t\ttimeouts {\n"
            f"\t\t\tidle {idle_timeout}\n"
            "\t\t}\n"
            f"\t\tkeepalive_interval {keepalive_interval}\n"
            "\t}\n"
            "}\n\n"
        )
        return block + content, "created"

    global_close = find_matching_brace(content, global_open)
    if global_close == -1:
        return content, "error"

    inner = content[global_open + 1:global_close]

    servers = _find_top_level_block(inner, 'servers')
    if servers is None:
        indent = _indent_of(inner)
        close = indent + "}"
        servers_text = (
            indent + "servers {\n"
            + indent + "\ttimeouts {\n"
            + indent + "\t\tidle " + idle_timeout + "\n"
            + indent + "\t}\n"
            + indent + "\tkeepalive_interval " + keepalive_interval + "\n"
            + close
            + ("\n" if inner.strip() else "")
        )
        new_inner = inner.rstrip() + ("\n" + servers_text if inner.strip() else servers_text)
        new_content = content[:global_open + 1] + new_inner + content[global_close:]
        return new_content, "updated"

    s_start, s_brace, s_close = servers
    s_indent = _indent_of(inner[s_start:s_close], fallback='\t')
    s_body = inner[s_brace + 1:s_close]
    body_indent = _indent_of(s_body, fallback=_child_indent(s_indent))

    timeouts = _find_top_level_block(s_body, 'timeouts')
    current_idle = None
    if timeouts is not None:
        t_start, t_brace, t_close = timeouts
        t_body = s_body[t_brace + 1:t_close]
        current_idle = _get_depth0_directive_value(t_body, 'idle')
    current_ka = _get_depth0_directive_value(s_body, 'keepalive_interval')

    if ((current_idle is not None and current_idle != idle_timeout)
            or (current_ka is not None and current_ka != keepalive_interval)):
        return content, "conflict"

    need_idle = current_idle is None
    need_ka = current_ka is None
    if not need_idle and not need_ka:
        return content, "unchanged"

    # Collect insertions as (offset_in_s_body, text); they are applied from
    # the highest offset down so earlier offsets stay valid. Inserting at the
    # start of the line holding the servers closing brace appends cleanly
    # before that brace.
    edits = []
    close_line_pos = _line_start_before(s_body, len(s_body))
    if need_idle:
        if timeouts is not None:
            t_start, t_brace, t_close = timeouts
            t_body = s_body[t_brace + 1:t_close]
            t_indent = _indent_of(t_body, fallback=_child_indent(body_indent))
            pos = _line_start_before(t_body, len(t_body)) + t_brace + 1
            edits.append((pos, f"{t_indent}idle {idle_timeout}\n"))
        else:
            edits.append((close_line_pos, (
                f"{body_indent}timeouts {{\n"
                f"{_child_indent(body_indent)}idle {idle_timeout}\n"
                f"{body_indent}}}\n"
            )))
    if need_ka:
        edits.append((close_line_pos,
                      f"{body_indent}keepalive_interval {keepalive_interval}\n"))

    s_body = _apply_insertions(s_body, edits)
    new_inner = inner[:s_brace + 1] + s_body + inner[s_close:]
    new_content = content[:global_open + 1] + new_inner + content[global_close:]
    return new_content, "updated"


_REVERSE_PROXY_RE = re.compile(r'^([ \t]*)reverse_proxy[ \t]+([^\s{}#]+)([^\n]*)$',
                                re.MULTILINE)


def harden_reverse_proxy_in_site(content, upstream, flush=True,
                                 ka_idle="5m", ka_interval="30s"):
    """Harden every ``reverse_proxy <upstream>`` directive found in *content*
    by injecting::

        flush_interval -1
        transport http {
            keepalive_idle <ka_idle>
            keepalive_interval <ka_interval>
        }

    Both the single-line form (``reverse_proxy host:port``, rewritten as a
    block) and the multi-line block form (``reverse_proxy host:port { ... }``)
    are handled; extra arguments following <upstream> are preserved.
    Already-hardened directives are left untouched (no-op) and existing values
    are never overwritten:
      - identical values -> skipped;
      - different values -> conflict, NOTHING is modified.

    Args:
        content: full Caddyfile text.
        upstream: upstream address to match (first argument of the directive).
        flush: inject ``flush_interval -1`` when True.
        ka_idle: keepalive_idle value for the http transport.
        ka_interval: keepalive_interval value for the http transport.

    Returns:
        (new_content, status) with status one of:
            'updated'   - at least one reverse_proxy was hardened.
            'unchanged' - matching directive(s) already fully hardened.
            'not_found' - no ``reverse_proxy <upstream>`` directive matched.
            'conflict'  - an existing option had a different value; untouched.
            'error'     - invalid upstream argument; content intact.
    """
    if not isinstance(upstream, str) or not upstream or re.search(r'[\s{}#]', upstream):
        return content, "error"

    plans = []          # per-match action, applied later in reverse order
    changed_possible = False
    for m in _REVERSE_PROXY_RE.finditer(content):
        if m.group(2) != upstream:
            continue
        base_indent = m.group(1)
        rest = m.group(3)

        # Detect the multi-line block form: an unquoted '{' within the
        # directive line opens the reverse_proxy sub-block.
        brace_off = None
        j, in_str = 0, False
        while j < len(rest):
            c = rest[j]
            if in_str:
                if c == '\\':
                    j += 2
                    continue
                if c == '"':
                    in_str = False
                j += 1
                continue
            if c == '#':
                break  # comment until end of line
            if c == '"':
                in_str = True
                j += 1
                continue
            if c == '{':
                brace_off = j
                break
            j += 1

        if brace_off is None:
            # Single-line form: always convertible, no existing sub-options.
            line_end = content.find('\n', m.start())
            if line_end == -1:
                line_end = len(content)
            else:
                line_end += 1
            args = rest.rstrip()
            if '#' in args:
                args = args.split('#', 1)[0].rstrip()
            inner_indent = _child_indent(base_indent)
            head = f"{base_indent}reverse_proxy {upstream}"
            if args:
                head += f" {args}"
            lines = [head + " {"]
            if flush:
                lines.append(f"{inner_indent}flush_interval -1")
            lines.append(f"{inner_indent}transport http {{")
            lines.append(f"{_child_indent(inner_indent)}keepalive_idle {ka_idle}")
            lines.append(f"{_child_indent(inner_indent)}keepalive_interval {ka_interval}")
            lines.append(inner_indent + "}")
            lines.append(base_indent + "}")
            plans.append(('line', m.start(), line_end, "\n".join(lines) + "\n"))
            changed_possible = True
            continue

        brace_abs = m.start(3) + brace_off
        close_abs = find_matching_brace(content, brace_abs)
        if close_abs == -1:
            continue  # malformed occurrence: skip it
        body_start = brace_abs + 1
        body = content[body_start:close_abs]
        indent = _indent_of(body, fallback=_child_indent(base_indent))

        conflicts = False
        cur_flush = _get_depth0_directive_value(body, 'flush_interval') if flush else '-1'
        if flush and cur_flush is not None and cur_flush != '-1':
            conflicts = True

        transport = _find_top_level_block(body, 'transport')
        t_info = None
        if transport is not None:
            t_start, t_brace, t_close = transport
            header = body[t_start:t_brace].split()
            if len(header) >= 2 and header[1] == 'http':
                t_body = body[t_brace + 1:t_close]
                cur_idle = _get_depth0_directive_value(t_body, 'keepalive_idle')
                cur_int = _get_depth0_directive_value(t_body, 'keepalive_interval')
                if ((cur_idle is not None and cur_idle != ka_idle)
                        or (cur_int is not None and cur_int != ka_interval)):
                    conflicts = True
                t_info = (t_brace, t_close, cur_idle, cur_int)
            else:
                conflicts = True  # transport with another protocol: leave it alone
        if conflicts:
            return content, "conflict"

        need_flush = flush and cur_flush is None
        if transport is None:
            need_transport_block = True
            need_t_keys = False
        else:
            need_transport_block = False
            _, _, cur_idle, cur_int = t_info
            need_t_keys = cur_idle is None or cur_int is None

        if not need_flush and not need_transport_block and not need_t_keys:
            plans.append(('unchanged',))
            continue

        changed_possible = True
        edits = []
        close_line_pos = _line_start_before(body, len(body))
        if need_flush:
            edits.append((close_line_pos, f"{indent}flush_interval -1\n"))
        if need_transport_block:
            edits.append((close_line_pos, (
                f"{indent}transport http {{\n"
                f"{_child_indent(indent)}keepalive_idle {ka_idle}\n"
                f"{_child_indent(indent)}keepalive_interval {ka_interval}\n"
                f"{indent}}}\n"
            )))
        elif need_t_keys:
            t_brace, t_close, cur_idle, cur_int = t_info
            t_body = body[t_brace + 1:t_close]
            t_indent = _indent_of(t_body, fallback=_child_indent(indent))
            t_pos = _line_start_before(t_body, len(t_body)) + t_brace + 1
            if cur_idle is None:
                edits.append((t_pos, f"{t_indent}keepalive_idle {ka_idle}\n"))
            if cur_int is None:
                edits.append((t_pos, f"{t_indent}keepalive_interval {ka_interval}\n"))
        plans.append(('block', body_start, close_abs, edits))

    actionable = [p for p in plans if p[0] != 'unchanged']
    if not actionable:
        if not plans:
            return content, "not_found"
        return content, "unchanged"
    if not changed_possible:  # defensive: should not happen
        return content, "unchanged"

    # Apply from the last match backwards so earlier offsets stay valid.
    for plan in sorted(actionable, key=lambda p: p[1], reverse=True):
        if plan[0] == 'line':
            _, start, end, replacement = plan
            content = content[:start] + replacement + content[end:]
        else:
            _, body_start, body_end, edits = plan
            body = _apply_insertions(content[body_start:body_end], edits)
            content = content[:body_start] + body + content[body_end:]
    return content, "updated"