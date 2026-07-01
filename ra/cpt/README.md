# CPT Data Pipeline

Continued pretraining data for OLS troubleshooting. Two sources: domain documentation and Stack Overflow Q&A.

## Data Sources

### Documentation Repos (18 repos, ~28K files, ~70MB text)

All shallow-cloned into `repos/` on `rh-h100-01:~/rawhad/ols-cpt/repos/`.

| Repo | License | Content |
|------|---------|---------|
| openshift-docs | Apache-2.0 | OpenShift product docs (~10.3M tokens, 59% of docs corpus) |
| kubernetes-website | CC-BY-4.0 | Kubernetes docs (~3.7M tokens, 21%) |
| prometheus-docs | CC-BY-4.0 | Prometheus documentation |
| alertmanager | Apache-2.0 | Alertmanager docs |
| helm-www | Apache-2.0 | Helm documentation |
| istio.io | Apache-2.0 | Istio service mesh docs |
| argocd | Apache-2.0 | Argo CD GitOps docs |
| calico-docs | Apache-2.0 | Calico networking docs |
| cilium | Apache-2.0 | Cilium networking/security |
| coredns | Apache-2.0 | CoreDNS plugin docs |
| cert-manager | Apache-2.0 | cert-manager TLS automation |
| tektoncd/pipeline | Apache-2.0 | Tekton CI/CD pipelines |
| etcd-io/website | Apache-2.0 | etcd distributed KV store |
| cri-o | Apache-2.0 | CRI-O container runtime |
| kubernetes-sigs/nfs-subdir-external-provisioner | Apache-2.0 | NFS provisioner |
| ovn-kubernetes | Apache-2.0 | OVN-Kubernetes networking |
| kube-proxy | Apache-2.0 | kube-proxy docs (in k8s website) |
| containerd | Apache-2.0 | containerd runtime |

Grafana was excluded due to AGPL-3.0 license risk.

### Stack Overflow (60 SEDE queries, ~539MB CSVs, ~157K unique posts after dedup)

Queried via [SEDE](https://data.stackexchange.com/) (Stack Exchange Data Explorer). Licensed CC BY-SA.

**Query template**: Top-voted questions with accepted/highest-scored answers, returning Title, QuestionBody (HTML), AnswerBody (HTML), Score, QuestionId.

**Tags scraped**:

| Category | Tags |
|----------|------|
| Core K8s | kubernetes (3 date batches), kubectl, kubelet, kube-proxy |
| K8s resources | kubernetes-deployment, kubernetes-statefulset, kubernetes-pod, kubernetes-ingress, kubernetes-pvc, kubernetes-secrets, kubernetes-cronjob, kubernetes-jobs, kubernetes-hpa, kubernetes-rbac, kubernetes-dns, kubernetes-networking, kubernetes-health-check, kubernetes-security |
| OpenShift | openshift, openshift-4, operator-sdk, operator-lifecycle-manager |
| Containers | docker (4 date batches, score>=1 filter), containers, containerd, cri-o |
| Networking | istio, calico, coredns, nfs |
| CI/CD | argocd, tekton, helm |
| Monitoring | prometheus, promql, alertmanager, grafana |
| Security | cert-manager |
| Storage | etcd |
| Cross-domain | kubernetes+postgresql, kubernetes+redis, kubernetes+elasticsearch, kubernetes+mongodb, kubernetes+cassandra, kubernetes+mysql, kubernetes+java, kubernetes+grpc, kubernetes+nginx-ingress |

**Date splitting**: Large tags (kubernetes, docker) were split into date ranges to stay under SEDE's download size/timeout limits. Docker also required a `Score >= 1` filter for pre-2021 data.

**Scraping method**: Chrome Remote Debugging + Playwright CDP. Headless Playwright was blocked by Cloudflare on data.stackexchange.com, so we connected to an already-running Chrome instance via CDP. Exponential backoff with jitter (max 10 retries, max 60s wait) for flaky downloads.

## Processing Pipeline

### HTML-to-text conversion (`html_to_text.py`)

BeautifulSoup4-based converter optimized for CPT:
- Code blocks preserved with triple backticks + language hints from CSS classes
- Inline `<code>` preserved with backticks
- Headings converted to markdown `#` syntax
- Lists converted to `- ` bullets
- Bold/italic markup stripped (noise for pretraining)
- Images stripped
- Links reduced to text content only

Three strategies were compared (regex, HTMLParser, bs4) on sample data. bs4 chosen for best fidelity on code blocks and inline code.

### Dataset builder (`build_dataset.py`)

Produces `cpt_dataset.jsonl` (one JSON object per line):

```jsonl
{"text": "...", "source": "docs", "repo": "coredns", "path": "coredns/plugin.md"}
{"text": "...", "source": "stackoverflow", "tag": "kubernetes__2023_2027", "qid": "12345"}
```

- Doc files: reads `*.md`, `*.txt`, `*.adoc`, `*.rst` from `repos/`, skips files < 50 chars
- SO posts: reads all CSVs from `so_data/`, global dedup by QuestionId across all files, applies html_to_text, formats as `## Title\n\nquestion\n\n### Answer\n\nanswer`, skips posts < 100 chars

## Output Stats

| Metric | Value |
|--------|-------|
| Total documents | 185,631 |
| Doc files | 28,402 |
| SO posts (after dedup) | 157,229 |
| SO dupes removed | 33,311 (17% cross-tag overlap) |
| Dataset file size | 572.7 MB |
| Location | `rh-h100-01:~/rawhad/ols-cpt/cpt_dataset.jsonl` |

## Data Location

All raw and processed data lives on `rh-h100-01:~/rawhad/ols-cpt/`:

```
ols-cpt/
  repos/              # 18 shallow-cloned doc repos
  so_data/            # 60 raw SEDE CSV files (539MB)
  so_corpus/          # intermediate plain text (from process_so_data.py)
  cpt_dataset.jsonl   # final CPT dataset (572.7MB)
  html_to_text.py     # HTML converter
  build_dataset.py    # dataset builder
  count_tokens.py     # token counter for doc repos
```
