"""Tests for ``sigmond.diag_sftp`` — the pure helpers behind
``smd diag sftp``.

Subprocess-driven behaviour (ssh-keyscan / ssh-keygen / ssh) is
exercised by the live command in ``bin/smd``; here we cover the
parsing + classification logic the helpers extract.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.diag_sftp import (
    DEFAULT_PORT,
    DEFAULT_USER,
    classify_flapping,
    extract_env_var,
    find_env_var,
    parse_server,
    parse_servers_list,
    parse_ssh_keygen_l_line,
)


class ParseServerTests(unittest.TestCase):

    def test_full_form(self):
        self.assertEqual(
            parse_server("wsprdaemon@gw1.wsprdaemon.org:22"),
            ("wsprdaemon", "gw1.wsprdaemon.org", 22),
        )

    def test_just_host(self):
        self.assertEqual(
            parse_server("gw2.wsprdaemon.org"),
            (DEFAULT_USER, "gw2.wsprdaemon.org", DEFAULT_PORT),
        )

    def test_user_at_host_no_port(self):
        self.assertEqual(
            parse_server("alice@gw"),
            ("alice", "gw", DEFAULT_PORT),
        )

    def test_host_with_port_no_user(self):
        self.assertEqual(
            parse_server("gw3:2200"),
            (DEFAULT_USER, "gw3", 2200),
        )

    def test_custom_defaults(self):
        self.assertEqual(
            parse_server("gw4", default_user="ops", default_port=2222),
            ("ops", "gw4", 2222),
        )

    def test_whitespace_tolerant(self):
        self.assertEqual(
            parse_server("  bob@host:443  "),
            ("bob", "host", 443),
        )

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            parse_server("")
        with self.assertRaises(ValueError):
            parse_server("   ")

    def test_empty_user_rejected(self):
        with self.assertRaises(ValueError):
            parse_server("@host")

    def test_empty_host_rejected(self):
        with self.assertRaises(ValueError):
            parse_server("user@:22")

    def test_non_integer_port_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-integer port"):
            parse_server("user@host:not-a-number")


class ParseServersListTests(unittest.TestCase):

    def test_typical_env_var_value(self):
        # Matches the actual WD_SFTP_SERVERS shape we see in the wild.
        self.assertEqual(
            parse_servers_list(
                "wsprdaemon@gw1.wsprdaemon.org,wsprdaemon@gw2.wsprdaemon.org"
            ),
            [
                ("wsprdaemon", "gw1.wsprdaemon.org", 22),
                ("wsprdaemon", "gw2.wsprdaemon.org", 22),
            ],
        )

    def test_empty_list_returns_empty(self):
        self.assertEqual(parse_servers_list(""), [])
        self.assertEqual(parse_servers_list("  ,  ,  "), [])

    def test_trailing_comma_tolerated(self):
        self.assertEqual(
            parse_servers_list("gw1,gw2,"),
            [
                (DEFAULT_USER, "gw1", DEFAULT_PORT),
                (DEFAULT_USER, "gw2", DEFAULT_PORT),
            ],
        )

    def test_malformed_entry_propagates(self):
        # If ANY entry is malformed, surface the error so the operator
        # fixes the config — don't silently drop entries.
        with self.assertRaises(ValueError):
            parse_servers_list("good@host,bad@:22")


class ExtractEnvVarTests(unittest.TestCase):

    SAMPLE = (
        "# wsprdaemon-server gateway list\n"
        "WD_RECEIVER_CALL=AC0G/B4\n"
        'WD_SFTP_SERVERS="gw1.wsprdaemon.org,gw2.wsprdaemon.org"\n'
        "HS_UPLOADER_SSH_KEY_FILE=/etc/hs-uploader/keys/id_ed25519\n"
        "\n"
        "# trailing comment\n"
    )

    def test_extract_present(self):
        self.assertEqual(
            extract_env_var(self.SAMPLE, "WD_RECEIVER_CALL"),
            "AC0G/B4",
        )

    def test_strips_double_quotes(self):
        self.assertEqual(
            extract_env_var(self.SAMPLE, "WD_SFTP_SERVERS"),
            "gw1.wsprdaemon.org,gw2.wsprdaemon.org",
        )

    def test_unquoted_value(self):
        self.assertEqual(
            extract_env_var(self.SAMPLE, "HS_UPLOADER_SSH_KEY_FILE"),
            "/etc/hs-uploader/keys/id_ed25519",
        )

    def test_absent_returns_none(self):
        self.assertIsNone(
            extract_env_var(self.SAMPLE, "NOT_SET"),
        )

    def test_comment_line_ignored(self):
        text = "# WD_SFTP_SERVERS=should-not-match\nWD_SFTP_SERVERS=real\n"
        self.assertEqual(extract_env_var(text, "WD_SFTP_SERVERS"), "real")

    def test_first_match_wins(self):
        # systemd EnvironmentFile= semantics: first occurrence is what
        # the running unit sees; later duplicates are dead code.
        text = "WD_SFTP_SERVERS=first\nWD_SFTP_SERVERS=second\n"
        self.assertEqual(extract_env_var(text, "WD_SFTP_SERVERS"), "first")

    def test_strips_single_quotes(self):
        text = "K='value with spaces'\n"
        self.assertEqual(extract_env_var(text, "K"), "value with spaces")

    def test_unmatched_quotes_passed_through(self):
        # ``it's a path`` shouldn't be mutilated.  Asymmetric quotes
        # mean the operator probably wrote them on purpose.
        text = "K=it's a path\n"
        self.assertEqual(extract_env_var(text, "K"), "it's a path")


class FindEnvVarTests(unittest.TestCase):

    def test_missing_dir_returns_none(self):
        self.assertIsNone(
            find_env_var(Path("/nonexistent/dir/very-much-not-there"),
                         "WD_SFTP_SERVERS"),
        )

    def test_walks_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "first.env").write_text(
                "WD_RECEIVER_CALL=AC0G/B4\n"
            )
            (d / "second.env").write_text(
                "WD_SFTP_SERVERS=gw1.example,gw2.example\n"
            )
            self.assertEqual(
                find_env_var(d, "WD_SFTP_SERVERS"),
                "gw1.example,gw2.example",
            )

    def test_glob_pattern_applies(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Off-pattern file should be ignored.
            (d / "ignored.txt").write_text("WD_SFTP_SERVERS=wrong\n")
            (d / "real.env").write_text("WD_SFTP_SERVERS=right\n")
            self.assertEqual(find_env_var(d, "WD_SFTP_SERVERS"), "right")

    def test_not_found_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "only.env").write_text("ANOTHER=val\n")
            self.assertIsNone(find_env_var(d, "WD_SFTP_SERVERS"))


class ParseSshKeygenLineTests(unittest.TestCase):
    """``ssh-keygen -l -f -`` line shape: ``<bits> <fp> <comment> (<TYPE>)``."""

    def test_ed25519(self):
        line = "256 SHA256:abc gw2.wsprdaemon.org (ED25519)"
        self.assertEqual(
            parse_ssh_keygen_l_line(line),
            ("ED25519", line),
        )

    def test_rsa(self):
        line = "3072 SHA256:def gw2.wsprdaemon.org (RSA)"
        self.assertEqual(
            parse_ssh_keygen_l_line(line),
            ("RSA", line),
        )

    def test_no_trailing_paren_returns_none(self):
        self.assertIsNone(parse_ssh_keygen_l_line("256 SHA256:abc host"))

    def test_no_open_paren_returns_none(self):
        self.assertIsNone(parse_ssh_keygen_l_line("256 SHA256:abc host)"))

    def test_empty_parens_returns_none(self):
        # Parser is defensive against ``()`` even though ssh-keygen
        # itself would never produce that.
        self.assertIsNone(parse_ssh_keygen_l_line("256 SHA256:abc h ()"))


class ClassifyFlappingTests(unittest.TestCase):
    """The core diagnostic logic — given per-attempt fingerprint sets,
    decide which key TYPES are flapping."""

    def test_stable_single_type(self):
        # 3 attempts, 1 type, all same fingerprint → stable.
        attempts = [
            {"ED25519": "256 SHA256:abc h (ED25519)"},
            {"ED25519": "256 SHA256:abc h (ED25519)"},
            {"ED25519": "256 SHA256:abc h (ED25519)"},
        ]
        self.assertEqual(classify_flapping(attempts), {})

    def test_stable_mixed_types(self):
        # 3 attempts, both ED25519 + RSA, all stable.  This is the
        # normal case for any modern OpenSSH server — must NOT be
        # flagged.  This was the false-positive in Cut 1 of the live
        # ``smd diag sftp`` run.
        ed = "256 SHA256:abc h (ED25519)"
        rsa = "3072 SHA256:def h (RSA)"
        attempts = [
            {"ED25519": ed, "RSA": rsa},
            {"ED25519": ed, "RSA": rsa},
            {"ED25519": ed, "RSA": rsa},
        ]
        self.assertEqual(classify_flapping(attempts), {})

    def test_flap_single_type(self):
        # ED25519 returns different fingerprints between attempts.
        ed_a = "256 SHA256:aaa h (ED25519)"
        ed_b = "256 SHA256:bbb h (ED25519)"
        attempts = [
            {"ED25519": ed_a},
            {"ED25519": ed_b},
            {"ED25519": ed_a},
        ]
        result = classify_flapping(attempts)
        self.assertEqual(set(result.keys()), {"ED25519"})
        self.assertEqual(result["ED25519"], [ed_a, ed_b])

    def test_flap_one_type_stable_other(self):
        # RSA flapping, ED25519 stable.  Result must surface ONLY the
        # flapping type so the operator knows which key type to chase.
        ed = "256 SHA256:abc h (ED25519)"
        rsa_a = "3072 SHA256:r1 h (RSA)"
        rsa_b = "3072 SHA256:r2 h (RSA)"
        attempts = [
            {"ED25519": ed, "RSA": rsa_a},
            {"ED25519": ed, "RSA": rsa_b},
        ]
        result = classify_flapping(attempts)
        self.assertEqual(set(result.keys()), {"RSA"})
        self.assertEqual(result["RSA"], [rsa_a, rsa_b])

    def test_flap_multiple_types(self):
        # Both types flapping simultaneously — both should appear.
        ed_a = "256 SHA256:e1 h (ED25519)"
        ed_b = "256 SHA256:e2 h (ED25519)"
        rsa_a = "3072 SHA256:r1 h (RSA)"
        rsa_b = "3072 SHA256:r2 h (RSA)"
        attempts = [
            {"ED25519": ed_a, "RSA": rsa_a},
            {"ED25519": ed_b, "RSA": rsa_b},
        ]
        result = classify_flapping(attempts)
        self.assertEqual(set(result.keys()), {"ED25519", "RSA"})

    def test_partial_attempt_handled(self):
        # If one attempt fails to scan a particular key type (server
        # was momentarily unreachable for that scan), the absence
        # mustn't count as a flap.  Two ED25519 fingerprints across
        # the attempts that DID see one would though.
        ed = "256 SHA256:abc h (ED25519)"
        attempts = [
            {"ED25519": ed, "RSA": "3072 SHA256:r1 h (RSA)"},
            {"ED25519": ed},                                   # no RSA scanned
            {"ED25519": ed, "RSA": "3072 SHA256:r1 h (RSA)"},
        ]
        self.assertEqual(classify_flapping(attempts), {})

    def test_empty_input(self):
        self.assertEqual(classify_flapping([]), {})
        self.assertEqual(classify_flapping([{}, {}, {}]), {})


if __name__ == "__main__":
    unittest.main()
