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


    def test_release_tags_stripped(self):
        p = parse_filename("The Pianist 2002 REMASTERED 720p 10Bit BluRay NF Engli.mkv")
        self.assertEqual(p.title, "The Pianist")
        self.assertEqual(p.year, 2002)
        self.assertEqual(p.resolution, "720p")

    def test_hash_tokens_stripped(self):
        p = parse_filename("Un Cœur En Hiver 1992 4K Nz87Gp0H5Au 247.mp4")
        self.assertEqual(p.title, "Un Cœur En Hiver")
        self.assertEqual(p.year, 1992)

    def test_uuid_stripped(self):
        p = parse_filename("Movie.Name.18240152-890F-4406-Ad34-C553B08A829D.1080p.WEB-DL.mkv")
        self.assertEqual(p.title, "Movie Name")

    def test_numeric_title_kept(self):
        p = parse_filename("21 Jump Street 2012 1080p BluRay x264.mkv")
        self.assertEqual(p.title, "21 Jump Street")
        self.assertEqual(p.year, 2012)

    def test_audio_tags_stripped(self):
        p = parse_filename("Dune Part Two 2024 IMAX 2160p WEB-DL DDP5.1 Atmos H.265.mkv")
        self.assertEqual(p.title, "Dune Part Two")
        self.assertEqual(p.year, 2024)
    def test_site_tags_stripped(self):
        p = parse_filename("Egybest Cloudy With A Chance Of Meatballs 2 2013 720P X264")
        self.assertEqual(p.title, "Cloudy With A Chance Of Meatballs 2")
        self.assertEqual(p.year, 2013)
        p = parse_filename("Iron Sky The Coming Race Mycima Tv 2019")
        self.assertEqual(p.title, "Iron Sky The Coming Race")

    def test_leading_site_id_stripped(self):
        p = parse_filename("1354295 Outlander 2008 Cima4U")
        self.assertEqual(p.title, "Outlander")
        self.assertEqual(p.year, 2008)

    def test_unclosed_bracket_stripped(self):
        p = parse_filename("Dark Figure Of Crime Hq [Tam Tel")
        self.assertEqual(p.title, "Dark Figure Of Crime")

    def test_sequel_number_kept(self):
        p = parse_filename("1682878 The Shop Around The Corner 1940 Cima4U 0")
        self.assertEqual(p.title, "The Shop Around The Corner")

    def test_flattened_audio_tags(self):
        p = parse_filename("Congratulations 2009 1080P Web Dl Ddp2 0 H 264 Aliraqi")
        self.assertEqual(p.title, "Congratulations")
        self.assertEqual(p.quality, "WEB-DL")

    def test_underscore_filename(self):
        p = parse_filename("Lanterns_2026_S01E01_720p_10bit_AMZN_WEBRip_HEVX_x265_JHS_Hindi.mkv")
        self.assertEqual(p.title, "Lanterns")
        self.assertEqual(p.year, 2026)
        self.assertEqual(p.kind, "series")
        self.assertEqual((p.season, p.episode), (1, 1))
        self.assertEqual(p.resolution, "720p")

    def test_pack_complete(self):
        p = parse_filename("Lanterns_2026_S01_COMBINED_720p_10bit_AMZN_WEBRip_HEVC_x265_JHS.mkv")
        self.assertTrue(p.is_pack)
        self.assertEqual(p.pack_label, "Complete")
        self.assertEqual(p.season, 1)
        self.assertIsNone(p.episode)
        self.assertEqual(p.title, "Lanterns")

    def test_pack_range(self):
        p = parse_filename("Lanterns 2026 S01 COMBINED [E01-04] 1080p 10bit AMZN.mkv")
        self.assertTrue(p.is_pack)
        self.assertEqual(p.pack_label, "E01\u2013E04")
        self.assertEqual(p.resolution, "1080p")

    def test_complete_unknown_not_pack(self):
        p = parse_filename("A Complete Unknown 2024 1080p BluRay.mkv")
        self.assertFalse(p.is_pack)
        self.assertEqual(p.title, "A Complete Unknown")

    def test_trailing_at_stripped(self):
        p = parse_filename("The.Furious.ESubs.MoviesMod.At.2026.mkv")
        self.assertEqual(p.title, "The Furious")
        p = parse_filename("At Close Range 1986 1080p.mkv")
        self.assertEqual(p.title, "At Close Range")

    def test_curly_brace_pack_range(self):
        p = parse_filename("13_Reasons_Why_2017_S01_{E01_06}_Combined_720p_10bit_NF_WEBRip_HEVC.mkv")
        self.assertEqual(p.title, "13 Reasons Why")
        self.assertTrue(p.is_pack)
        self.assertEqual(p.pack_label, "E01\u2013E06")
        self.assertEqual(p.season, 1)

    def test_curly_brace_pack_range_dash(self):
        p = parse_filename("13 Reasons Why (2017) S02 {E01-06} Combined 720p 10bit N.mkv")
        self.assertEqual(p.title, "13 Reasons Why")
        self.assertEqual(p.pack_label, "E01\u2013E06")

    def test_audio_channel_stripped(self):
        p = parse_filename("12_Monkeys_S01_COMBiNED_720p_WEB_DL_2CH_x265_HEVC_PSAOyeSale.mkv")
        self.assertEqual(p.title, "12 Monkeys")
        self.assertTrue(p.is_pack)

    def test_release_group_suffix_stripped(self):
        p = parse_filename("12_Monkeys_S02_COMBiNED_720p_WEB_DL_2CH_x265_HEVC_ESub_Harsh.mkv")
        self.assertEqual(p.title, "12 Monkeys")

if __name__ == "__main__":
    unittest.main()

