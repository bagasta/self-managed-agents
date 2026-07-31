"""CLI entrypoint for the isolated legacy Arthur seed package."""
from arthur.seed import seed


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Seed legacy Arthur")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(seed(dry_run=args.dry_run))
