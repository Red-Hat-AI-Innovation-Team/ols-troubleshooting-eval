# OLS Troubleshooting Eval Report

## Overview

This report presents evaluation results for three open-source models on the OLS (OpenShift Lightspeed) troubleshooting benchmark. Each model was tested across five configurations: a baseline (no inference-time scaling) and four ITS (Inference-Time Scaling) configurations varying budget (4 or 8) and voting strategy (hierarchical or flat_all).

All metrics are counted. Each scenario produces 20 metrics per iteration (10 single-turn scenarios × 1 metric + 1 multi-turn scenario × 10 metrics). With 5 iterations, this gives 100 data points per configuration. **Pass threshold: score ≥ 0.50.**

### Models Evaluated

| Model | Parameters | Architecture | Serving |
|-------|-----------|--------------|---------|
| Gemma 4 12B-IT | 12B | Dense | vLLM on 1x H100 |
| Gemma 4 31B-IT | 31B | Dense | vLLM on 2x H100 |
| Qwen3.6 35B-A3B | 35B (3B active) | Mixture of Experts | vLLM on 2x H100 |

### Evaluation Setup

- **Scenarios**: 11 troubleshooting scenarios on a CRC (CodeReady Containers) OpenShift cluster
- **Iterations**: 5 per configuration (100 metric evaluations per config)
- **Metrics**: `answer_correctness`, `generic_troubleshooting_experience`, `troubleshooting_continuity`, `conversation_completeness`, `conversation_relevancy`, `knowledge_retention`
- **Judge model**: claude-haiku-4-5
- **ITS algorithm**: Self-consistency with tool-aware voting
- **Pass threshold**: score ≥ 0.50

---

## Overall Results

### Pass Rate Summary (all metrics, 100 data points per config)

| Model | Config | Pass | Fail | Error | Total | Pass% |
|-------|--------|------|------|-------|-------|-------|
| Gemma 4 12B-IT | Baseline | 60 | 40 | 0 | 100 | 60.0% |
| Gemma 4 12B-IT | ITS-4 Hierarchical | 61 | 39 | 0 | 100 | 61.0% |
| Gemma 4 12B-IT | ITS-4 Flat All | 58 | 41 | 1 | 100 | 58.0% |
| Gemma 4 12B-IT | ITS-8 Hierarchical | 65 | 35 | 0 | 100 | 65.0% |
| Gemma 4 12B-IT | ITS-8 Flat All | 76 | 24 | 0 | 100 | 76.0% |
| | | | | | | |
| Gemma 4 31B-IT | Baseline | 73 | 27 | 0 | 100 | 73.0% |
| Gemma 4 31B-IT | ITS-4 Hierarchical | 66 | 34 | 0 | 100 | 66.0% |
| Gemma 4 31B-IT | ITS-4 Flat All | 73 | 27 | 0 | 100 | 73.0% |
| Gemma 4 31B-IT | ITS-8 Hierarchical | 73 | 27 | 0 | 100 | 73.0% |
| Gemma 4 31B-IT | ITS-8 Flat All | 78 | 22 | 0 | 100 | 78.0% |
| | | | | | | |
| Qwen3.6 35B-A3B | Baseline | 78 | 22 | 0 | 100 | 78.0% |
| Qwen3.6 35B-A3B | ITS-4 Hierarchical | 82 | 18 | 0 | 100 | 82.0% |
| Qwen3.6 35B-A3B | ITS-4 Flat All | 83 | 17 | 0 | 100 | 83.0% |
| Qwen3.6 35B-A3B | ITS-8 Hierarchical | 90 | 10 | 0 | 100 | 90.0% |
| Qwen3.6 35B-A3B | ITS-8 Flat All | 80 | 20 | 0 | 100 | 80.0% |

### Pass Rate Summary (answer_correctness metric only)

| Model | Config | Pass Rate |
|-------|--------|--------|
| Gemma 4 12B-IT | Baseline | 53.3% |
| Gemma 4 12B-IT | ITS-4 Hierarchical | 62.4% |
| Gemma 4 12B-IT | ITS-4 Flat All | 53.9% |
| Gemma 4 12B-IT | ITS-8 Hierarchical | 60.0% |
| Gemma 4 12B-IT | ITS-8 Flat All | 81.2% |
| | | |
| Gemma 4 31B-IT | Baseline | 77.0% |
| Gemma 4 31B-IT | ITS-4 Hierarchical | 67.9% |
| Gemma 4 31B-IT | ITS-4 Flat All | 78.2% |
| Gemma 4 31B-IT | ITS-8 Hierarchical | 75.8% |
| Gemma 4 31B-IT | ITS-8 Flat All | 82.4% |
| | | |
| Qwen3.6 35B-A3B | Baseline | 74.5% |
| Qwen3.6 35B-A3B | ITS-4 Hierarchical | 80.0% |
| Qwen3.6 35B-A3B | ITS-4 Flat All | 89.7% |
| Qwen3.6 35B-A3B | ITS-8 Hierarchical | 90.9% |
| Qwen3.6 35B-A3B | ITS-8 Flat All | 76.4% |

