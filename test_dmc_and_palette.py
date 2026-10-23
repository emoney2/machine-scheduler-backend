import json
import unittest

from dmc_colors import load_dmc_chart, suggest_dmc_for_hex
from needlepoint_belts import extract_palette_colors_from_package


class DmcAndPaletteTests(unittest.TestCase):
    def test_dmc_chart_loads(self):
        chart = load_dmc_chart()
        self.assertGreater(len(chart), 400)
        self.assertTrue(any(c["code"] == "310" for c in chart))

    def test_suggest_near_black(self):
        matches = suggest_dmc_for_hex("#111111", limit=3)
        self.assertEqual(len(matches), 3)
        self.assertEqual(matches[0]["code"], "310")

    def test_extract_palette_from_studio_package(self):
        package = {
            "kind": "jr-needlepoint-studio-package",
            "document": {
                "width": 4,
                "height": 2,
                "palette": [
                    {"hex": "#111111", "name": "Ink"},
                    {"hex": "#C72B3B"},
                    {"hex": "#FFFFFF"},
                ],
                "cells": [
                    [0, 0, 1, 2],
                    [0, 1, 1, 2],
                ],
            },
            "order": {
                "customerViewPng": "data:image/png;base64,aa",
            },
        }
        colors = extract_palette_colors_from_package(json.dumps(package))
        self.assertEqual(len(colors), 3)
        self.assertEqual(colors[0]["hex"], "#111111")
        self.assertEqual(colors[0]["stitchCount"], 3)
        self.assertEqual(colors[0]["name"], "Ink")
        self.assertTrue(colors[0]["suggestions"])
        self.assertEqual(colors[0]["suggestions"][0]["code"], "310")


if __name__ == "__main__":
    unittest.main()
