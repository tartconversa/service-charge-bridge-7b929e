"""金额与合同分摊工具。

金额在仓内一律以人民币整数分（BIGINT）存储，避免浮点尾差；
来源金额先按字符串转 Decimal 再四舍五入到分。
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("1")


def to_cents(amount) -> int:
    """来源金额（元）转整数分，半数向上舍入。空值由调用方处理。"""
    return int((Decimal(str(amount)) * 100).quantize(CENT, rounding=ROUND_HALF_UP))


def from_cents(cents) -> Decimal:
    """整数分转回元，供报表展示。"""
    return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))


def allocate(total_cents, items):
    """按基数把整数分总额分摊到各键。

    items 为 ``(key, base)`` 序列，base 非负。先按比例向下取整，
    剩余每一分依次分给基数最大项，基数并列按业务键升序。
    基数全为零时各项分得 0；返回 ``dict[key, int]``，各项之和恒等于总额。
    """
    total_cents = int(total_cents)
    bases = [(key, Decimal(str(base))) for key, base in items]
    total_base = sum((base for _, base in bases), Decimal("0"))
    if total_base <= 0:
        return {key: 0 for key, _ in bases}
    raw = [
        (key, base, Decimal(total_cents) * base / total_base)
        for key, base in bases
    ]
    floors = {key: int(value.to_integral_value(rounding="ROUND_FLOOR")) for key, _, value in raw}
    remainder = total_cents - sum(floors.values())
    order = sorted(raw, key=lambda triple: (-triple[1], str(triple[0])))
    result = dict(floors)
    for key, _, _ in order:
        if remainder == 0:
            break
        result[key] += 1
        remainder -= 1
    assert remainder == 0
    assert sum(result.values()) == total_cents
    return result
