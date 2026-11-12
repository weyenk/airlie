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
