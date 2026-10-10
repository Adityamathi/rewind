"""Demo 3: mutable default arg shares state. Ask: 'Why does the second user see the first user's data?'"""

def add_to_bucket(item, bucket=[]):
    # BUG: default list is shared across calls
    bucket.append(item)
    return bucket


def main():
    alice = add_to_bucket("alice-item")
    print(f"Alice bucket: {alice}")
    bob = add_to_bucket("bob-item")
    print(f"Bob bucket: {bob}")
    print(f"Same object: {alice is bob}")


if __name__ == "__main__":
    main()
