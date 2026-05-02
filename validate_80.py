"""Validate the 80 frequency wallets — keep only profitable ones."""
import sys
sys.path.insert(0, '.')
from validate_wallets import analyze_wallet

WALLETS = [
    "HCjUmZTEq87UeEx1Sw8V9hkG34csJFFGtLNwq48x63kt", "odinjHrFM5LGewQhZ3tichTy2nETqMLXiYJX5PxuWbg",
    "Gygj9QQby4j2jryqyqBHvLP7ctv2SaANgh4sCb69BUpA", "CC1Vo9y9183zoiyckDwmwwNYhTB8vfyP7UGjZUR1wX4m",
    "Aiz8BvMt9WudK8vCZQVYDBkkLTYim7san4578p6xVwud", "Xwu6DKqGo4wKPBAPvNYHsjMTV2JxqmW6ubuvhQYKu6E",
    "haqqiUUXB5gp7gDbSjpEkw3phAjQb25r86uXJsZQgWq", "BCdJLWTX26JTcWxThPPxohvaqup3rwmSSDDGTBpya6Le",
    "DMZjZJKVXqpF3NgtpmGfdy4t65JJoLMh1RAcZceYnjBc", "FsxdGbUT9kziKdez5fCpaBBJ8FDKXkqjGwaGuo2Mq3M",
    "CWzpWzJ2bhV1x5Rvhy7WGCSxvVVqk2ZVjmYCpLE7WHti", "Aywk7AN98iTnwaK9ZBafYqGL14xM65zJF68sVC8UkAiY",
    "GEBsDVsiynTD7zW5cNr9Ed71HYthm4Xef3Jmr9JTZJyg", "EWf4UWoQ9TdcY4j2wpqbgDjvPw42J8SgknusTNd5jANX",
    "64hP97Bwr5PubotcTeGgfhkFrGiLVVxT2kVo9M9b4AEz", "9NyKGwDHk5mVwR5UPShQpWPBqs2APuPZDQsnib5mocwR",
    "CCTrzM9RcQ6knvGNjD85jrmcjkTytjpPASfqddFdsgsX", "FroTwW3qY6ndH5xApXwEzLujzDP5m375adzcYhECv7VT",
    "eedjS2MAS7VHzRFqXZNcoi8S7rJSHKPsUUjwS4vAULT", "omegoMAe1AMY5MFKQQr3JwXVy8F4eCvmBAfcpo8XAfq",
    "Ack1QVrWLNTT1BqoZXye4FoTxHWTnx3HCBwyn3Mc5kV3", "BsE7X6bpG9KnxCY9oc5NDKf1GYMJ3F3ofTAqnPC9GWqf",
    "9Q4TpsUMco8hU6xY3LAseURi3mmjMKVWDwuvEqvZuEQk", "6FyjER59DQkVtXDM2pPPwonpZpSv4dEWXx35aCUymFGd",
    "8PUSP1nTeh8KcWk5FaN384wS9sckFmEsw8kz8Chz7N93", "4mLmXQgkEThAWcKpPrXPSUG6QvoCexpBERy7DMXeNAh6",
    "4qFfFMuDqLFMQ3XQjCcFddxx9Y764JTR9AT5f2C79ujs", "3msNuQVG4yTiWSJaVGzMvAaRQi4VX8NxN4NJvfVR9jD8",
    "A9LFnzLAoK1r337qqnvsrySWvA6HyHy7sShzXNAS9w9F", "GyxV38Dio9va7XDUMfGYN5jWo21rJUdgv6F1AV3W3VxQ",
    "574QABp92xpXN4rYfBZFMQLBuFPLDWLJAeP3ZuYbXL1i", "BnHZ99JebZEw17DXQnrnZeF3VqEBLigCNxxmv8GcobKC",
    "4WMdFmMR4TTHRf4tst6i3kif4MqjT9xwytm6qNLBxJqs", "7yRY9sNxNujWUqUBgBpMpA4vJUm9nJUnQvR43WYDYtdv",
    "5M9A9xQ2SRXeGs3MoWKvz4MFScBcojEiwTDfV41cMuWw", "3QqmsedKtYV9SAtohH4H4ihXwdGMEb7pCP5AHTcDRgNQ",
    "Fkemk4DNp9ncsixH8gJqoysHu8mvG2A1TMVbGTMoYa9d", "3uRApJaCNXQ1NtL7c5v5NedXGpRt1MQpoKva2FsGdbdm",
    "SQHK48QT8SY1vYN44iXji7wQ6CJek8AjfX6mBp47TZq", "AvXDgtQqehm9gSvg4pc6swRpHXVSeYTjk6fiSx9k6GGZ",
    "CsWYeNerkJRabFNd9UqixyH58knjvgcZmFVvV15oV5z3", "Ao4SYqcfyKzrxeUZezzFh457zVqYJbShjvUmVypZbsJ4",
    "9tU27EScUhbuFJG3rH5GvQ1kVTagFppY4UYrYe9jNrMj", "4tH7jjPRZmXgFJvUTLK13YkRg1kjWQSUWJa1bqRVjJsE",
    "DzpVESbfz8FLBRt7yjFNE1fZnbpSyxaBWTd8Bhz8qeaW", "Efq7Nqf5Yv7wEZX7JCYqc8EJc5SqUVpacGJ5LCWKvHLS",
    "GLZSQ9RZ1GghzpvhMjyuBy2cC7ky3XJj7fw6yC4vn7K6", "6hrnKDRsWT1PPfBgxST1pbSwkWfBpnpP3oDDstN2tpnG",
    "EuKbiJuzfnwTkWnSFCsrV58PcD5CAzEo25pZAZJP6649", "C6GY1sX96ULkj4EkVLjVDdN7vxejHcGeepec7T7VGzxC",
    "HoCezVGvY7aZosLs7cxV7UtoA8NjtLnpWLw9YiJ2mPCL", "CsDL8yHruRnxRGdnwwTrsrsX7DWsuEPFBk2VP6BXK6AY",
    "3Ksc9EE8wR8ZzkkuKPywNAkeeCh5DhKByBxQPGzgQ3o5", "EsWPskUe3o5t8Z1cLZgHCnQanUUakKApsX7JJYhr1FyW",
    "5fjZatNnqmWixUtycekodpsaNHCHVe48oBzaLbV4kXRL", "6ccknqfnao4ASbGsGio921PREtMGYFHXve4qjsvn1oHc",
    "GKDiL2NHBzk82rQFHpGdiN6y7tu7WGGFTHGdgDyqzLCk", "FYTVwP5hgCUiB14eYYTPtZpBCBL4tqbYFbRkjmRwbNto",
    "D8y4tB2Nw5gqPahzBYhMMP4PgyCaeV2mmhjaBQwgeGiH", "EM1E9gJ8gxroiVsjzMLexvwHdttvX2Sn6ooXvTYXzDQ1",
    "Uf3onHjKuZYGQiLXtWECELGsdyqCeiAk78c6V4LwRcy", "FcjY4NAg8DnCNrZXubvBcCE6TuvRrzNYgcW4F85Ug1m3",
    "5qmuztAZGnAX1o2NBjMD3B8tPtSJh9wVy8TgT1pZ7nMK", "4jiyvWrE6qk1wS5pSxnbvS6hjzBH6ZE25ZX6si1RC4PW",
    "8gCJYyKWnKGoY6Di3iF1iUErfXHpQyiaGLB1TyRYbhcF", "B59mW345zuSj1fhBFp6fzDoTXgksmFwncyX2xiraQPAx",
    "FaVqTAXTfTdL5qHoZ99NAXZKU7PkuTfVkPfNLwzzA1fX", "FswaoN2oXZcXhhJkLMGhD17vMHA1ts3TMhfeA5yA4Mnn",
    "8NRSnFtTDoFa31EvGEEb11n1rYBEzpUH9aNes6GH6Lab", "8FZPdehCg51mfRbmDVZ2NdbQ2JmY8ym5rSnDwqFGfG3E",
    "EuyDTS27zwGZiRai8LqRYBiALU9bsqws4ZuvrgvyMzuF", "8yG9XobFX9VpNVDYzAwoR8MrmCE5bC8ThoaKLJ4Bev7j",
    "DN9SS1n13yiHhQ4PwToTMfA9HRqadXYvHfurwpxKTmUm", "KYpUHEhDjpeBiECBkTKP66joLymtQ7LceFysyCq4SjT",
    "39j2aaXEQhVq7Jur5Wy9XpYqnat7TY5rJL9fLL8NfVU9", "Aop91KkNRco3eV99pyXCDxk23hfAkAD8PsUsTEumS4Uf",
    "6EbsHdF6UTckvhspsx32Zt6LB4R8S74AoiL1k71EC85E", "GtaDh72LZd8aPSHcrDNfLBZPVizwgQ47KMohNXtM7JP3",
    "CTqrcVyDwz99wXAa8USVFjwLpVNodMUWWtsmL75gM5JX", "H1cLzWt7bvAhzdS19Vw6RkZZHVbUaMTX7FtWLRqtojok",
]

