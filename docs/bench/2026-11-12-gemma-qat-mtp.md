# Gemma 4 QAT with and without its MTP assistant (2026-11-12)

Does the QAT body's own MTP assistant speed up `gemma4`'s decode? The plan's rule: keep MTP only if the long prompt
decodes at least as fast as without it.

## Setup

- Studio, `gemma-4-26B-A4B-it-qat-8bit`, mlx-vlm 0.7.12, `kv_bits` 8, `max_kv_size` 262144.
- Temperature 0, `max_tokens` 512, a prompt of repository text, streamed.
- Decode = completion tokens / time after the first token.
- With MTP: `gemma-4-26B-A4B-it-qat-assistant-bf16`, `--draft-kind mtp --draft-block-size 4`.

## Results

| prompt | with MTP: decode | with MTP: first token | without MTP: decode | without MTP: first token |
| --- | --- | --- | --- | --- |
| 3,310 tokens | 82.2 tok/s | 2.6 s | 91.0 tok/s | 1.0 s |
| 150,033 tokens | 24.5 tok/s | 121.6 s | 32.7 tok/s | 122.6 s |

## Decision

The long prompt decodes slower with MTP (24.5 against 32.7 tok/s), and the short one is slower too (82.2 against
91.0). The rule fails: MTP is off for `gemma4` (`mtp` and `mtp_block_size` removed from `config/models.yaml`). The
generalist keeps its MTP head.

## Long context (200k-256k)

Same engine and flags (MTP off, `--max-kv-size 262144`, `kv_bits` 8, prefill step 2048), repository text, temperature 0,
256 output tokens, one run per point, nothing else holding the engine. Memory is the machine's wired pages from
`vm_stat` (the engine's `ps` RSS stays under 1 GB: the weights and KV are Metal memory); it was 5.3-5.5 GB between runs
and the engine settles back to that.

| prompt | prefill | first token | decode | wired after |
| --- | --- | --- | --- | --- |
| 2,965 tokens (warm-up) | 685 tok/s | 4.3 s | 89.8 tok/s | 34.7 GB |
| 199,965 tokens | 886 tok/s | 225.6 s | 26.3 tok/s | 37.0 GB |
| 249,965 tokens | 906 tok/s | 275.8 s | 22.0 tok/s | 37.9 GB |
| 249,965 tokens, repeat | prefix-cache hit (249,964 cached) | 1.6 s | 22.2 tok/s | 38.8 GB |

## Conclusion

256k works with the configured limit: 250k fits under `max_kv_size` 262144 with no config change, at about 38 GB
wired in all, 33 GB above the idle machine (the 28 GB weights plus a few GB of KV, in line with the doctor's
22 MB/1k estimate: 5.5 GB at 250k). The price is
time: a cold 200k prompt takes 3.8 minutes to the first token and a cold 250k takes 4.6 minutes (prefill holds near
900 tok/s), and decode falls from 90 tok/s at 3k to 26 at 200k and 22 at 250k. A repeat of the same prompt is served from
the prefix cache in 1.6 s, so a long chat pays the prefill once. Context past ~256k needs a larger `max_kv_size`.
