#!/usr/bin/env python

import unittest
from swift_undelete import middleware as md


class FakeTest(unittest.TestCase):
    def test_works(self):
        self.assertEqual("world", md.hello())
