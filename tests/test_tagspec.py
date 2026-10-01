# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from conftest import write_config

from smackcicd import config, tagspec


@pytest.fixture
def both(home):
    write_config(home, '[platforms]\ndefault = ["Windows", "Android"]\n'
                       '[platforms.Linux]\nenabled = true\n')
    cfg = config.load(home)
    # Make every platform "buildable" regardless of the test machine's OS.
    cfg.enabled_platforms = lambda: ["Windows", "Android", "Linux"]
    return cfg


@pytest.mark.parametrize("tag, configuration, platforms, prerelease", [
    ("v1.4.2", "Shipping", ["Windows", "Android"], False),
    ("v1.4.2-rc.1", "Shipping", ["Windows", "Android"], True),
    ("v1.4.2-beta.3", "Development", ["Windows", "Android"], True),
    ("v1.4.2-alpha.1+android", "Development", ["Android"], True),
    ("v1.4.2-beta.2+win.linux", "Development", ["Windows", "Linux"], True),
    ("v1.4.2-quest", "Shipping", ["Android"], False),
    ("v1.4.2-nightly.4", "Development", ["Windows", "Android"], True),
    ("v1.4.2+all", "Shipping", ["Windows", "Android", "Linux"], False),
])
def test_channels_and_platforms(both, tag, configuration, platforms, prerelease):
    spec = tagspec.parse(tag, both)
    assert spec.configuration == configuration
    assert spec.platforms == platforms
    assert spec.is_prerelease is prerelease


def test_selectors_do_not_leak_into_the_version(both):
    spec = tagspec.parse("v2.0.1-beta.4+android.ue54", both)
    assert spec.semver == "2.0.1-beta.4"
    assert spec.channel == "beta" and spec.channel_number == 4
    assert spec.engines == ["5.4"]
    assert spec.metadata == ""


@pytest.mark.parametrize("token, version", [
    ("ue54", "5.4"), ("UE58", "5.8"), ("ue5_10", "5.10"), ("ue510", "5.10"),
    ("ue10_1", "10.1"), ("ue4_27", "4.27"), ("android", None), ("ue", None),
])
def test_engine_tokens(token, version):
    assert tagspec.engine_version(token) == version


def test_two_engines_build_twice(both):
    assert tagspec.parse("v1.0.0-beta.1+ue53.ue54", both).engines == ["5.3", "5.4"]


def test_rejections(both):
    with pytest.raises(tagspec.TagRejected):
        tagspec.parse("nightly", both)
    with pytest.raises(tagspec.TagRejected):
        tagspec.parse("v1.2", both)


def test_disabled_platform_is_dropped(home):
    write_config(home, '[platforms]\ndefault = ["Windows", "Android"]\n'
                       '[platforms.Android]\nenabled = false\n')
    cfg = config.load(home)
    with pytest.raises(tagspec.TagRejected):
        tagspec.parse("v1.0.0+android", cfg)
    assert tagspec.parse("v1.0.0", cfg).platforms == ["Windows"]


def test_version_codes_always_increase(both):
    order = ["v1.2.3-alpha.1", "v1.2.3-alpha.2", "v1.2.3-beta.1", "v1.2.3-rc.1", "v1.2.3",
             "v1.2.4-alpha.1", "v1.3.0"]
    codes = [tagspec.parse(t, both).android_version_code for t in order]
    assert codes == sorted(codes) and len(set(codes)) == len(codes)
    assert tagspec.parse("v1.2.3-beta.2", both).android_version_code == 10_203_302
    assert tagspec.parse("v209.99.99", both).android_version_code < 2_100_000_000
    assert tagspec.parse("v210.0.0", both).android_version_code > 2_100_000_000


def test_slug_is_folder_safe(both):
    assert tagspec.parse("v1.0.0-beta.1+win", both).slug() == "v1.0.0-beta.1_win"
