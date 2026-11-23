"""Check the analytical Boresch correction from saved B-state parameters."""

import tempfile
import unittest
from pathlib import Path

from utils.boresch import calculate_boresch_release, estimate_boresch_release

# B-state values from Yuan DOR_10; deliberately different A-state parameters.
TOPOLOGY = """
; Saved GROMACS restraints, distances in nm and angles in degrees.
[ intermolecular_interactions ]
[ bonds ]
1 4 6 0.1 100 0.761 3835.35 ; rA kA rB kB
[ angles ]
2 1 4 1 20 10 97.357 467.50
1 4 5 1 30 10 135.564 463.32
[ dihedrals ]
3 2 1 4 2 0 10 110.030 214.26
2 1 4 5 2 0 10 -96.254 176.02
1 4 5 6 2 0 10 -154.420 147.67
"""


class BoreschTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.complex = Path(temporary.name) / "run/complex"
        self.topology = self.complex.parent / "boresch/BoreschRestraint.top"
        self.topology.parent.mkdir(parents=True)
        self.topology.write_text(TOPOLOGY)
        self.mdp = self.complex / "rest.00/prod/rest.prod.00.mdp"
        self.mdp.parent.mkdir(parents=True)
        self.mdp.write_text("ref-t = 298.15 298.15 ; protein and solvent\n")

    def test_b_state_formula_and_kcal_conversion(self):
        # Independent evaluation of the supplied formula in kJ, divided by 4.184.
        self.assertAlmostEqual(
            calculate_boresch_release(self.topology, 298.15),
            -9.23382693797829,
            places=10,
        )
        self.assertAlmostEqual(
            estimate_boresch_release(self.complex), -9.23382693797829, places=10
        )

    def test_production_temperature_changes_the_correction(self):
        self.mdp.write_text("ref_t = 310 310\n")
        self.assertAlmostEqual(
            estimate_boresch_release(self.complex),
            calculate_boresch_release(self.topology, 310),
        )
        self.assertNotEqual(
            estimate_boresch_release(self.complex),
            calculate_boresch_release(self.topology, 298.15),
        )

    def test_uses_complex_copy_only_when_primary_topology_is_absent(self):
        copy = self.complex / "BoreschRestraint.top"
        copy.write_text("invalid topology")
        expected = estimate_boresch_release(self.complex)
        copy.write_text(TOPOLOGY)
        self.topology.unlink()
        self.assertEqual(estimate_boresch_release(self.complex), expected)
        self.topology.write_text("invalid topology")
        with self.assertRaises(ValueError):
            estimate_boresch_release(self.complex)

    def test_rejects_invalid_or_incomplete_restraints(self):
        for old, new in (
            ("0.761 3835.35", "0 3835.35"),
            ("0.761 3835.35", "0.761 0"),
            ("97.357 467.50", "0 467.50"),
            ("135.564 463.32", "180 463.32"),
            ("135.564 463.32", "135.564 -1"),
            ("-154.420 147.67", "-154.420 nan"),
            ("-154.420 147.67", "-154.420 inf"),
            ("-154.420 147.67", "-154.420 text"),
            ("1 4 6", "1 4 3"),
            ("1 4 5 6 2 0 10 -154.420 147.67", ""),
            ("[ angles ]", "[ angles ]\n2 1 4 1 20 10 97.357 467.50"),
            ("1 4 6 0.1 100 0.761 3835.35", "1 4 6 0.761 3835.35"),
        ):
            with self.subTest(new=new):
                self.topology.write_text(TOPOLOGY.replace(old, new))
                with self.assertRaises(ValueError):
                    estimate_boresch_release(self.complex)

    def test_rejects_missing_ambiguous_or_invalid_temperature(self):
        for contents in (
            "gen-temp = 298.15\n",
            "ref-t = 298.15 310\n",
            "ref-t =\n",
            "ref-t = 0\n",
            "ref-t = -1\n",
            "ref-t = nan\n",
            "ref-t = inf\n",
            "ref-t = text\n",
        ):
            with self.subTest(contents=contents):
                self.mdp.write_text(contents)
                with self.assertRaises(ValueError):
                    estimate_boresch_release(self.complex)
        self.mdp.unlink()
        with self.assertRaises(FileNotFoundError):
            estimate_boresch_release(self.complex)

    def test_missing_topology_is_an_error(self):
        self.topology.unlink()
        with self.assertRaises(FileNotFoundError):
            estimate_boresch_release(self.complex)


if __name__ == "__main__":
    unittest.main()
