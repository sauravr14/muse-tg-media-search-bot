"""Regression tests: junk after the release year must never leak into titles."""
import unittest

from app.parser import parse_filename


class TailCutTest(unittest.TestCase):
    def t(self, name, title, year=None, kind="movie"):
        p = parse_filename(name)
        self.assertEqual(p.title, title, name)
        if year is not None:
            self.assertEqual(p.year, year, name)
        self.assertEqual(p.kind, kind, name)
        return p

    def test_truncated_tags_after_year(self):
        # screenshot bug: duplicate cards "... We", "... BI", "... X2", "... X26", "... Web"
        for tail in ("We", "BI", "X2", "X26", "Web", "WEB-DL", "BluRay x26"):
            self.t(f"Spider Man No Way Home 2021 {tail}", "Spider Man No Way Home", 2021)
        self.t("Spider.Man.No.Way.Home.2021.1080p.WEB-DL.DDP5.1.Atmos.x264.mkv",
               "Spider Man No Way Home", 2021)

    def test_channel_handle_and_year_in_brackets(self):
        self.t("@MoviesHub_Pushpa_The_Rise(2021) 1080p.mkv", "Pushpa The Rise", 2021)
        self.t("@SomeChannel Dune Part Two (2024) 2160p WEB-DL.mkv", "Dune Part Two", 2024)

    def test_years_inside_titles(self):
        self.t("Wonder Woman 1984 2020 1080p BluRay.mkv", "Wonder Woman 1984", 2020)
        self.t("1917.2019.720p.BluRay.mkv", "1917", 2019)
        self.t("2012.2009.1080p.mkv", "2012", 2009)
        self.t("Blade Runner 2049 2017 720p.mkv", "Blade Runner 2049", 2017)

    def test_episode_marker_after_year_is_kept(self):
        p = self.t("Show Name 2019 E02 720p.mkv", "Show Name", 2019, kind="series")
        self.assertEqual((p.season, p.episode), (1, 2))

    def test_series_with_year(self):
        p = self.t("Lanterns.2026.S01E03.720p.WEB-DL.mkv", "Lanterns", 2026, kind="series")
        self.assertEqual((p.season, p.episode), (1, 3))

    def test_year_first_falls_back_to_whole_name(self):
        # nothing before the year -> do not return an empty title
        self.t("[Hindi] 2021 Movie Name 720p.mkv", "Movie Name", 2021)

    def test_quality_fields_still_detected(self):
        p = parse_filename("Spider Man No Way Home 2021 1080p BluRay x265.mkv")
        self.assertEqual((p.resolution, p.quality, p.codec), ("1080p", "BluRay", "x265"))


if __name__ == "__main__":
    unittest.main()
