"""Tests for McDoc header ↔ SETTING PARAMETERS mismatch diagnostics."""
from __future__ import annotations

from mclsp.document import parse_document

_COMP_WITH_MCDOC_MISMATCH = """\
/*******************************************************************************
* Component: TestComp
*
* %I
* Written by: Test
* Date: 2025
* Origin: Test
*
* A test component.
*
* %D
* Does nothing.
*
* %P
* INPUT PARAMETERS:
*
* x: [m]  The x parameter
* obsolete_param: [1]  This parameter no longer exists
*
* %E
*******************************************************************************/
DEFINE COMPONENT TestComp
SETTING PARAMETERS (x=0, new_param=1.0)
TRACE %{ %}
END
"""

_COMP_WITHOUT_MCDOC = """\
DEFINE COMPONENT TestComp
SETTING PARAMETERS (x=0)
TRACE %{ %}
END
"""


class TestMcDocDiagnostics:
    def _compute(self, source):
        import mclsp.server as srv
        uri = 'test://TestComp.comp'
        srv._docs[uri] = parse_document(uri, source)
        srv._update_mcdoc_diags(uri)
        diags = srv._mcdoc_diags.get(uri, [])
        srv._docs.pop(uri, None)
        srv._mcdoc_diags.pop(uri, None)
        return diags

    def test_mismatch_detected(self):
        diags = self._compute(_COMP_WITH_MCDOC_MISMATCH)
        messages = [d.message for d in diags]
        assert any('new_param' in m for m in messages), messages
        assert any('obsolete_param' in m for m in messages), messages

    def test_missing_header_warning(self):
        diags = self._compute(_COMP_WITHOUT_MCDOC)
        messages = [d.message for d in diags]
        assert any('missing' in m.lower() for m in messages)
        assert any('x' in m for m in messages), messages

    def test_no_diags_when_header_matches(self):
        source = """\
/*******************************************************************************
* Component: Good
*
* %I
* Written by: Test
* Date: 2025
* Origin: Test
*
* A test.
*
* %D
* Does something.
*
* %P
* INPUT PARAMETERS:
*
* x: [m]  The x parameter
*
* %E
*******************************************************************************/
DEFINE COMPONENT Good
SETTING PARAMETERS (x=0)
TRACE %{ %}
END
"""
        assert self._compute(source) == []
