"""金额工具测试：整数分、恒等分摊与尾差规则。"""
import unittest
from decimal import Decimal

from sourcepack.money import allocate, from_cents, to_cents


class MoneyTests(unittest.TestCase):
    def test_to_cents_rounding(self):
        self.assertEqual(to_cents("1.005"), 101)
        self.assertEqual(to_cents(800), 80000)
        self.assertEqual(from_cents(16000), Decimal("160.00"))

    def test_allocate_identity_and_floor_tie(self):
        # 100 元按 1:1:1 分摊：先各 3333 分，余 1 分归业务键升序第一项
        shares = allocate(10000, [("a", 1), ("b", 1), ("c", 1)])
        self.assertEqual(sum(shares.values()), 10000)
        self.assertEqual(shares, {"a": 3334, "b": 3333, "c": 3333})

    def test_allocate_largest_base_gets_remainder(self):
        shares = allocate(100, [("x", 9), ("y", 1)])
        self.assertEqual(sum(shares.values()), 100)
        self.assertEqual(shares["x"], 90)
        self.assertEqual(shares["y"], 10)

    def test_allocate_zero_bases(self):
        shares = allocate(500, [("x", 0), ("y", 0)])
        self.assertEqual(shares, {"x": 0, "y": 0})

    def test_package_pk1_fixed_denominator(self):
        # 资料口径明示：PK1 总权重 1000，合同价 800
        shares = allocate(80000, [("S1", 600), ("S2", 400)])
        self.assertEqual(shares, {"S1": 48000, "S2": 32000})


if __name__ == "__main__":
    unittest.main()
