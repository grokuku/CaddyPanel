"""
Unit tests for caddyfile_parser.py.

These tests document the OBSERVED behaviour of the parser functions, not the
ideal behaviour.  Where a discrepancy is considered a bug, it is flagged with
a ``# BUG:`` comment (do NOT fix here — another task will address them).

Run with::

    pytest tests/test_caddyfile_parser.py -v
"""

import pytest

from caddyfile_parser import (
    add_log_to_site_blocks,
    configure_caddyfile_logging,
    ensure_global_servers_options,
    find_matching_brace,
    harden_reverse_proxy_in_site,
    remove_directive_block,
)

# Exact log config block that configure_caddyfile_logging() injects.
EXPECTED_GLOBAL_LOG = (
    "\tlog {\n"
    "\t\toutput stdout\n"
    "\t\tformat json\n"
    "\t\tlevel INFO\n"
    "\t}"
)


# ---------------------------------------------------------------------------
# find_matching_brace
# ---------------------------------------------------------------------------

def test_find_matching_brace_simple():
    assert find_matching_brace("{abc}", 0) == 4


def test_find_matching_brace_adjacent_pair():
    assert find_matching_brace("{}", 0) == 1


def test_find_matching_brace_nested():
    assert find_matching_brace("{{x}}y", 0) == 4
    assert find_matching_brace("{{{{}}}}", 0) == 7


def test_find_matching_brace_close_on_own_line():
    content = "{\n    root * /var/www\n}"
    assert find_matching_brace(content, 0) == len(content) - 1


def test_find_matching_brace_start_not_at_beginning_of_content():
    assert find_matching_brace("x {y}", 2) == 4


def test_find_matching_brace_returns_minus_one_when_start_is_not_open_brace():
    assert find_matching_brace("abc", 0) == -1
    assert find_matching_brace("}", 0) == -1


def test_find_matching_brace_returns_minus_one_when_start_out_of_range():
    assert find_matching_brace("{}", 5) == -1
    assert find_matching_brace("", 0) == -1


def test_find_matching_brace_unmatched_open_brace():
    assert find_matching_brace("{abc", 0) == -1


def test_find_matching_brace_ignores_braces_inside_double_quoted_strings():
    assert find_matching_brace('{"}"}', 0) == 4
    assert find_matching_brace('{"{"}', 0) == 4


def test_find_matching_brace_ignores_hash_inside_string():
    # A '#' inside a string must not start a comment.
    assert find_matching_brace('{"#x"}', 0) == 5


def test_find_matching_brace_handles_escaped_quote_in_string():
    # '{"\\"}"}' -> {, ", \, ", }, ", }  -- the inner } is inside the string.
    assert find_matching_brace('{"\\"}"}', 0) == 6


def test_find_matching_brace_ignores_braces_inside_comments():
    assert find_matching_brace("{# }\n}", 0) == 5
    assert find_matching_brace("{#}\n}", 0) == 4


def test_find_matching_brace_comment_terminated_by_crlf():
    # The '}' inside the comment is skipped; the comment ends at '\n'
    # (which is the '\n' of the CRLF), then the real '}' matches.
    assert find_matching_brace("{#}\r\n}", 0) == 5


# ---------------------------------------------------------------------------
# remove_directive_block
# ---------------------------------------------------------------------------

def test_remove_directive_block_removes_block():
    content = "log {\n    output stdout\n}\n"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == ""


def test_remove_directive_block_multiline_directives():
    content = (
        "log {\n"
        "    output stdout\n"
        "    format json\n"
        "    level INFO\n"
        "}\n"
        "\n"
        "site {\n"
        "    root * /app\n"
        "}\n"
    )
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    # Observed: the blank line separator after the block is kept (leading '\n').
    assert new_content == "\nsite {\n    root * /app\n}\n"


def test_remove_directive_block_handles_nested_braces():
    content = "log {\n    handle {\n        x\n    }\n}\nrest"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == "rest"


def test_remove_directive_block_directive_not_found_is_noop():
    content = "site {\n    root * /app\n}\n"
    new_content, found = remove_directive_block(content, "log")
    assert found is False
    assert new_content is content  # returns the same object, unchanged


