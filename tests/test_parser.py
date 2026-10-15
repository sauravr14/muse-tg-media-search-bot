"""Unit tests for the filename parser."""
import unittest

from app.parser import normalize_title, parse_filename


class ParserTest(unittest.TestCase):
    def test_movie_basic(self):
        p = parse_filename("Inception.2010.1080p.BluRay.x264.mkv")
        self.assertEqual(p.title, "Inception")
        self.assertEqual(p.year, 2010)
        self.assertEqual(p.resolution, "1080p")
        self.assertEqual(p.codec, "x264")
        self.assertEqual(p.quality, "BluRay")
        self.assertEqual(p.kind, "movie")

    def test_movie_no_year(self):
        p = parse_filename("Dune.Part.Two.2160p.WEB-DL.mkv")
        self.assertEqual(p.title, "Dune Part Two")
        self.assertIsNone(p.year)
        self.assertEqual(p.resolution, "2160p")

    def test_series_sxxexx(self):
        p = parse_filename("Breaking.Bad.S01E02.720p.WEB-DL.x265.mkv")
        self.assertEqual(p.kind, "series")
        self.assertEqual(p.season, 1)
        self.assertEqual(p.episode, 2)
        self.assertEqual(p.title, "Breaking Bad")
        self.assertEqual(p.resolution, "720p")

    def test_series_compact(self):
        p = parse_filename("The.Boys.S3E5.1080p.mkv")
        self.assertEqual((p.kind, p.season, p.episode), ("series", 3, 5))
        self.assertEqual(p.title, "The Boys")

    def test_series_x_pattern(self):
        p = parse_filename("Dark.1x03.720p.mkv")
        self.assertEqual((p.kind, p.season, p.episode), ("series", 1, 3))

    def test_series_words(self):
        p = parse_filename("Loki Season 2 Episode 1 1080p.mp4")
        self.assertEqual((p.kind, p.season, p.episode), ("series", 2, 1))
        self.assertEqual(p.title, "Loki")

    def test_episode_only(self):
        p = parse_filename("Panchayat E04 720p.mkv")
        self.assertEqual(p.kind, "series")
        self.assertEqual(p.season, 1)
        self.assertEqual(p.episode, 4)

    def test_year_not_season(self):
        p = parse_filename("2012.2009.1080p.mkv")
        self.assertEqual(p.kind, "movie")
        self.assertEqual(p.year, 2009)

    def test_brackets_dropped(self):
        p = parse_filename("[TGx]Inception (2010) [1080p].mkv")
        self.assertEqual(p.title, "Inception")
        self.assertEqual(p.year, 2010)

    def test_normalize(self):
        self.assertEqual(normalize_title("Breaking-Bad!"), "breaking bad")
        self.assertEqual(normalize_title("  Dune: Part Two "), "dune part two")

    def test_grouping_key_same(self):
        a = parse_filename("Inception.2010.480p.mkv")
        b = parse_filename("Inception.2010.720p.mkv")
        c = parse_filename("Inception.2010.1080p.mkv")
        self.assertEqual((a.norm_title, a.year), (b.norm_title, b.year))
        self.assertEqual((b.norm_title, b.year), (c.norm_title, c.year))
        self.assertEqual({a.resolution, b.resolution, c.resolution}, {"480p", "720p", "1080p"})


if __name__ == "__main__":
    unittest.main()
