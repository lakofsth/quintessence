# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Drift test for CONFIG.md: the committed doc must be byte-identical to a fresh
render of tools/gen_config_docs.py against the CURRENT quintessence.config.KEYS registry, so
a KeyDef edit that isn't mirrored into CONFIG.md fails the suite instead of silently drifting.
Also checks the render never leaks the *building* machine's actual $HOME (this test's own
os.path.expanduser("~")) into the table — CONFIG.md ships publicly."""
import os
import sys
import unittest

_ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ENGINE)
sys.path.insert(0, os.path.join(_ENGINE, "tools"))

import gen_config_docs  # noqa: E402

CONFIG_MD = os.path.join(_ENGINE, "CONFIG.md")


class TestConfigDocsNotDrifted(unittest.TestCase):
    def test_config_md_exists(self):
        self.assertTrue(os.path.isfile(CONFIG_MD), "CONFIG.md missing — run "
                        "`python3 tools/gen_config_docs.py --write CONFIG.md`")

    def test_config_md_matches_fresh_render(self):
        with open(CONFIG_MD) as f:
            committed = f.read()
        fresh = gen_config_docs.render()
        self.assertEqual(committed, fresh,
                          "CONFIG.md has drifted from quintessence/config.py's KEYS registry "
                          "— regenerate with `python3 tools/gen_config_docs.py "
                          "--write CONFIG.md` and commit the result.")

    def test_every_registered_key_appears(self):
        from quintessence.config import KEYS
        with open(CONFIG_MD) as f:
            committed = f.read()
        for kd in KEYS:
            self.assertIn(f"`{kd.name}`", committed, f"{kd.name} missing from CONFIG.md")

    def test_render_never_leaks_this_machines_home(self):
        # Regression guard distinct from the byte-diff above: even if CONFIG.md were hand
        # edited to "match" a leaky render, this independently checks the render function
        # itself never embeds the literal $HOME of whatever machine runs the generator.
        fresh = gen_config_docs.render()
        home = os.path.expanduser("~")
        if home and home != "/":
            self.assertNotIn(home, fresh, f"generated config docs leaked this machine's HOME "
                             f"({home!r}) instead of substituting the portable '~'")


if __name__ == "__main__":
    unittest.main()
