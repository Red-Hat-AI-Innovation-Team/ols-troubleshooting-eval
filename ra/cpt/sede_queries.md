# SEDE Queries

All queries run against [data.stackexchange.com/stackoverflow](https://data.stackexchange.com/stackoverflow/query/new).

## Scraping Method

Chrome Remote Debugging + Playwright CDP. Headless Playwright was blocked by Cloudflare, so we connected to an already-running Chrome instance (`--remote-debugging-port=9222`) via CDP. Exponential backoff with jitter (max 10 retries, max 60s wait) for transient failures.

Script: `sede_cdp.py` (not committed, lived in temp dir during scraping).

## Single-Tag Query Template

Returns top 50,000 open questions (by score) with the best answer (accepted first, then highest-scored).

```sql
SELECT TOP 50000
  q.Id AS QuestionId, q.Title, q.Tags,
  q.Score AS QScore, q.ViewCount, q.CreationDate,
  q.Body AS QuestionBody,
  a.Id AS AnswerId, a.Score AS AScore, a.Body AS AnswerBody
FROM Posts q
JOIN PostTags pt ON pt.PostId = q.Id
JOIN Tags t ON t.Id = pt.TagId
OUTER APPLY (
  SELECT TOP 1 a2.Id, a2.Score, a2.Body
  FROM Posts a2
  WHERE a2.ParentId = q.Id AND a2.PostTypeId = 2
  ORDER BY
    CASE WHEN a2.Id = q.AcceptedAnswerId THEN 0 ELSE 1 END,
    a2.Score DESC
) a
WHERE t.TagName = '{{TAG}}'
  AND q.PostTypeId = 1
  AND q.ClosedDate IS NULL
  -- Optional date filter for large tags:
  -- AND q.CreationDate >= '{{START}}' AND q.CreationDate < '{{END}}'
  -- Optional score filter (used for docker):
  -- AND q.Score >= 1
ORDER BY q.Score DESC
```

## Cross-Domain Query Template

Questions tagged with both `kubernetes` and a second technology tag.

```sql
SELECT TOP 50000
  q.Id AS QuestionId, q.Title, q.Tags,
  q.Score AS QScore, q.ViewCount, q.CreationDate,
  q.Body AS QuestionBody,
  a.Id AS AnswerId, a.Score AS AScore, a.Body AS AnswerBody
FROM Posts q
INNER JOIN PostTags pt1 ON pt1.PostId = q.Id
INNER JOIN Tags t1 ON t1.Id = pt1.TagId AND t1.TagName = 'kubernetes'
INNER JOIN PostTags pt2 ON pt2.PostId = q.Id
INNER JOIN Tags t2 ON t2.Id = pt2.TagId AND t2.TagName = '{{TAG2}}'
OUTER APPLY (
  SELECT TOP 1 a2.Id, a2.Score, a2.Body
  FROM Posts a2
  WHERE a2.ParentId = q.Id AND a2.PostTypeId = 2
  ORDER BY
    CASE WHEN a2.Id = q.AcceptedAnswerId THEN 0 ELSE 1 END,
    a2.Score DESC
) a
WHERE q.PostTypeId = 1
  AND q.ClosedDate IS NULL
ORDER BY q.Score DESC
```

## Queries Executed (60 total)

### Tag Counts (1 query)

```sql
SELECT TagName, Count FROM Tags
WHERE TagName IN ('alertmanager', 'argo-cd', 'argocd', 'calico', 'cassandra',
  'cert-manager', 'containerd', 'container-runtime', 'containers', 'coredns',
  'cri-o', 'docker', 'elasticsearch', 'etcd', 'grafana', 'grafana-alerting',
  'grpc', 'helm', 'istio', 'java', 'kube-proxy', 'kubectl', 'kubelet',
  'kubernetes', 'kubernetes-configmap', 'kubernetes-cronjob',
  'kubernetes-daemonset', 'kubernetes-deployment', 'kubernetes-dns',
  'kubernetes-hpa', 'kubernetes-health-check', 'kubernetes-ingress',
  'kubernetes-jobs', 'kubernetes-networking', 'kubernetes-pod',
  'kubernetes-pvc', 'kubernetes-rbac', 'kubernetes-secrets',
  'kubernetes-security', 'kubernetes-statefulset', 'mongodb', 'mysql',
  'nfs', 'nginx-ingress', 'openshift', 'openshift-4',
  'operator-lifecycle-manager', 'operator-sdk', 'ovn', 'postgresql',
  'prometheus', 'promql', 'redis', 'tekton')
ORDER BY Count DESC
```

### Large Tags — Date Partitioned (7 queries)

| Tag | Date Range | Score Filter | Output File |
|-----|------------|--------------|-------------|
| kubernetes | 2015-01-01 to 2021-01-01 | none | `single__kubernetes__2015_2021.csv` |
| kubernetes | 2021-01-01 to 2023-01-01 | none | `single__kubernetes__2021_2023.csv` |
| kubernetes | 2023-01-01 to 2027-01-01 | none | `single__kubernetes__2023_2027.csv` |
| docker | 2008-01-01 to 2017-01-01 | Score >= 1 | `single__docker__2008_2017.csv` |
| docker | 2017-01-01 to 2020-01-01 | Score >= 1 | `single__docker__2017_2020.csv` |
| docker | 2020-01-01 to 2022-01-01 | Score >= 1 | `single__docker__2020_2022.csv` |
| docker | 2022-01-01 to 2027-01-01 | Score >= 1 | `single__docker__2022_2027.csv` |

Docker date ranges were split into 4 (originally 3) because the pre-2021 batch exceeded SEDE's download timeout. The `Score >= 1` filter was added to further reduce download size.

### Single Tags (43 queries)

| # | Tag | Output File |
|---|-----|-------------|
| 1 | openshift | `single__openshift.csv` |
| 2 | kubectl | `single__kubectl.csv` |
| 3 | helm | `single__helm.csv` |
| 4 | prometheus | `single__prometheus.csv` |
| 5 | grafana | `single__grafana.csv` |
| 6 | istio | `single__istio.csv` |
| 7 | etcd | `single__etcd.csv` |
| 8 | calico | `single__calico.csv` |
| 9 | tekton | `single__tekton.csv` |
| 10 | argocd | `single__argocd.csv` |
| 11 | argo-cd | `single__argo_cd.csv` |
| 12 | alertmanager | `single__alertmanager.csv` |
| 13 | cri-o | `single__cri_o.csv` |
| 14 | coredns | `single__coredns.csv` |
| 15 | ovn | `single__ovn.csv` |
| 16 | containers | `single__containers.csv` |
| 17 | container-runtime | `single__container_runtime.csv` |
| 18 | kubernetes-ingress | `single__kubernetes_ingress.csv` |
| 19 | kubernetes-networking | `single__kubernetes_networking.csv` |
| 20 | kube-proxy | `single__kube_proxy.csv` |
| 21 | kubernetes-dns | `single__kubernetes_dns.csv` |
| 22 | kubernetes-pvc | `single__kubernetes_pvc.csv` |
| 23 | nfs | `single__nfs.csv` |
| 24 | kubernetes-pod | `single__kubernetes_pod.csv` |
| 25 | kubernetes-deployment | `single__kubernetes_deployment.csv` |
| 26 | kubernetes-statefulset | `single__kubernetes_statefulset.csv` |
| 27 | kubernetes-cronjob | `single__kubernetes_cronjob.csv` |
| 28 | kubernetes-jobs | `single__kubernetes_jobs.csv` |
| 29 | kubernetes-daemonset | `single__kubernetes_daemonset.csv` |
| 30 | kubernetes-hpa | `single__kubernetes_hpa.csv` |
| 31 | kubernetes-health-check | `single__kubernetes_health_check.csv` |
| 32 | kubernetes-configmap | `single__kubernetes_configmap.csv` |
| 33 | kubernetes-secrets | `single__kubernetes_secrets.csv` |
| 34 | cert-manager | `single__cert_manager.csv` |
| 35 | kubernetes-rbac | `single__kubernetes_rbac.csv` |
| 36 | kubernetes-security | `single__kubernetes_security.csv` |
| 37 | kubelet | `single__kubelet.csv` |
| 38 | containerd | `single__containerd.csv` |
| 39 | promql | `single__promql.csv` |
| 40 | grafana-alerting | `single__grafana_alerting.csv` |
| 41 | openshift-4 | `single__openshift_4.csv` |
| 42 | operator-sdk | `single__operator_sdk.csv` |
| 43 | operator-lifecycle-manager | `single__operator_lifecycle_manager.csv` |

Note: some tags returned 0 or near-0 results (e.g., `ovn`, `container-runtime`, `kubernetes-daemonset`, `kubernetes-configmap`, `grafana-alerting`). These produced empty or near-empty CSVs.

### Cross-Domain: kubernetes + X (9 queries)

| # | Second Tag | Output File |
|---|------------|-------------|
| 1 | postgresql | `cross__kubernetes__postgresql.csv` |
| 2 | redis | `cross__kubernetes__redis.csv` |
| 3 | elasticsearch | `cross__kubernetes__elasticsearch.csv` |
| 4 | mongodb | `cross__kubernetes__mongodb.csv` |
| 5 | cassandra | `cross__kubernetes__cassandra.csv` |
| 6 | mysql | `cross__kubernetes__mysql.csv` |
| 7 | java | `cross__kubernetes__java.csv` |
| 8 | grpc | `cross__kubernetes__grpc.csv` |
| 9 | nginx-ingress | `cross__kubernetes__nginx_ingress.csv` |