def test_remove_directive_block_empty_content():
    new_content, found = remove_directive_block("", "log")
    assert found is False
    assert new_content == ""


def test_remove_directive_block_unmatched_brace_is_noop():
    content = "log {"
    new_content, found = remove_directive_block(content, "log")
    assert found is False
    assert new_content == content


def test_remove_directive_block_matches_indented_blocks_too():
    # Despite the docstring saying "top-level", the regex is anchored per-line
    # so an indented (nested) block is removed as well.
    content = "site {\n    log {\n        x\n    }\n}"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == "site {\n}"


def test_remove_directive_block_no_partial_word_match():
    # 'logging' must not match a request for 'log'; only the real `log {` is removed.
    content = "logging {\n}\nlog {\n}"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == "logging {\n}\n"


def test_remove_directive_block_escapes_directive_name():
    content = "a.b {\n    x\n}\n"
    new_content, found = remove_directive_block(content, "a.b")
    assert found is True
    assert new_content == ""


def test_remove_directive_block_crlf():
    content = "log {\r\n    output stdout\r\n}\r\n"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == ""


def test_remove_directive_block_keeps_blank_line_separator():
    content = "log {\n    output stdout\n}\n\nsite {\n}"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == "\nsite {\n}"


def test_remove_directive_block_removes_non_block_directive():
    # The single-line (non-block) form `log <file>` is also removed.
    content = "log /var/log/caddy_access.log\n"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == ""


def test_remove_directive_block_removes_indented_non_block_directive():
    content = "site {\n    log /var/log/caddy_access.log\n    root * /a\n}\n"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == "site {\n    root * /a\n}\n"


def test_remove_directive_block_non_block_no_partial_word_match():
    # `log` must not remove a line whose directive merely starts with `log`.
    content = "logrotate daily\nlog /var/log/x\n"
    new_content, found = remove_directive_block(content, "log")
    assert found is True
    assert new_content == "logrotate daily\n"


# ---------------------------------------------------------------------------
# add_log_to_site_blocks
# ---------------------------------------------------------------------------

