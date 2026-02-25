"""Tests for mclsp.flavor — FlavorResolver and _flavor_from_string."""
from __future__ import annotations


class TestFlavorResolver:
    def test_defaults_to_mcstas(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        assert r.resolve('file:///home/user/instrument.instr') == Flavor.MCSTAS

    def test_uri_heuristic_mcxtrace(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        assert r.resolve('file:///home/user/mcxtrace-3.0/test.instr') == Flavor.MCXTRACE

    def test_uri_heuristic_mcstas(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        assert r.resolve('file:///opt/mcstas/lib/test.instr') == Flavor.MCSTAS

    def test_explicit_workspace_override_wins(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_workspace_flavor(Flavor.MCSTAS)
        assert r.resolve('file:///mcxtrace/something.instr') == Flavor.MCSTAS

    def test_clearing_workspace_flavor_restores_inference(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_workspace_flavor(Flavor.MCSTAS)
        r.set_workspace_flavor(None)
        result = r.resolve('file:///mcxtrace/something.instr')
        assert result == Flavor.MCXTRACE

    def test_explicit_document_override(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_document_flavor('file:///neutral/test.instr', Flavor.MCXTRACE)
        assert r.resolve('file:///neutral/test.instr') == Flavor.MCXTRACE

    def test_forget_removes_document(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_document_flavor('file:///test.instr', Flavor.MCXTRACE)
        r.forget('file:///test.instr')
        assert r.resolve('file:///test.instr') == Flavor.MCSTAS

    def test_project_config_file(self, tmp_path):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        config = tmp_path / '.mclsp.toml'
        config.write_text('flavor = "mcxtrace"\n')
        r = FlavorResolver(workspace_root=str(tmp_path))
        assert r.resolve('file:///neutral/test.instr') == Flavor.MCXTRACE

    def test_project_config_file_missing_falls_through(self, tmp_path):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver(workspace_root=str(tmp_path))
        assert r.resolve('file:///neutral/test.instr') == Flavor.MCSTAS

    def test_infer_from_source_with_mock_registries(self):
        """Component-based inference works when registries have distinct names."""
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        import unittest.mock as mock

        mcstas_comps   = frozenset({'Progress_bar', 'Arm', 'E_monitor'})
        mcxtrace_comps = frozenset({'Arm', 'ESRF_BM', 'Filter_crystal'})

        source = 'DEFINE INSTRUMENT T()\nTRACE\nCOMPONENT a = ESRF_BM()\nAT (0,0,0) ABSOLUTE\nEND\n'
        r = FlavorResolver()
        with mock.patch('mclsp.flavor._known_components',
                        side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            result = r.resolve('file:///neutral/test.instr', source=source)
        assert result == Flavor.MCXTRACE

    def test_infer_caches_result(self):
        """Once inferred, the result is cached and returned without re-parsing."""
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        import unittest.mock as mock

        mcstas_comps   = frozenset({'Progress_bar'})
        mcxtrace_comps = frozenset({'ESRF_BM'})
        source = 'COMPONENT a = ESRF_BM()\n'

        r = FlavorResolver()
        with mock.patch('mclsp.flavor._known_components',
                        side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            r.resolve('file:///t.instr', source=source)

        assert r.resolve('file:///t.instr') == Flavor.MCXTRACE

    def test_re_infer_updates_after_new_component(self):
        """Adding a discriminating component on edit updates the cached flavor."""
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        import unittest.mock as mock

        mcstas_comps   = frozenset({'Progress_bar', 'Arm'})
        mcxtrace_comps = frozenset({'Arm', 'ESRF_BM'})

        r = FlavorResolver()
        source1 = 'COMPONENT a = Arm()\n'
        with mock.patch('mclsp.flavor._known_components',
                        side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            r.resolve('file:///t.instr', source=source1)

        source2 = source1 + 'COMPONENT b = ESRF_BM()\n'
        with mock.patch('mclsp.flavor._known_components',
                        side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            r2 = r.re_infer('file:///t.instr', source=source2)

        assert r2 == Flavor.MCXTRACE

    def test_flavor_from_string(self):
        from mclsp.flavor import _flavor_from_string
        from mccode_antlr import Flavor
        assert _flavor_from_string('mcxtrace') == Flavor.MCXTRACE
        assert _flavor_from_string('MCSTAS') == Flavor.MCSTAS
        assert _flavor_from_string('invalid') is None
        assert _flavor_from_string('') is None
