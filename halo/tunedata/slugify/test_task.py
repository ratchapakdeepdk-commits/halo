import unittest
from slug import slugify

class T(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
    def test_accents(self):
        self.assertEqual(slugify("Crème Brûlée à la carte"), "creme-brulee-a-la-carte")
    def test_runs(self):
        self.assertEqual(slugify("  a -- b__c  "), "a-b-c")
    def test_trunc(self):
        s = slugify("abc def ghi", max_len=8)
        self.assertEqual(s, "abc-def")
    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")
        self.assertEqual(slugify("日本語"), "")
