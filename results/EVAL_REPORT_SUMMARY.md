# Inference-Time Scaling for OpenShift Troubleshooting

## Evaluation Setup

**Three** open-source models were evaluated on the lightspeed-service eval consisting of **11** scenarios deployed on a CRC (CodeReady Containers) OpenShift cluster. Each model was tested with and without ITS, running **5 iterations** per configuration. An LLM judge **(claude-haiku-4-5)** scored each response. **Pass threshold: score ≥ 0.50.**

### Models Evaluated

| Model | Parameters | Architecture | Serving |
|-------|-----------|--------------|---------|
| Gemma 4 12B-IT | 12B | Dense | vLLM on 1x H100 |
| Gemma 4 31B-IT | 31B | Dense | vLLM on 2x H100 |
| Qwen3.6 35B-A3B | 35B (3B active) | Mixture of Experts | vLLM on 2x H100 |

All three models completed evaluation runs with zero or near-zero errors, providing reliable results for comparison.

---

## Results Overview: Baseline vs ITS

| Model | Baseline | Best ITS | Improvement |
|-------|:---------:|:---------:|:------:|
| Gemma 4 12B-IT | 60% | 76% | **+16 pts** |
| Gemma 4 31B-IT | 73% | 78% | **+5 pts** |
| Qwen3.6 35B-A3B | 78% | 90% | **+12 pts** |

Pass rate across all 100 metric evaluations per configuration (20 metrics x 5 iterations). Best ITS is the highest-scoring ITS configuration for each model.

### Answer Correctness Metric

| Model | Baseline | Best ITS | Improvement |
|-------|:---------:|:---------:|:------:|
| Gemma 4 12B-IT | 53.3% | 81.2% | **+27.9 pts** |
| Gemma 4 31B-IT | 77.0% | 82.4% | **+5.5 pts** |
| Qwen3.6 35B-A3B | 74.5% | 90.9% | **+16.4 pts** |

Pass rate on the `answer_correctness` metric only, averaged across 11 scenarios. Multi-turn scenarios (wrong_networkpolicy, 3 turns) are scored per-turn. This measures whether the model identifies the correct root cause, independent of conversational quality metrics.

## Results by Scenario

### Discriminating scenarios (where ITS and model scale diverge)

These scenarios show meaningful variation across models and ITS configurations.

| Scenario | Category | Gemma 12B | Gemma 31B | Qwen3.6 |
|----------|----------|:---------:|:---------:|:---------:|
| periodic_failure_window | Temporal Reasoning | +100 pts | +20 pts | +100 pts |
| scheduled_outage_detection | Temporal Reasoning | +100 pts | +40 pts | +80 pts |

**Temporal reasoning** is where ITS provides the largest improvement. All three models score 0% on scheduled_outage_detection at baseline — ITS takes them to 40-100%. Multiple sampling attempts help the model correctly parse timestamps and isolate time windows.

### Easy scenarios (ITS closes the gap for Gemma 12B)

Gemma 31B and Qwen3.6 already solve these at baseline. ITS brings Gemma 12B to 100%.

| Scenario | Category | Gemma 12B | Gemma 31B | Qwen3.6 |
|----------|----------|:---------:|:---------:|:---------:|
| oom | Resource Status | +40 pts | +0 pts | +0 pts |
| readiness_probe_diagnosis | Resource Status | +20 pts | +0 pts | +0 pts |
| batch_failure | Log Analysis | +40 pts | +0 pts | +0 pts |

### Solved scenarios (100% baseline, +0 pts ITS improvement)

These scenarios are fully solved at baseline.

| Scenario | Category |
|----------|----------|
| envvar_missing | Resource Status |
| namespace_pod_count | Precision |
| storage_binding | Log Analysis |


### Reasoning-bound scenarios (where model needs better reasoning, not more samples)

These scenarios show bottleneck in the models reasoning capability, not sampling.

| Scenario | Category | Gemma 12B | Gemma 31B | Qwen3.6 |
|----------|----------|:---------:|:---------:|:---------:|
| ingress_rule_mismatch | Network Diagnosis | +20 pts | +0 pts | -20 pts |
| wrong_networkpolicy | Network Diagnosis | +7 pts | +0 pts | +0 pts |
| config_drift_analysis | Log Analysis | -20 pts | +0 pts | +20 pts |

---

## Additional Metrics

### Conversational Quality Metrics

These metrics are evaluated only on the multi-turn scenario (`wrong_networkpolicy`, 3 turns). They measure how well the model conducts a troubleshooting conversation beyond just getting the right answer.

| Metric | Gemma 12B | Gemma 31B | Qwen3.6 |
|--------|:---------:|:---------:|:---------:|
| generic_troubleshooting_experience | +0 pts | 14 pts | +20 pts |
| troubleshooting_continuity | +0 pts | +20 pts | +0 pts |
| conversation_completeness | +0 pts | +0 pts | +0 pts |
| conversation_relevancy | +0 pts | +0 pts | +0 pts |

- **conversation_completeness** and **conversation_relevancy** are 100% across the board — all models stay on-topic and cover the required ground.
- **generic_troubleshooting_experience** improves with ITS for Gemma 31B (+14 pts) and Qwen3.6 (+20 pts), suggesting ITS helps models produce better-structured diagnostic responses.

---