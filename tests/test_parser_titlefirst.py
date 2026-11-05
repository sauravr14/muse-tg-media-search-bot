"""Title-first boundary parser golden cases (spec v2, 2026-11-05).

One row per real filename, in the form
    (filename, title, year, kind, season, episode, skip)
Add every future mis-parse as a row BEFORE fixing it.

"Mr & Mrs Smith" (not "Mr. & Mrs. Smith") and "Lgm" (not "LGM") are the
parser's title() normalisation; the matcher normalises further (R15).
"""
import unittest

from app.parser import parse_filename


CASES = [
    # --- v1's 18 filenames ---
    ("LGM (let's get married) (2023) 720p 10bit AMZN WEBRip x2.mkv",
     "Lgm", 2023, "movie", None, None, ""),
    ("Paid.in.Full.2002.720p.WEBRip.800MB.x264-GalaxyRG.mkv",
     "Paid In Full", 2002, "movie", None, None, ""),
    ("Why_We_Hate_S01_COMPLETE_1080p_AMZN_WEB-DL_HINDI_6CH.mkv",
     "Why We Hate", None, "series", 1, None, ""),
    ("Wrong.Turn.2021.1080p.Blu-Ray.DUAL.x264.AAC.ESub-Skymovi.mkv",
     "Wrong Turn", 2021, "movie", None, None, ""),
    ("Your Honor S01 [E01-08] COMBiNED 720p AMZN WEB-DL x265 HEVC .mkv",
     "Your Honor", None, "series", 1, None, ""),
    ("Hear Me S01E22 Hear Me EP 22 720p AMZN WEB DL DDP2 0 @RjRip.mkv",
     "Hear Me", None, "series", 1, 22, ""),
    ("TheMoviesBoss_Kabzaa_2023_480p_AMZN_WebRip_Multi_AAC2_0.mkv",
     "Kabzaa", 2023, "movie", None, None, ""),
    ("Crackdown_S02E03_Garima_grows_frustrated_1080p_JIO_WEB_DL_DD+5_1.mkv",
     "Crackdown", None, "series", 2, 3, ""),
    ("Love.Like.The.Galaxy.S01E24.Storm.In.The.Court.720p.AMZN.WEB.mkv",
     "Love Like The Galaxy", None, "series", 1, 24, ""),
    ("The.Rookie.S08E15.Survive.the.Streets.1080p.10bit.AMZN.WEB-D.mkv",
     "The Rookie", None, "series", 8, 15, ""),
    ("Fisaddi (2024) S01 720p AMZN WEB-DL Hindi AAC 2.0 ~ ANUP.mkv",
     "Fisaddi", 2024, "series", 1, None, ""),
    ("Camila Comes Out Tonight (2021) UNRATED 720p HEVC BluRay x26.mkv",
     "Camila Comes Out Tonight", 2021, "movie", None, None, ""),
    ("Shin_Chan_Movie_Villain_aur_Dulhan_Hindi_Dubbed_BluRay_720p_MoviesVerse.mkv",
     "Shin Chan Movie Villain Aur Dulhan", None, "movie", None, None, ""),
    ("Spider_Man_HomeComing_1_Movie_in_Multiple_Language_Tamil+English+Telugu+Hindi.mkv",
     "Spider Man Homecoming", None, "movie", None, None, ""),
    ("[CC].S02E10.Attack.on.Titan.720p.BluRay.x264-Pahe.mkv",
     "Attack On Titan", None, "series", 2, 10, ""),
    ("@QueenMirrorOfficial EPIK v4 3LEECH BY @QueenMirrorOfficial.apk",
     "", None, "movie", None, None, "non-video"),
    ("S01 E09 - [].mp4",
     "", None, "series", 1, 9, "no-title"),
    ("Throne.Of.Seal.(Shen.Yin.Wangzuo).20.mkv",
     "Throne Of Seal", None, "series", None, 20, ""),
    # --- spec v2 edge cases ---
    ("Hindi.Medium.2017.1080p.BluRay.x264.mkv",
     "Hindi Medium", 2017, "movie", None, None, ""),
    ("[REC] 2007 720p BluRay.mkv",
     "Rec", 2007, "movie", None, None, ""),
    ("S.W.A.T.2017.S01E01.720p.HDTV.mkv",
     "Swat", 2017, "series", 1, 1, ""),
    ("2001.A.Space.Odyssey.1968.1080p.BluRay.mkv",
     "2001 A Space Odyssey", 1968, "movie", None, None, ""),
    ("Blade.Runner.2049.2017.1080p.mkv",
     "Blade Runner 2049", 2017, "movie", None, None, ""),
    ("Wonder.Woman.1984.2020.1080p.BluRay.mkv",
     "Wonder Woman 1984", 2020, "movie", None, None, ""),
    ("Kung.Fu.Panda.4.1080p.WEB-DL.mkv",
     "Kung Fu Panda 4", None, "movie", None, None, ""),
    ("[SubsPlease] Sousou no Frieren - 12 (1080p) [ABCD1234].mkv",
     "Sousou No Frieren", None, "series", None, 12, ""),
    ("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR10+.HEVC-CMRG.mkv",
     "Dune Part Two", 2024, "movie", None, None, ""),
    ("Mr. & Mrs. Smith (2005) 720p.mkv",
     "Mr & Mrs Smith", 2005, "movie", None, None, ""),
    ("Breaking.Bad.1x05.720p.mkv",
     "Breaking Bad", None, "series", 1, 5, ""),
]


class TestParserTitleFirst(unittest.TestCase):
    def test_golden_cases(self):
        for raw, title, year, kind, season, episode, skip in CASES:
            with self.subTest(raw=raw):
                p = parse_filename(raw)
                self.assertEqual(
                    (p.title, p.year, p.kind, p.season, p.episode,
                     p.extra.get("skip", "")),
                    (title, year, kind, season, episode, skip))

    def test_lgm_alias_captured(self):
        p = parse_filename("LGM (let's get married) (2023) 720p 10bit AMZN WEBRip x2.mkv")
        self.assertIn("let's get married", p.extra.get("aliases", []))

    def test_soft_phrase_skipped_when_year_known(self):
        # "Complete" before a known year may be the title itself
        # ("A Complete Unknown"); soft-phrase cuts apply only with no year.
        p = parse_filename("A Complete Unknown 2024 1080p BluRay.mkv")
        self.assertEqual(p.title, "A Complete Unknown")
        self.assertFalse(p.is_pack)
        p = parse_filename("Movie Complete 720p.mkv")
        self.assertEqual(p.title, "Movie")

    def test_wonder_woman_alt(self):
        # No release year: 1984 is read as the year, so the true title
        # ("Wonder Woman 1984") is offered as a matcher alternate (R7).
        p = parse_filename("Wonder.Woman.1984.1080p.BluRay.mkv")
        self.assertEqual((p.title, p.year), ("Wonder Woman", 1984))
        self.assertIn("Wonder Woman 1984", p.extra.get("alts", []))


if __name__ == "__main__":
    unittest.main()