def test_add_log_to_site_blocks_injects_into_each_site_block():
    content = (
        "{\n"
        "    email admin@example.com\n"
        "}\n"
        "\n"
        "example.com {\n"
        "    root * /var/www\n"
        "}\n"
        "\n"
        "api.example.com {\n"
        "    reverse_proxy localhost:8000\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    assert result.count("\n    log\n") == 2
    assert "example.com {\n    log\n    root * /var/www" in result
    assert "api.example.com {\n    log\n    reverse_proxy localhost:8000" in result
    # The global block must remain untouched.
    assert "{\n    email admin@example.com\n}" in result


def test_add_log_to_site_blocks_skips_blocks_that_already_have_log():
    content = (
        "example.com {\n"
        "    log {\n"
        "        output file /var/log/access.log\n"
        "    }\n"
        "    root * /var/www\n"
        "}\n"
    )
    assert add_log_to_site_blocks(content) == content


def test_add_log_to_site_blocks_skips_non_block_log_directive():
    # A plain `log path` directive (no braces) also counts as "has log".
    content = "example.com {\n    log /var/log/access.log\n    root * /var/www\n}\n"
    assert add_log_to_site_blocks(content) == content


def test_add_log_to_site_blocks_does_not_touch_global_block_only_file():
    content = "{\n    email a@b.com\n}\n"
    assert add_log_to_site_blocks(content) == content


def test_add_log_to_site_blocks_empty_and_whitespace_only():
    assert add_log_to_site_blocks("") == ""
    whitespace = "  \n\t\n"
    assert add_log_to_site_blocks(whitespace) == whitespace


def test_add_log_to_site_blocks_works_without_global_block():
    content = "example.com {\n    root * /var/www\n}\n"
    result = add_log_to_site_blocks(content)
    assert result == "example.com {\n    log\n    root * /var/www\n}\n"


def test_add_log_to_site_blocks_fallback_tab_indent_for_empty_block():
    content = "example.com {\n}\n"
    result = add_log_to_site_blocks(content)
    assert result == "example.com {\n\tlog\n}\n"


def test_add_log_to_site_blocks_reuses_existing_indentation():
    content = (
        "example.com {\n"
        "    root * /var/www\n"
        "    php_fastcgi localhost:9000\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    assert result == (
        "example.com {\n"
        "    log\n"
        "    root * /var/www\n"
        "    php_fastcgi localhost:9000\n"
        "}\n"
    )


def test_add_log_to_site_blocks_nested_subblocks_are_not_site_blocks():
    # Nested blocks (e.g. `handle`) are not treated as site blocks: the scan
    # jumps past each top-level block, so only the outer block gets the log.
    content = (
        "example.com {\n"
        "    handle /api/* {\n"
        "        reverse_proxy localhost:8000\n"
        "    }\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    assert "example.com {\n    log\n    handle /api/* {" in result
    assert "handle /api/* {\n        log" not in result


def test_add_log_to_site_blocks_nested_log_does_not_suppress_site_log():
    # A log directive nested inside a sub-block (e.g. `handle`) is not a
    # top-level log for the site block: the outer block still gets one so
    # access logs are emitted at site level.
    content = (
        "example.com {\n"
        "    handle /api/* {\n"
        "        log\n"
        "    }\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    assert "example.com {\n    log\n    handle /api/* {\n        log\n" in result


def test_add_log_to_site_blocks_does_not_inject_log_into_snippets():
    # Snippets `(name) { ... }` are reusable templates, not site blocks:
    # injecting a `log` into them would pollute every block that imports the
    # snippet, so they must be left untouched.
    content = (
        "(common) {\n"
        "    encode gzip\n"
        "}\n"
        "\n"
        "example.com {\n"
        "    import common\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    assert result == (
        "(common) {\n"
        "    encode gzip\n"
        "}\n"
        "\n"
        "example.com {\n"
        "    log\n"
        "    import common\n"
        "}\n"
    )


def test_add_log_to_site_blocks_preserves_comments_and_rest_of_content():
    content = (
        "# site one\n"
        "example.com {\n"
        "    root * /var/www\n"
        "}\n"
        "\n"
        "# site two\n"
        "api.example.com {\n"
        "    reverse_proxy localhost:8000\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    assert result.count("\n    log\n") == 2
    assert result.startswith("# site one\n")
    assert "# site two\n" in result


def test_add_log_to_site_blocks_crlf_mixes_line_endings():
    content = "example.com {\r\n    root * /var/www\r\n}\r\n"
    result = add_log_to_site_blocks(content)
    # Observed: the injected `log` line uses an LF newline while the original
    # lines keep their CRLF — the file ends up with mixed line endings.
    assert result == "example.com {\n    log\r\n    root * /var/www\r\n}\r\n"


def test_add_log_to_site_blocks_only_adds_to_site_blocks_without_log():
    content = (
        "a.example.com {\n"
        "    log\n"
        "    root * /a\n"
        "}\n"
        "\n"
        "b.example.com {\n"
        "    root * /b\n"
        "}\n"
    )
    result = add_log_to_site_blocks(content)
    # Block `a` already had a log (kept, no duplicate); block `b` gets one added.
    assert result == (
        "a.example.com {\n"
        "    log\n"
        "    root * /a\n"
        "}\n"
        "\n"
        "b.example.com {\n"
        "    log\n"
        "    root * /b\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# configure_caddyfile_logging
# ---------------------------------------------------------------------------

def _write_caddyfile(tmp_path, content):
    path = tmp_path / "Caddyfile"
    path.write_text(content, encoding="utf-8")
    return path


def test_configure_logging_file_not_found():
    result = configure_caddyfile_logging("/nonexistent/path/Caddyfile")
    assert result["status"] == "error"
    assert "not found" in result["message"]


def test_configure_logging_empty_file_creates_global_block(tmp_path):
    path = _write_caddyfile(tmp_path, "")
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n\tlog {\n\t\toutput stdout\n\t\tformat json\n\t\tlevel INFO\n\t}\n}\n\n")
    assert "log {" in text


def test_configure_logging_adds_log_to_existing_global_block(tmp_path):
    path = _write_caddyfile(tmp_path, "{\n    email admin@example.com\n}\n")
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    text = path.read_text(encoding="utf-8")
    assert EXPECTED_GLOBAL_LOG in text
    assert "email admin@example.com" in text


def test_configure_logging_prepends_global_block_when_missing(tmp_path):
    path = _write_caddyfile(tmp_path, "example.com {\n    root * /var/www\n}\n")
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n\tlog {\n\t\toutput stdout\n\t\tformat json\n\t\tlevel INFO\n\t}\n}\n\n")
    assert "example.com {\n    log\n    root * /var/www" in text


def test_configure_logging_idempotent_when_already_configured(tmp_path):
    content = (
        "{\n"
        "    log {\n"
        "        output stdout\n"
        "        format json\n"
        "        level INFO\n"
        "    }\n"
        "    email admin@example.com\n"
        "}\n"
        "\n"
        "example.com {\n"
        "    log\n"
        "    root * /var/www\n"
        "}\n"
    )
    path = _write_caddyfile(tmp_path, content)
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    text = path.read_text(encoding="utf-8")
    # Exactly one global log config, site log not duplicated, global content kept.
    assert text.count("output stdout") == 1
    assert text.count("example.com {\n    log") == 1
    assert "email admin@example.com" in text


def test_configure_logging_removing_first_log_squishes_newline(tmp_path):
    # Observed cosmetic quirk: when the log block is the *first* line of the
    # global block, removing it leaves the opening brace directly followed by
    # the next directive with no newline: '{    email ...'.
    content = (
        "{\n"
        "    log {\n"
        "        output stdout\n"
        "    }\n"
        "    email a@b.com\n"
        "}\n"
    )
    path = _write_caddyfile(tmp_path, content)
    configure_caddyfile_logging(path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{    email a@b.com\n\tlog {")
    assert EXPECTED_GLOBAL_LOG in text


def test_configure_logging_malformed_global_block_returns_error(tmp_path):
    path = _write_caddyfile(tmp_path, "{\n    email a@b.com")
    result = configure_caddyfile_logging(path)
    assert result["status"] == "error"
    assert "Malformed" in result["message"]


def test_configure_logging_normalizes_crlf_to_lf(tmp_path):
    # Observed: Path.read_text() applies universal newline translation, so a
    # CRLF Caddyfile is silently rewritten with LF line endings.
    path = tmp_path / "Caddyfile"
    path.write_bytes(
        b"{\r\n    email a@b.com\r\n}\r\n\r\nexample.com {\r\n    root * /var/www\r\n}\r\n"
    )
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text
    assert EXPECTED_GLOBAL_LOG in text
    assert "example.com {\n    log\n    root * /var/www" in text


def test_configure_logging_leading_comment_does_not_duplicate_global_block(tmp_path):
    # The global-block detection ignores leading comments: the existing global
    # block is modified in place and no second address-less block is prepended
    # (which would produce an invalid Caddyfile).
    content = (
        "# leading comment\n"
        "{\n"
        "    email admin@example.com\n"
        "}\n"
        "\n"
        "example.com {\n"
        "    root * /var/www\n"
        "}\n"
    )
    path = _write_caddyfile(tmp_path, content)
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    text = path.read_text(encoding="utf-8")
    # Comment preserved, single global block, exactly one injected log config.
    assert text.startswith("# leading comment\n{\n")
    assert text.count(EXPECTED_GLOBAL_LOG) == 1
    assert "email admin@example.com" in text
    assert text.count("{\n    email admin@example.com\n}") == 0
    assert "example.com {\n    log\n    root * /var/www" in text


def test_configure_logging_removes_non_block_log_directive(tmp_path):
    # A non-block `log file ...` directive in the global block is removed so it
    # does not conflict with the newly injected log config.
    path = _write_caddyfile(
        tmp_path, "{\n    log /var/log/caddy_access.log\n    email a@b.com\n}\n"
    )
    configure_caddyfile_logging(path)
    text = path.read_text(encoding="utf-8")
    assert "log /var/log/caddy_access.log" not in text
    assert EXPECTED_GLOBAL_LOG in text
    assert "email a@b.com" in text


def test_configure_logging_writes_file_and_returns_success(tmp_path):
    path = _write_caddyfile(tmp_path, "example.com {\n    root * /var/www\n}\n")
    result = configure_caddyfile_logging(path)
    assert result["status"] == "success"
    assert "message" in result
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "log {" in text
    assert "example.com {\n    log" in text


# ---------------------------------------------------------------------------
# ensure_global_servers_options
# ---------------------------------------------------------------------------

def test_ensure_global_servers_options_creates_block_when_absent():
    content = "example.com {\n    root * /var/www\n}\n"
    new_content, status = ensure_global_servers_options(content, "10m", "30s")
    assert status == "created"
    assert new_content.startswith("{\n\tservers {\n\t\ttimeouts {\n\t\t\tidle 10m\n")
    assert "\t\tkeepalive_interval 30s\n" in new_content
    assert new_content.endswith("}\n}\n\nexample.com {\n    root * /var/www\n}\n")
    # Site block untouched
    assert "example.com {\n    root * /var/www\n}\n" in new_content


def test_ensure_global_servers_options_merges_into_existing_global_block():
    content = (
        "{\n"
        "\tlog {\n"
        "\t\toutput stdout\n"
        "\t}\n"
        "}\n"
        "\n"
        "example.com {\n"
        "}\n"
    )
    new_content, status = ensure_global_servers_options(content, "15m", "45s")
    assert status == "updated"
    assert "log {" in new_content and "output stdout" in new_content
    assert "\tservers {\n" in new_content
    assert "\t\ttimeouts {\n\t\t\tidle 15m\n\t\t}\n" in new_content
    assert "\t\tkeepalive_interval 45s\n" in new_content
    # Still a single well-formed structure: global{log{}, servers{timeouts{}},
    # and the site block.
    assert new_content.count("{") == 5


def test_ensure_global_servers_options_merges_partial_config():
    # idle already present with the requested value, keepalive_interval missing:
    # only the missing option is added.
    content = (
        "{\n"
        "\tservers {\n"
        "\t\ttimeouts {\n"
        "\t\t\tidle 10m\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    new_content, status = ensure_global_servers_options(content, "10m", "30s")
    assert status == "updated"
    assert "\t\t\tidle 10m\n" in new_content          # untouched
    assert new_content.count("idle 10m") == 1         # not duplicated
    assert "\t\tkeepalive_interval 30s\n" in new_content


def test_ensure_global_servers_options_is_idempotent():
    content = "example.com {\n}\n"
    once, status1 = ensure_global_servers_options(content, "10m", "30s")
    assert status1 == "created"
    twice, status2 = ensure_global_servers_options(once, "10m", "30s")
    assert status2 == "unchanged"
    assert twice == once
    # Idempotence on the merge path as well (call twice after partial config).
    partial = "{\n\tservers {\n\t\tkeepalive_interval 30s\n\t}\n}\n"
    merged, status3 = ensure_global_servers_options(partial, "10m", "30s")
    assert status3 == "updated"
    remarried, status4 = ensure_global_servers_options(merged, "10m", "30s")
    assert status4 == "unchanged"
    assert remarried == merged


def test_ensure_global_servers_options_conflict_does_not_overwrite():
    content = (
        "{\n"
        "\tservers {\n"
        "\t\ttimeouts {\n"
        "\t\t\tidle 2h\n"
        "\t\t}\n"
        "\t\tkeepalive_interval 30s\n"
        "\t}\n"
        "}\n"
    )
    new_content, status = ensure_global_servers_options(content, "10m", "30s")
    assert status == "conflict"
    assert new_content == content  # existing values never overwritten
    assert "idle 2h" in new_content


# ---------------------------------------------------------------------------
# harden_reverse_proxy_in_site
# ---------------------------------------------------------------------------

def test_harden_reverse_proxy_simple_single_line():
    content = "app.example.com {\n    reverse_proxy localhost:8080\n}\n"
    new_content, status = harden_reverse_proxy_in_site(
        content, "localhost:8080", flush=True, ka_idle="5m", ka_interval="30s")
    assert status == "updated"
    assert (
        "    reverse_proxy localhost:8080 {\n"
        "        flush_interval -1\n"
        "        transport http {\n"
        "            keepalive 5m\n"
        "            keepalive_interval 30s\n"
        "        }\n"
        "    }\n"
    ) in new_content


def test_harden_reverse_proxy_multiline_block():
    content = (
        "app.example.com {\n"
        "  reverse_proxy localhost:8080 {\n"
        "    lb_policy round_robin\n"
        "  }\n"
        "}\n"
    )
    new_content, status = harden_reverse_proxy_in_site(
        content, "localhost:8080", flush=True, ka_idle="5m", ka_interval="30s")
    assert status == "updated"
    assert "lb_policy round_robin" in new_content           # preserved
    assert "flush_interval -1" in new_content
    assert "keepalive 5m" in new_content
    assert "keepalive_interval 30s" in new_content
    assert new_content.count("reverse_proxy") == 1           # not duplicated


def test_harden_reverse_proxy_already_hardened_is_noop():
    hardened = (
        "app.example.com {\n"
        "    reverse_proxy localhost:8080 {\n"
        "        flush_interval -1\n"
        "        transport http {\n"
        "            keepalive 5m\n"
        "            keepalive_interval 30s\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    new_content, status = harden_reverse_proxy_in_site(
        hardened, "localhost:8080", flush=True, ka_idle="5m", ka_interval="30s")
    assert status == "unchanged"
    assert new_content == hardened


def test_harden_reverse_proxy_migrates_legacy_keepalive_idle_token():
    # 'keepalive_idle' was never a valid Caddyfile subdirective (Caddy >= 2.9
    # rejects it); hardening must migrate it to the valid 'keepalive' form.
    content = (
        "app.example.com {\n"
        "    reverse_proxy localhost:8080 {\n"
        "        flush_interval -1\n"
        "        transport http {\n"
        "            keepalive_idle 5m\n"
        "            keepalive_interval 30s\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    new_content, status = harden_reverse_proxy_in_site(
        content, "localhost:8080", flush=True, ka_idle="5m", ka_interval="30s")
    assert status == "unchanged"   # values already correct after migration
    assert "keepalive_idle" not in new_content
    assert "keepalive 5m" in new_content and "keepalive_interval 30s" in new_content


def test_harden_reverse_proxy_partial_transport_adds_missing_keys():
    content = (
        "app.example.com {\n"
        "  reverse_proxy localhost:8080 {\n"
        "    transport http {\n"
        "      keepalive_idle 5m\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    new_content, status = harden_reverse_proxy_in_site(
        content, "localhost:8080", flush=True, ka_idle="5m", ka_interval="30s")
    assert status == "updated"
    assert "flush_interval -1" in new_content
    assert new_content.count("transport http {") == 1        # not duplicated
    assert "keepalive 5m" in new_content
    assert "keepalive_interval 30s" in new_content


def test_harden_reverse_proxy_upstream_not_found():
    content = "app.example.com {\n    reverse_proxy localhost:8080\n}\n"
    new_content, status = harden_reverse_proxy_in_site(content, "other:9999")
    assert status == "not_found"
    assert new_content == content


def test_harden_reverse_proxy_conflict_keeps_existing_transport_values():
    content = (
        "app.example.com {\n"
        "  reverse_proxy localhost:8080 {\n"
        "    transport http {\n"
        "      keepalive_idle 2m\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    new_content, status = harden_reverse_proxy_in_site(
        content, "localhost:8080", flush=True, ka_idle="5m", ka_interval="30s")
    assert status == "conflict"
    # Only the legacy token name is migrated; existing values are untouched.
    assert new_content == content.replace("keepalive_idle", "keepalive")
    assert "keepalive 2m" in new_content


def test_harden_reverse_proxy_invalid_upstream_is_error():
    for bad in ("", "a b", "with{brace}", "with#hash"):
        new_content, status = harden_reverse_proxy_in_site(
            "example.com {\n}\n", bad)
        assert status == "error"
        assert new_content == "example.com {\n}\n"


def test_harden_reverse_proxy_handles_multiple_sites_and_flush_disabled():
    content = (
        "a.example.com {\n"
        "    reverse_proxy app:3000 {\n"
        "        lb_policy first\n"
        "    }\n"
        "}\n"
        "\n"
        "b.example.com {\n"
        "\treverse_proxy app:3000\n"
        "}\n"
    )
    new_content, status = harden_reverse_proxy_in_site(
        content, "app:3000", flush=False, ka_idle="7m", ka_interval="60s")
    assert status == "updated"
    assert "flush_interval" not in new_content               # flush disabled
    assert new_content.count("keepalive 7m") == 2           # both occurrences
    assert new_content.count("keepalive_interval 60s") == 2