### ITS Impact on answer_correctness (Baseline vs Best ITS Config)

| Model | Baseline | Best ITS | Config | Improvement |
|-------|----------|----------|--------|-------------|
| Gemma 4 12B-IT | 53.3% | 81.2% | ITS-8 Flat All | +27.9 pts |
| Gemma 4 31B-IT | 77.0% | 82.4% | ITS-8 Flat All | +5.5 pts |
| Qwen3.6 35B-A3B | 74.5% | 90.9% | ITS-8 Hierarchical | +16.4 pts |

---

## Per-Scenario Results

pass rate by scenario and ITS configuration (n=5 iterations each, `answer_correctness` only).

### Gemma 4 12B-IT

| Scenario | Baseline | ITS-4 Hier | ITS-4 Flat | ITS-8 Hier | ITS-8 Flat |
|----------|-------:|-------:|-------:|-------:|-------:|
| batch_failure | 60% | 80% | 60% | 100% | 100% |
| config_drift_analysis | 40% | 60% | 0% | 0% | 20% |
| envvar_missing | 100% | 100% | 100% | 100% | 100% |
| ingress_rule_mismatch | 20% | 60% | 20% | 20% | 40% |
| namespace_pod_count | 100% | 100% | 100% | 100% | 100% |
| oom | 60% | 100% | 100% | 100% | 100% |
| periodic_failure_window | 0% | 0% | 0% | 0% | 100% |
| readiness_probe_diagnosis | 80% | 100% | 100% | 100% | 100% |
| scheduled_outage_detection | 0% | 0% | 0% | 0% | 100% |
| storage_binding | 100% | 80% | 80% | 100% | 100% |
| wrong_networkpolicy | 27% | 7% | 33% | 40% | 33% |
| **Average** | **53%** | **62%** | **54%** | **60%** | **81%** |

### Gemma 4 31B-IT

| Scenario | Baseline | ITS-4 Hier | ITS-4 Flat | ITS-8 Hier | ITS-8 Flat |
|----------|-------:|-------:|-------:|-------:|-------:|
| batch_failure | 100% | 100% | 100% | 100% | 100% |
| config_drift_analysis | 20% | 0% | 0% | 0% | 20% |
| envvar_missing | 100% | 100% | 100% | 100% | 100% |
| ingress_rule_mismatch | 100% | 80% | 100% | 100% | 100% |
| namespace_pod_count | 100% | 100% | 100% | 100% | 100% |
| oom | 100% | 80% | 100% | 100% | 100% |
| periodic_failure_window | 80% | 60% | 60% | 40% | 100% |
| readiness_probe_diagnosis | 100% | 80% | 100% | 100% | 100% |
| scheduled_outage_detection | 0% | 20% | 60% | 60% | 40% |
| storage_binding | 100% | 100% | 100% | 100% | 100% |
| wrong_networkpolicy | 47% | 27% | 40% | 33% | 47% |
| **Average** | **77%** | **68%** | **78%** | **76%** | **82%** |

### Qwen3.6 35B-A3B

| Scenario | Baseline | ITS-4 Hier | ITS-4 Flat | ITS-8 Hier | ITS-8 Flat |
|----------|-------:|-------:|-------:|-------:|-------:|
| batch_failure | 100% | 100% | 100% | 100% | 100% |
| config_drift_analysis | 20% | 20% | 20% | 40% | 0% |
| envvar_missing | 100% | 100% | 100% | 100% | 100% |
| ingress_rule_mismatch | 100% | 100% | 100% | 80% | 100% |
| namespace_pod_count | 100% | 100% | 100% | 100% | 100% |
| oom | 100% | 100% | 100% | 100% | 100% |
| periodic_failure_window | 0% | 60% | 100% | 100% | 40% |
| readiness_probe_diagnosis | 100% | 100% | 100% | 100% | 100% |
| scheduled_outage_detection | 0% | 0% | 80% | 80% | 0% |
| storage_binding | 100% | 100% | 100% | 100% | 100% |
| wrong_networkpolicy | 100% | 100% | 87% | 100% | 100% |
| **Average** | **75%** | **80%** | **90%** | **91%** | **76%** |

---
