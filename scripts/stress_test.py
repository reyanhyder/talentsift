import argparse
import concurrent.futures
import time
import urllib.request


def hit(url):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            response.read()
            return response.status, time.perf_counter() - started
    except Exception:
        return 0, time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description="Basic TalentSift smoke/stress check.")
    parser.add_argument("--url", default="https://talentsift-production.up.railway.app/_version")
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(hit, [args.url] * args.requests))

    statuses = {}
    durations = []
    for status, duration in results:
        statuses[status] = statuses.get(status, 0) + 1
        durations.append(duration)

    total = time.perf_counter() - started
    avg = sum(durations) / len(durations)
    print(f"URL: {args.url}")
    print(f"Requests: {args.requests}, concurrency: {args.concurrency}")
    print(f"Statuses: {statuses}")
    print(f"Average latency: {avg:.2f}s")
    print(f"Total time: {total:.2f}s")


if __name__ == "__main__":
    main()
