"""Demo 2: off-by-one pagination drops the last partial page. Ask: 'Why are 3 items missing?'"""

def paginate(items, size):
    # BUG: range(0, n // size) drops the remainder page
    n = len(items)
    pages = []
    for i in range(0, n // size):
        pages.append(items[i * size:(i + 1) * size])
    return pages


def main():
    items = list(range(1, 24))  # 23 items, page size 10 -> expect 3 pages, get 2
    size = 10
    pages = paginate(items, size)
    flat = [x for p in pages for x in p]
    missing = [x for x in items if x not in flat]
    print(f"Items: {len(items)}, pages: {len(pages)}, shown: {len(flat)}")
    print(f"Missing: {missing}")


if __name__ == "__main__":
    main()
