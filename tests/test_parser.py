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

    def test_channel_watermark_handle_and_code(self):
        p = parse_filename("@Fbm Hw Teenage Mutant Ninja Turtles 1990 720p.mkv")
        self.assertEqual(p.title, "Teenage Mutant Ninja Turtles")
        self.assertEqual(p.year, 1990)

    def test_channel_watermark_handle_only(self):
        p = parse_filename("@Mm Race 3 Pro 900Mb 2018.mkv")
        self.assertEqual(p.title, "Race 3 Pro")
        self.assertEqual(p.year, 2018)

    def test_channel_watermark_channel_word(self):
        p = parse_filename("@Mallu Movies Race 2008 700Mb.mkv")
        self.assertEqual(p.title, "Race")
        self.assertEqual(p.year, 2008)

    def test_channel_watermark_short_title_protected(self):
        p = parse_filename("@Mm It 2017 720p.mkv")
        self.assertEqual(p.title, "It")
        self.assertEqual(p.year, 2017)

    def test_channel_watermark_short_title_up_protected(self):
        p = parse_filename("@Mm Up 2009 BluRay.mkv")
        self.assertEqual(p.title, "Up")
        self.assertEqual(p.year, 2009)

    def test_channel_watermark_dotted(self):
        p = parse_filename("@Fbm.Hw.The.Matrix.1999.1080p.mkv")
        self.assertEqual(p.title, "The Matrix")
        self.assertEqual(p.year, 1999)

    def test_no_watermark_untouched(self):
        # no @handle: parser must not touch the title
        p = parse_filename("The Dark Knight 2008 720p.mkv")
        self.assertEqual(p.title, "The Dark Knight")
        self.assertEqual(p.year, 2008)

    def test_ts_inside_word_not_stripped(self):
        # "Mutants" keeps its "ts" — the CAM source pattern only matches
        # standalone TS, not the TS inside DTS/Mutants
        p = parse_filename("Mutants 2009 720p BluRay.mkv")
        self.assertEqual(p.title, "Mutants")
        p = parse_filename("The Matrix 1999 1080p BluRay DTS x264.mkv")
        self.assertEqual(p.title, "The Matrix")
        self.assertEqual(p.quality, "BluRay")

    def test_file_size_not_in_title(self):
        p = parse_filename("Vivah (2006) Hindi 1080p BluRay 3.2GB ESub [BollYFlix].mkv")
        self.assertEqual(p.title, "Vivah")
        self.assertEqual(p.year, 2006)
        p = parse_filename("Vivah (2006) Hindi 720p BluRay 1.2GB [BollyFlix].mkv")
        self.assertEqual(p.title, "Vivah")
        p = parse_filename("Some Movie 2020 700MB 480p.mkv")
        self.assertEqual(p.title, "Some Movie")

    def test_file_size_glued_to_separators(self):
        # "_450MB" has no \b before the digits ("_" is a word char),
        # so the size pattern must be separator-aware on both sides
        p = parse_filename("Vivaha_Bhojanambu_2021_HQ_HDRip_x264_Telugu_Kannada_450MB.mkv")
        self.assertEqual(p.title, "Vivaha Bhojanambu")  # language tags after the year are junk
        p = parse_filename("Movie_2020_700MB_480p.mkv")
        self.assertEqual(p.title, "Movie")
        p = parse_filename("Movie.2020.1.2GB.1080p.mkv")
        self.assertEqual(p.title, "Movie")
        # ...but a size glued to letters is not a size token
        p = parse_filename("ABC450MB 2020 720p.mkv")
        self.assertIn("450", p.title)

    def test_underscore_separators(self):
        p = parse_filename("Vivah_2006_BluRay_REMASTERED_720p_Hindi_AAC_5_1_x264_ESub_mkvCinemas.mkv")
        self.assertEqual(p.title, "Vivah")
        self.assertEqual(p.year, 2006)

    def test_site_suffix_stripped(self):
        cases = {
            "Vivah 2006 Hindi 720p AMZN WEBRip x264 - LOKiHD - Telly.mkv": "Vivah",
            "Vivah.(2006).Hindi.1080p.BluRay-IBUDDHU.mkv": "Vivah",
            "@bhaiyajihub Vivah (2006) 480p Hindi Bluray - Bhaiyajihub.mp4": "Vivah",
            "Movie 2020 1080p BluRay x264-YTS.mkv": "Movie",
            "Movie 2020 1080p WEB-DL x264-PRiEST.mkv": "Movie",
        }
        for raw, exp in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_filename(raw).title, exp)

    def test_hashtag_watermark_stripped(self):
        p = parse_filename("#Nexleech_Vivah_2006_BluRay_REMASTERED_Hindi_480p_ESub_mkvCinemas.mkv")
        self.assertEqual(p.title, "Vivah")
        self.assertEqual(p.year, 2006)

    def test_midname_handle_stripped(self):
        p = parse_filename("Vivah @bhaiyajihub 2006 720p BluRay.mkv")
        self.assertEqual(p.title, "Vivah")
        # but an email-like token is left alone
        p = parse_filename("user@example.com file 2020 720p.mkv")
        self.assertIn("Example", p.title)

    def test_release_tag_soup(self):
        # 540p/HR/OTT/AAC5.1/PMI all stripped -> variants merge to one title
        for raw in [
            "Vivaha.Avahanam.2023.540p.HR.OTT.WEB-DL.AAC5.1.H.264-PMI.mkv",
            "Vivaha.Avahanam.2023.720p.HR.OTT.WEB-DL.AAC5.1.H.264-PMI.mkv",
            "Vivaha.Avahanam.2023.1080p.HR.OTT.WEB-DL.AAC5.1.H.264-PM.mkv",
        ]:
            with self.subTest(raw=raw):
                p = parse_filename(raw)
                self.assertEqual(p.title, "Vivaha Avahanam")
                self.assertEqual(p.year, 2023)

    def test_hin_language_code_stripped(self):
        p = parse_filename("@Jesseverse Vivaha Bhojanambu 2021 Hin Tel Esub 480p WebRip.mkv")
        self.assertEqual(p.title, "Vivaha Bhojanambu")
        self.assertEqual(p.year, 2021)

    def test_leading_emoji_stripped(self):
        p = parse_filename("🎬 Vivah (2006) Hindi 1080p BluRay.mkv")
        self.assertEqual(p.title, "Vivah")

    def test_no_way_home_release_tag_family(self):
        """Real filenames that used to splinter Spider-Man: No Way Home
        into ~28 titles: ORG/BMS/HYBRID/BCORE tags, truncated tokens
        (HEV, BluRa, Web D, AAC_5), glued channel brands, split-archive
        part markers and version tags all strip to the one title."""
        cases = [
            "Spider_Man_No_Way_Home_2021_720p_10bit_BluRay_x265_HEVC_Org_BMS.mkv",
            "Spider_Man:_No_Way_Home_2021_Extended_Cut_HYBRID_IMAX_1080p_x265.mkv",
            "Spider_Man:_No_Way_Home_2021_720p_BluRay_10bit_HEVC_Hindi_AAC_5.mkv",
            "@Gmovies_Hub_|Spider_Man_No_Way_Home_2021_WebRip_EXTENDED_Hin_Eng.mkv",
            "@Gmovies_Hub_Spider_Man_No_Way_Home_2021_BluRay_Hindi_Cam_+_English_ORG.mkv",
            "@VIP_MOVIES_BACKUPSpider Man: No Way Home @VIP_MOVIES_BACKUP.mkv",
            "Spider_Man_No_Way_Home_2021_1080p_BCORE_IMAX_WEBRip_x265_10bit_Hindi.mkv",
            "Spider-Man - No Way Home (2021) 1080p UHD HDR10 DV BluRa.mkv",
            "[MM] Spider-Man No Way Home (2021) Blu-Ray - 720p - HEV.mkv",
            "Spider-Man: No.Way.Home.2021.720p.SonyLiv.WEB-DL.HINDI-M.mkv",
            "Spider-Man: No.Way.Home.2021.1080p.SonyLiv.WEB-D.part001.mkv",
            "Spider_Man_No_Way_Home_2021_Hindi_English_720p_V2_HQ_HD_CAM_x264.mkv",
            "Spider_Man_No_Way_Home_2021_720p_10bit_BRRip_Hin_Eng_x265_HEVC_KatmovieHD.mkv",
            "TheMoviesBoss_Spider_Man_No_Way_Home_Extended_Version_2021_480p.mkv",
            "Spider-Man - No Way Home (2021) 1080p 10bit 60FP.part001.mkv",
            "Spider_Man_No_Way_Home_2021_EXTENDED_720p_10bit_WEBRip_x.mkv",
            "Spider_man:_No_way_Home_2021_Hindi_ORG_English_ORG_BluRAY_720pHEVC",
            "Spider_Man:_No_Way_Home_2021_720p_BluRay_ORG_Dual_Audio_Hindi_DD.mkv",
            "Spider_Man_No_Way_Home_2022_English_BluRay_720p_@Gmovies_Hub.mkv",
        ]
        for name in cases:
            with self.subTest(name=name):
                p = parse_filename(name)
                # norm_title is the grouping key (display titles may
                # keep a ":" — "Spider Man: No Way Home")
                self.assertEqual(p.norm_title, "spider man no way home")
                self.assertEqual(p.kind, "movie")

    def test_no_way_home_extended_2022_keeps_its_year(self):
        # the extended-version re-release files genuinely claim 2022;
        # the title cleans up but the year is data, not junk
        p = parse_filename(
            "Spider_Man_No_Way_Home_Extended_Version_2022_720p_10bit_DS4K_WEBRip.mkv")
        self.assertEqual(p.norm_title, "spider man no way home")
        self.assertEqual(p.year, 2022)

    def test_new_strips_do_not_eat_real_titles(self):
        # guards for the aggressive additions above
        p = parse_filename("Uncut.Gems.2019.1080p.BluRay.x264.mkv")
        self.assertEqual(p.title, "Uncut Gems")
        p = parse_filename("Dune.Part.Two.2024.1080p.WEB-DL.mkv")
        self.assertEqual(p.title, "Dune Part Two")
        p = parse_filename("The.Version.1999.DVDRip.mkv")
        self.assertEqual(p.title, "The Version")
        # the intact "AAC 5.1" path must keep working end to end
        p = parse_filename("Some.Movie.2020.720p.BluRay.AAC.5.1.x264.mkv")
        self.assertEqual(p.title, "Some Movie")

if __name__ == "__main__":
    unittest.main()

