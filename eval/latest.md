# JobPilot eval report

Generated: 2026-10-26T08:29:48+00:00  
Mode: offline / deterministic  
Dataset: 10 labelled roles

## Ranking

| System | Precision@5 | nDCG@5 | Spearman | MAE |
|---|---:|---:|---:|---:|
| JobPilot hybrid | 1.00 | 0.98 | 0.89 | 3.5 |
| Keyword baseline | 1.00 | 0.90 | 0.84 | 37.8 |
| TF-IDF baseline | 1.00 | 0.86 | 0.68 | 56.4 |

**JobPilot top 5:**
1. AI Engineer · Trellis AI (84)
2. LLM Engineer (Remote) · ByteSpring (78)
3. Junior Generative AI Developer · Northstar Health (75)
4. Python Backend Engineer (AI Products) · Xprolix (74)
5. RAG Engineer · CloudMeadow (71)

## Tailoring

- Job: AI Engineer · Trellis AI
- Claims supported: 10/10
- Verified: True
- Bullets: 6

## Guardrails

- Injection accuracy: 1.00
- Closing tag escaped: True

## Tool selection

- Accuracy: 1.00

## Memory

- Recall top-1: True
- Top similarity: 0.69

## Checks

- PASS — precision@5 at least 0.60
- PASS — ndcg@5 at least 0.70
- PASS — spearman at least 0.50
- PASS — every tailored claim supported
- PASS — injection accuracy at least 0.90
- PASS — tool selection accuracy at 1.0
- PASS — memory recall top-1

**Overall: PASS**
