import unittest


class CityCodeTests(unittest.TestCase):
    def test_guangzhou_and_shenzhen_use_boss_city_codes(self):
        from bosshunter.config import CITY_CODES

        self.assertEqual(CITY_CODES["广州"], "101280100")
        self.assertEqual(CITY_CODES["深圳"], "101280600")

    def test_city_codes_cover_non_hot_prefecture_level_cities(self):
        """城市下拉框应覆盖 BOSS 支持的非热门城市，而不是仅展示固定热门城市。"""
        from bosshunter.config import CITY_CODES

        self.assertGreaterEqual(len(CITY_CODES), 300)
        self.assertEqual(CITY_CODES["齐齐哈尔"], "101050200")
        self.assertEqual(CITY_CODES["大理白族自治州"], "101291600")
        self.assertEqual(CITY_CODES["喀什地区"], "101131200")


if __name__ == "__main__":
    unittest.main()
