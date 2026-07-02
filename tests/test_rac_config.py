"""Tests for sigmond.rac_config — frpc activation render/read."""

import unittest

from sigmond.rac_config import (
    placeholder, read_frpc_values, render_frpc_config,
)

# Mirrors sigmond-rac/config/frpc.toml.template after install.sh renders
# the proxy name.
TEMPLATE = '''\
# RAC (frpc) reverse-tunnel config TEMPLATE.
# fleet constants below — do not edit.

serverAddr = "gw2.wsprdaemon.org"
serverPort = 35736
user = "<RAC_USER_FROM_WD_ADMIN>"

[auth]
method = "token"
token = "<RAC_TOKEN_FROM_WD_ADMIN>"

[transport.tls]
enable = true
trustedCaFile = "/etc/sigmond/frps-ca.crt"

[webServer]
addr = "127.0.0.1"
port = 7500

[[proxies]]
name = "AC0G/SIGMA"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 0   # <SSH_REMOTEPORT_FROM_WD_ADMIN>

[[proxies]]
name = "AC0G/SIGMA-WEB"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8081
remotePort = 0   # <WEB_REMOTEPORT_FROM_WD_ADMIN>
'''


class TestRenderFrpcConfig(unittest.TestCase):

    def test_full_assignment(self):
        out = render_frpc_config(TEMPLATE, user="ac0g-sigma",
                                 token="s3cret", ssh_port=35802,
                                 web_port=35803)
        vals = read_frpc_values(out)
        self.assertEqual(vals["user"], "ac0g-sigma")
        self.assertEqual(vals["token"], "s3cret")
        self.assertEqual(vals["ssh_port"], 35802)
        self.assertEqual(vals["web_port"], 35803)
        self.assertEqual(vals["proxy"], "AC0G/SIGMA")
        # fleet constants untouched
        self.assertIn('serverAddr = "gw2.wsprdaemon.org"', out)
        self.assertIn("serverPort = 35736", out)
        self.assertIn('trustedCaFile = "/etc/sigmond/frps-ca.crt"', out)
        # placeholder comments dropped once filled
        self.assertNotIn("FROM_WD_ADMIN", out)

    def test_no_web_port_drops_web_proxy(self):
        out = render_frpc_config(TEMPLATE, user="u", token="t",
                                 ssh_port=35802, web_port=0)
        vals = read_frpc_values(out)
        self.assertEqual(vals["ssh_port"], 35802)
        self.assertEqual(vals["web_port"], 0)
        self.assertNotIn("AC0G/SIGMA-WEB", out)
        # remotePort = 0 must never ship (frps rejects it)
        self.assertNotIn("remotePort = 0", out)

    def test_result_is_valid_toml(self):
        import tomllib
        out = render_frpc_config(TEMPLATE, user="u", token="t",
                                 ssh_port=35802, web_port=0)
        data = tomllib.loads(out)
        self.assertEqual(len(data["proxies"]), 1)
        self.assertEqual(data["proxies"][0]["remotePort"], 35802)

    def test_rerender_is_idempotent(self):
        once = render_frpc_config(TEMPLATE, user="u", token="t",
                                  ssh_port=35802, web_port=35803)
        twice = render_frpc_config(once, user="u", token="t",
                                   ssh_port=35802, web_port=35803)
        self.assertEqual(once, twice)

    def test_update_existing_values(self):
        once = render_frpc_config(TEMPLATE, user="old", token="oldtok",
                                  ssh_port=35802, web_port=35803)
        again = render_frpc_config(once, user="new", token="newtok",
                                   ssh_port=35899, web_port=35900)
        vals = read_frpc_values(again)
        self.assertEqual(vals["user"], "new")
        self.assertEqual(vals["token"], "newtok")
        self.assertEqual(vals["ssh_port"], 35899)
        self.assertEqual(vals["web_port"], 35900)


class TestReadFrpcValues(unittest.TestCase):

    def test_template_reads_placeholders(self):
        vals = read_frpc_values(TEMPLATE)
        self.assertTrue(placeholder(vals["user"]))
        self.assertTrue(placeholder(vals["token"]))
        self.assertEqual(vals["ssh_port"], 0)
        self.assertEqual(vals["proxy"], "AC0G/SIGMA")

    def test_garbage_returns_empty(self):
        vals = read_frpc_values("not toml [ at all")
        self.assertEqual(vals["user"], "")
        self.assertEqual(vals["ssh_port"], 0)


class TestPlaceholder(unittest.TestCase):

    def test_markers(self):
        self.assertTrue(placeholder(""))
        self.assertTrue(placeholder("<RAC_USER_FROM_WD_ADMIN>"))
        self.assertFalse(placeholder("ac0g-sigma"))


if __name__ == "__main__":
    unittest.main()
