# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the configuration reference honest: every setting is documented."""

from pathlib import Path

from smackcicd.config import DEFAULTS, FREE_FORM

DOCS = (Path(__file__).resolve().parent.parent / "docs" / "configuration.md").read_text(
    encoding="utf-8")


def leaf_settings(table, prefix=""):
    for key, value in table.items():
        dotted = prefix + key
        if isinstance(value, dict) and dotted not in FREE_FORM and dotted != "channels":
            yield from leaf_settings(value, dotted + ".")
        else:
            yield dotted


def test_every_setting_is_in_the_configuration_reference():
    missing = []
    for dotted in leaf_settings(DEFAULTS):
        section, _, name = dotted.rpartition(".")
        if not section:                      # a free-form table such as [channels]
            if "`[%s]`" % name not in DOCS:
                missing.append(dotted)
            continue
        if dotted.startswith("platforms.") and section != "platforms":
            section = "platforms"            # per-platform keys share one table
        if "`%s`" % name not in DOCS and "`" + name not in DOCS:
            missing.append(dotted)
        if "`[%s]`" % section not in DOCS:
            missing.append("section [%s]" % section)
    assert not missing, "document these in docs/configuration.md: %s" % sorted(set(missing))
