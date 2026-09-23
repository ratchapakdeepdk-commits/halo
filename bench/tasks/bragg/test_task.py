import math, unittest
from bragg import d_spacing, two_theta, allowed_fcc, first_peaks_fcc

CU = 1.5406

class T(unittest.TestCase):
    def test_d(self):
        self.assertAlmostEqual(d_spacing(4.0, 1, 1, 1), 4 / math.sqrt(3))
        with self.assertRaises(ValueError): d_spacing(4.0, 0, 0, 0)
    def test_2theta_silicon_like(self):
        # Al, a = 4.0495 A: (111) at ~38.47 deg with Cu K-alpha
        self.assertAlmostEqual(two_theta(d_spacing(4.0495, 1, 1, 1), CU), 38.47, delta=0.02)
        with self.assertRaises(ValueError): two_theta(0.5, CU)
    def test_fcc_rule(self):
        self.assertTrue(allowed_fcc(1, 1, 1)); self.assertTrue(allowed_fcc(2, 0, 0))
        self.assertFalse(allowed_fcc(1, 0, 0)); self.assertFalse(allowed_fcc(2, 1, 0))
        self.assertFalse(allowed_fcc(0, 0, 0))
    def test_first_peaks(self):
        peaks = first_peaks_fcc(4.0495, CU, n=5)
        self.assertEqual([p[0] for p in peaks], [(1,1,1), (2,0,0), (2,2,0), (3,1,1), (2,2,2)])
        self.assertTrue(all(peaks[i][1] < peaks[i+1][1] for i in range(4)))
        self.assertAlmostEqual(peaks[1][1], 44.72, delta=0.03)
