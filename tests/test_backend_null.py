"""v0.4.0: null backend は廃止。このファイルはプレースホルダー。

null backend が必要なユーザーは capture_enabled=False / inject_enabled=False で
同等の動作を得られる。
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="null backend は v0.4.0 で廃止 (capture_enabled=False で代替)")
def test_placeholder() -> None:
    pass
