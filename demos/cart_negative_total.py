"""Demo 1: coupon applied twice -> negative total. Ask: 'Why is the total negative?'"""

def calc_discount(coupons):
    discount = 0.0
    for c in coupons:
        if c == "SAVE5":
            discount += 5.0
    return discount


def main():
    prices = [3.50, 2.00, 1.00]
    subtotal = sum(prices)
    coupons = []
    coupons.append("SAVE5")
    # BUG: same coupon added twice (e.g. double-click / retry)
    coupons.append("SAVE5")
    discount = calc_discount(coupons)
    total = subtotal - discount
    print(f"Subtotal: {subtotal:.2f}")
    print(f"Discount: {discount:.2f}")
    print(f"Total: {total:.2f}")


if __name__ == "__main__":
    main()
