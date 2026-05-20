#!/bin/bash
# Resolve mint addresses from buy sigs via Helius
set -e
source /root/piggy/.env
declare -A SIGS=(
    [WIN_AQjZ]="24N7uEX7LGp7KheutaVKAvwDRx5xcpPTtHVoJ7BBisuf5iLVTsLXbptxTgC6jJgCC8w35MjrkVKwWrN6M9XrZ2Jt"
    [WIN_5KXW]="5mAbwN5z4xc3skvT5JNdaZDWUZb8LoWBgC9h3eQvRVudHvUBjGpejDiKmWv3fAoyT8ASHkQUFU7uhqNzQauRWDJq"
    [WIN_FfbD]="3yCWvrUXCjXdCWyBfzzZHHDEw6MhcgKhkx5hTFMXsu7UEicnNCocMXn9BUZkJt8W2tKuDWSi1E1q19iTujir24Uf"
    [DUMP_F8rJ]="4hPpM1wv7UVhz3WCmHbo17DPx22YZaBDXwvqpHZR42Mi2V2iLZTuXjtZfZoUBgZbwWqBPECLU919DAbxA6CGxdHx"
    [WIN_H4u7]="5QESpTLjBbPgJw1gMNDNko1Ww9F6N8TThZQEN5Ngumkx8QN6FnxmurZNNZBgxRm7oYPUyXfPjZH9FhWuZae96ijT"
    [WIN_5ARd]="3hKGRuNS9pzgku11cChNWX4goBj2qbRYeFaeSG1bqrpSSWyiu7iHNrFp8oPwKxjrCg81W95xuUFqxiqQzQnuzaSz"
    [WIN_HW3P]="3YSVMjihYLU21NCXza7kAbZV7qnKmxVfnV5DNFLo7UT3KpQDeaBQdeJJvFmdQ4m6aNcAQr9J9L5BFeK8tzcVdpLd"
    [DUMP_HqpP]="4oNJ9JExzGG3PHcYNj7DbAUuW4t12FV5ojAYDUab6JfERQUCCBvtchhQQDR3sq8QCpJ3qqay5jpKDe1iJWVMs3id"
)
for LABEL in "${!SIGS[@]}"; do
    SIG="${SIGS[$LABEL]}"
    MINT=$(curl -s -X POST "https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY}" \
        -H 'Content-Type: application/json' \
        -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getTransaction\",\"params\":[\"$SIG\",{\"encoding\":\"jsonParsed\",\"maxSupportedTransactionVersion\":0}]}" \
        | /root/piggy/venv/bin/python -c "import json,sys; d=json.load(sys.stdin); r=d.get('result'); mints=[t['mint'] for t in (r.get('meta',{}).get('postTokenBalances',[]) or []) if t.get('mint') and t['mint']!='So11111111111111111111111111111111111111112']; print(mints[0] if mints else '')" 2>/dev/null)
    echo "$LABEL=$MINT"
done