print(f"Validating {len(WALLETS)} frequency wallets...", flush=True)
validated = []
for i, w in enumerate(WALLETS):
    if i % 10 == 0:
        print(f"  {i}/{len(WALLETS)} done, found {len(validated)} smart so far", flush=True)
    r = analyze_wallet(w)
    if not r or "err" in r or r.get("trades", 0) < 3:
        continue
    wr = r["wins"] / r["trades"] * 100
    if r["total_pnl"] > 0 and wr >= 50 and r["avg_hold"] >= 30:
        validated.append((w, r["total_pnl"], r["trades"], wr, r["avg_hold"]))

validated.sort(key=lambda x: -x[1])
print(f"\nVALIDATED {len(validated)} smart wallets out of {len(WALLETS)}:")
print(f"{'WALLET':<46} {'PNL':>9} {'TRD':>4} {'WR%':>5} {'HOLD':>6}")
for w, pnl, trd, wr, hold in validated:
    print(f"{w} {pnl:>+8.4f}  {trd:>3}  {wr:>4.0f}%  {hold:>5.0f}s")

with open("validated_80_results.txt", "w") as f:
    for w, pnl, trd, wr, hold in validated:
        f.write(f'    "{w}": "f_pnl_{pnl:+.2f}_wr_{wr:.0f}_h_{hold:.0f}",\n')
print(f"\nWritten to validated_80_results.txt")
